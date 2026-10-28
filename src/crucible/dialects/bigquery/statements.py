"""Split a BigQuery request into statements and route each one (docs/architecture.md)."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.bigquery import BigQuery
from sqlglot.errors import ParseError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.tokenizer_core import TokenType

from crucible.dialects.bigquery import (
    ddl,
    dml,
    errors,
    metadata,
    net,
    parameters,
    scripting,
    types,
    udfs,
)
from crucible.dialects.bigquery.keywords import RESERVED
from crucible.dialects.bigquery.translate import (
    fix_annotation,
    outer_selects,
    schema_for,
    translate_query,
)
from crucible.engine import (
    CommandPlan,
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    Location,
    Plan,
    Result,
)
from crucible.engine.catalog import qualified


@dataclass(frozen=True)
class Statement:
    tree: exp.Expr


def split(sql: str) -> list[object]:
    try:
        trees = sqlglot.parse(sql, read="bigquery")
    except ParseError as exc:
        detail = exc.errors[0] if exc.errors else {}
        line, col = detail.get("line"), detail.get("col")
        location = Location(int(line), int(col)) if line and col else None
        message = str(detail.get("description") or exc).strip()
        raise CrucibleError(
            ErrorKind.INVALID_QUERY, f"Syntax error: {message}", location=location
        ) from exc
    statements: list[object] = []
    _refuse_unknown_functions(sql)
    starts = _statement_starts(sql)
    _locate_parameters([t for t in trees if t is not None], sql)
    position = 0  # positional parameters (`?`) count across the whole request
    for index, tree in enumerate(trees):
        if tree is not None:
            for placeholder in tree.find_all(exp.Placeholder, bfs=False):  # text order
                position += 1
                placeholder.meta[parameters.POSITION] = position
            _check_reserved_aliases(tree, sql)
            if index < len(starts):
                tree.meta[errors.STATEMENT_START] = starts[index]
            statements.append(Statement(tree))
    if not statements:
        raise errors.invalid("Syntax error: the request has no statement")
    return statements


# Functions SQLGlot parses into BigQuery nodes although BigQuery has no function by that name
# (recorded: "Function not found: BOOL_AND at [1:8]"; LOGICAL_AND is BigQuery's).
_NOT_BIGQUERY_FUNCTIONS = frozenset({"BOOL_AND", "BOOL_OR"})


def _location_of(sql: str, start: int) -> Location:
    line = sql.count("\n", 0, start) + 1
    return Location(line, start - (sql.rfind("\n", 0, start) + 1) + 1)


def _refuse_unknown_functions(sql: str) -> None:
    tokens = BigQuery().tokenize(sql)
    for token, after in itertools.pairwise(tokens):
        if after.token_type == TokenType.L_PAREN and token.text.upper() in _NOT_BIGQUERY_FUNCTIONS:
            raise CrucibleError(
                ErrorKind.INVALID_QUERY,
                f"Function not found: {token.text}",
                location=_location_of(sql, token.start),
            )


def _locate_parameters(trees: list[exp.Expr], sql: str) -> None:
    """Mark each `@name` with where it starts, for "Query parameter 'x' not found at [1:8]"
    (jobs_query_parameters_missing). SQLGlot keeps no position on parameters."""
    tokens = BigQuery().tokenize(sql)
    starts = [
        _location_of(sql, token.start)
        for i, token in enumerate(tokens)
        if token.token_type == TokenType.PARAMETER
        and (i == 0 or tokens[i - 1].token_type != TokenType.PARAMETER)
        and (i + 1 == len(tokens) or tokens[i + 1].token_type != TokenType.PARAMETER)
    ]
    nodes = [n for tree in trees for n in tree.find_all(exp.Parameter, bfs=False)]
    if len(nodes) == len(starts):  # otherwise (`@@` system variables) leave them unlocated
        for node, start in zip(nodes, starts, strict=True):
            node.meta[parameters.LOCATION] = start


def _statement_starts(sql: str) -> list[Location]:
    """Where each statement's first token is, for errors BigQuery reports at the statement
    (recorded: "UPDATE must have a WHERE clause at [1:1]")."""
    starts: list[Location] = []
    expecting = True
    for token in BigQuery().tokenize(sql):
        if token.token_type == TokenType.SEMICOLON:
            expecting = True
        elif expecting:
            starts.append(_location_of(sql, token.start))
            expecting = False
    return starts


def _check_reserved_aliases(tree: exp.Expr, sql: str) -> None:
    """SQLGlot accepts `SELECT 1 AS by`; BigQuery reports an unquoted reserved keyword used as a
    name as a syntax error (recorded: "Syntax error: Unexpected keyword BY")."""
    for node in tree.find_all(exp.Alias, exp.TableAlias):
        identifier = node.args.get("alias") if isinstance(node, exp.Alias) else node.this
        if not isinstance(identifier, exp.Identifier) or identifier.quoted:
            continue
        word = identifier.name.upper()
        if word in RESERVED:
            start = identifier.meta.get("start")
            location = None
            if isinstance(start, int):
                line = sql.count("\n", 0, start) + 1
                location = Location(line, start - (sql.rfind("\n", 0, start) + 1) + 1)
            raise CrucibleError(
                ErrorKind.INVALID_QUERY,
                f"Syntax error: Unexpected keyword {word}",
                location=location,
            )


def plan(statement: object, ctx: ExecContext) -> Plan:
    assert isinstance(statement, Statement)
    planned = _plan(statement.tree.copy(), ctx)
    target = _target(statement.tree, ctx)
    if target is None or not isinstance(planned, CommandPlan):
        return planned
    run = planned.run

    def with_target(c: ExecContext) -> Result:
        # The server reports it as the job's destination table (jobs.get configuration).
        result = run(c)
        result.info["target"] = (target.project, target.dataset, target.table)
        return result

    return CommandPlan(with_target, planned.statement_type)


def _target(tree: exp.Expr, ctx: ExecContext) -> metadata.Name | None:
    """The table a CREATE TABLE / CREATE VIEW / INSERT / MERGE / UPDATE / DELETE writes."""
    creates = isinstance(tree, exp.Create) and (tree.kind or "").upper() in ("TABLE", "VIEW")
    if not creates and not isinstance(
        tree, exp.Insert | exp.Merge | exp.Update | exp.Delete | exp.Alter
    ):
        return None
    target = tree.this
    if isinstance(tree, exp.Delete) and not isinstance(target, exp.Table):
        target = (tree.args.get("tables") or [None])[0]  # `DELETE ds.t WHERE ...`
    table = target.this if isinstance(target, exp.Schema) else target
    return metadata.name_of(table, ctx) if isinstance(table, exp.Table) else None


# Functions SQLGlot parses without modelling them; a transform handles each of these by name.
_HANDLED_ANONYMOUS = frozenset({"IEEE_DIVIDE"})
_ML = (exp.Predict, exp.MLForecast, exp.MLTranslate, exp.GenerateEmbedding, exp.GenerateText)


def _refuse_unmodelled(tree: exp.Expr) -> None:
    """Refuse functions Crucible does not emulate before DuckDB runs its own namesake (RANGE)."""
    for node in tree.find_all(exp.Anonymous, exp.SHA2Digest, *_ML):
        if isinstance(node, exp.Anonymous) and node.name.upper() not in _HANDLED_ANONYMOUS:
            raise CrucibleUnsupportedError(f"function {node.name.upper()}")
        if isinstance(node, exp.SHA2Digest) and node.text("length") not in ("", "256"):
            raise CrucibleUnsupportedError(f"function SHA{node.text('length')}")
        if isinstance(node, _ML):
            raise CrucibleUnsupportedError("BigQuery ML functions")


def _plan(tree: exp.Expr, ctx: ExecContext) -> Plan:
    # Crucible's Python functions, registered once per engine (NET.REG_DOMAIN).
    ctx.engine.create_function(net.FUNCTION, net.udf, ["VARCHAR"], "VARCHAR")
    if isinstance(tree, exp.Create) and (tree.kind or "").upper() == "FUNCTION":
        return udfs.plan_create(tree, ctx)  # its body is checked when the function is called
    if isinstance(tree, exp.Drop) and (tree.kind or "").upper() == "FUNCTION":
        return udfs.plan_drop(tree, ctx)
    tree = udfs.expand(tree, ctx)
    _refuse_unmodelled(tree)
    if isinstance(tree, exp.Query):
        return run_sql(translate_query(tree, ctx).sql, "SELECT", ctx)
    if isinstance(tree, exp.Create):
        return ddl.plan_create(tree, ctx)
    if isinstance(tree, exp.Alter):
        return ddl.plan_alter(tree, ctx)
    if isinstance(tree, exp.Drop):
        return ddl.plan_drop(tree, ctx)
    if isinstance(tree, exp.Merge):
        return dml.plan_merge(tree, ctx)
    if isinstance(tree, exp.Update):
        return dml.plan_update(tree, ctx)
    if isinstance(tree, exp.Delete):
        return dml.plan_delete(tree, ctx)
    if isinstance(tree, exp.TruncateTable):
        return dml.plan_truncate(tree, ctx)
    if isinstance(tree, exp.Insert):
        return _insert(tree, ctx)
    if isinstance(tree, exp.Declare):
        return scripting.plan_declare(tree, ctx, lambda q: _plan(q, ctx))
    if isinstance(tree, exp.Set):
        return scripting.plan_set(tree, ctx, lambda q: _plan(q, ctx))
    if isinstance(tree, exp.Command) and tree.name.upper() == "ALTER":
        return ddl.plan_alter_column(str(tree.args.get("expression") or ""), ctx)
    if isinstance(tree, exp.Command):
        raise CrucibleUnsupportedError(f"{tree.name.upper()} statement")
    raise CrucibleUnsupportedError(f"{type(tree).__name__.upper()} statement")


def run_sql(sql: str, statement_type: str, ctx: ExecContext, *, returns_rows: bool = True) -> Plan:
    """Run generated SQL, giving DuckDB's own runtime errors BigQuery's wording. The request's
    parameters it references are bound by ExecContext.run_sql."""

    def run(c: ExecContext) -> Result:
        try:
            result = c.run_sql(sql, returns_rows=returns_rows)
        except CrucibleError as exc:
            raise errors.reword(exc) from exc
        result.statement_type = statement_type
        return result

    return CommandPlan(run, statement_type, columns_sql=sql if returns_rows else None)


def _insert(tree: exp.Insert, ctx: ExecContext) -> Plan:
    target = tree.this
    table = target.this if isinstance(target, exp.Schema) else target
    assert isinstance(table, exp.Table)
    info = metadata.find_table(ctx, metadata.name_of(table, ctx))
    by_name = {c.name.lower(): c for c in info.columns}
    if isinstance(target, exp.Schema):
        try:
            columns = [by_name[c.name.lower()] for c in target.expressions]
        except KeyError as exc:
            raise errors.invalid(
                f"Column {exc.args[0]} is not present in table {info.name}"
            ) from exc
    else:
        columns = list(info.columns)
    target_types = [types.parse(types.type_name(c.type)) for c in columns]

    source = tree.expression
    if isinstance(source, exp.Values):
        source = _values_as_select(source)
    if not isinstance(source, exp.Query):
        raise CrucibleUnsupportedError(f"INSERT source {source.sql('bigquery')}")
    _check_assignable(
        source, columns_names=[c.name for c in columns], target_types=target_types, ctx=ctx
    )

    translation = translate_query(source, ctx, target_types=target_types)
    names = ", ".join(qualified(c.name) for c in columns)
    full = qualified(info.catalog, info.schema, info.name)
    return run_sql(
        f"INSERT INTO {full} ({names}) {translation.sql}", "INSERT", ctx, returns_rows=False
    )


def _values_as_select(values: exp.Values) -> exp.Query:
    rows = [row.expressions if isinstance(row, exp.Tuple) else [row] for row in values.expressions]
    query: exp.Query = exp.select(*rows[0])
    for row in rows[1:]:
        query = exp.union(query, exp.select(*row), distinct=False)
    return query


def _check_assignable(
    source: exp.Query, columns_names: list[str], target_types: list[exp.DataType], ctx: ExecContext
) -> None:
    """BigQuery refuses to INSERT a value whose type does not coerce to the column type."""
    probe = source.copy()
    infos = metadata.qualify_names(probe, ctx)
    annotate_types(probe, schema=schema_for(infos), dialect="bigquery")
    fix_annotation(probe)
    for select in outer_selects(probe):
        if len(select.expressions) != len(target_types):
            raise errors.invalid(
                f"Inserted row has wrong column count; Has {len(select.expressions)}, "
                f"expected {len(target_types)}"
            )
        for index, (projection, target) in enumerate(
            zip(select.expressions, target_types, strict=True)
        ):
            value = projection.this if isinstance(projection, exp.Alias) else projection
            source_type = value.type
            # A literal NULL is assignable to any column (SQLGlot types a bare NULL as INT64).
            if isinstance(value, exp.Null) or source_type is None:
                continue
            if source_type.this == exp.DataType.Type.UNKNOWN:
                continue
            literal_string = isinstance(value, exp.Literal) and value.is_string
            if not types.coerces(source_type, target, literal_string=literal_string):
                raise errors.invalid(
                    f"Query column {index + 1} has type {types.type_name(source_type)} "
                    f"which cannot be inserted into column {columns_names[index]}, which has type "
                    f"{types.type_name(target)}"
                )
