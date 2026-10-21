"""MERGE, UPDATE, DELETE and TRUNCATE TABLE, with BigQuery's duplicate-match error (ADR 0011).

The ON condition, WHEN conditions and every assigned value are translated together, as the
projections of one synthetic query over `<target> AS t CROSS JOIN <source> AS s`, so they are
qualified, typed and transformed exactly like any query (ADR 0010). Each assigned value is then
coerced and written like an INSERT value: BigQuery's coercion check, arrays finalised, and a cast
to the column's storage type (ADR 0014).

BigQuery raises "UPDATE/MERGE must match at most one source row for each target row" when a
target row matches several source rows and the statement has a WHEN MATCHED clause; DuckDB
applies one of them silently. A check query runs first, in the same transaction as the MERGE.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from crucible.dialects.bigquery import errors, metadata, types
from crucible.dialects.bigquery.generator import generate
from crucible.dialects.bigquery.translate import outer_selects, prepare, write_value
from crucible.engine import (
    CommandPlan,
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    Plan,
    Result,
    TableInfo,
)
from crucible.engine.catalog import qualified

DUPLICATE_MATCH = "UPDATE/MERGE must match at most one source row for each target row"


@dataclass
class _Clause:
    when: exp.When
    condition: int | None  # index of the WHEN condition among the synthetic projections
    assignments: list[tuple[str, int]]  # (target column, projection index)


def _alias(node: exp.Expr, default: str) -> str:
    alias = node.args.get("alias")
    return alias.name if isinstance(alias, exp.TableAlias) and alias.name else default


def plan_merge(tree: exp.Merge, ctx: ExecContext) -> Plan:
    target = tree.this
    assert isinstance(target, exp.Table)
    info = metadata.find_table(ctx, metadata.name_of(target, ctx))
    target_alias = _alias(target, info.name)
    source = tree.args["using"]
    # An unaliased source table is named by its table name, as in BigQuery: USING s ON t.k = s.k.
    default = source.name if isinstance(source, exp.Table) else "crucible_source"
    source_alias = _alias(source, default)
    by_name = {c.name.lower(): c for c in info.columns}
    target_types = {c.name: types.parse(types.type_name(c.type)) for c in info.columns}

    def scoped(expression: exp.Expr, alias: str) -> exp.Expr:
        """Unqualified columns in a NOT MATCHED clause name the only row there is."""
        out = expression.copy()
        for column in out.find_all(exp.Column):
            if not column.table:
                column.set("table", exp.to_identifier(alias))
        return out

    projections: list[exp.Expr] = [tree.args["on"].copy()]
    clauses: list[_Clause] = []
    has_matched = False
    whens = tree.args["whens"].expressions
    for when in whens:
        assert isinstance(when, exp.When)
        condition = None
        # NOT MATCHED (by target): only the source row exists; BY SOURCE: only the target row.
        only = None
        if not when.args.get("matched"):
            only = target_alias if when.args.get("source") else source_alias
        if when.args.get("condition") is not None:
            cond = when.args["condition"]
            projections.append(scoped(cond, only) if only else cond.copy())
            condition = len(projections) - 1
        then = when.args["then"]
        assignments: list[tuple[str, int]] = []
        if when.args.get("matched"):
            has_matched = True
        if isinstance(then, exp.Update):
            for eq in then.expressions:
                column = by_name.get(eq.this.name.lower())
                if column is None:
                    raise errors.invalid(
                        f"Column {eq.this.name} is not present in table {info.name}"
                    )
                projections.append(eq.expression.copy())
                assignments.append((column.name, len(projections) - 1))
        elif isinstance(then, exp.Insert):
            columns = (
                [c.name for c in then.this.expressions]
                if then.this
                else [c.name for c in info.columns]
            )
            values: list[exp.Expr] = list(then.expression.expressions) if then.expression else []
            if len(columns) != len(values):
                raise errors.invalid("INSERT column list and VALUES have different lengths")
            for name, value in zip(columns, values, strict=True):
                column = by_name.get(name.lower())
                if column is None:
                    raise errors.invalid(f"Column {name} is not present in table {info.name}")
                projections.append(scoped(value, only) if only else value.copy())
                assignments.append((column.name, len(projections) - 1))
        elif not (isinstance(then, exp.Var) and then.name.upper() == "DELETE"):
            raise CrucibleUnsupportedError(f"MERGE action {then.sql('bigquery')}")
        clauses.append(_Clause(when, condition, assignments))

    # One query types and translates every expression against both relations.
    target_ref = exp.Table(
        this=exp.to_identifier(info.name, quoted=True),
        db=exp.to_identifier(info.schema, quoted=True),
        catalog=exp.to_identifier(info.catalog, quoted=True),
        alias=exp.TableAlias(this=exp.to_identifier(target_alias)),
    )
    source_ref = source.copy()
    source_ref.set("alias", exp.TableAlias(this=exp.to_identifier(source_alias)))
    synthetic = exp.select(*(exp.alias_(p, f"crucible_m{i}") for i, p in enumerate(projections)))
    synthetic = synthetic.from_(target_ref).join(source_ref, join_type="cross")
    prepared = prepare(synthetic, ctx)
    [select] = outer_selects(prepared.query)
    values = [p.this if isinstance(p, exp.Alias) else p for p in select.expressions]
    joined = select.args["joins"][0].this
    source_sql = generate(joined.this if isinstance(joined, exp.Subquery) else joined)
    if not isinstance(joined, exp.Subquery):
        source_sql = f"SELECT * FROM {source_sql}"

    def written(column: str, index: int) -> str:
        value = values[index]
        target_type = target_types[column]
        if (
            not isinstance(value, exp.Null)
            and value.type is not None
            and value.type.this != exp.DType.UNKNOWN
        ):
            literal = isinstance(value, exp.Literal) and value.is_string
            if not types.coerces(value.type, target_type, literal_string=literal):
                raise errors.invalid(
                    f"Value of type {types.type_name(value.type)} cannot be assigned to "
                    f"{column}, which has type {types.type_name(target_type)}"
                )
        return generate(write_value(value.copy(), target_type, column))

    on_sql = generate(values[0])
    target_sql = f"{qualified(info.catalog, info.schema, info.name)} AS {qualified(target_alias)}"
    parts = [
        f"MERGE INTO {target_sql}",
        f"USING ({source_sql}) AS {qualified(source_alias)} ON {on_sql}",
    ]
    for clause in clauses:
        when = clause.when
        matched = "MATCHED" if when.args.get("matched") else "NOT MATCHED"
        if when.args.get("source"):
            matched += " BY SOURCE"
        head = f"WHEN {matched}"
        if clause.condition is not None:
            head += f" AND {generate(values[clause.condition])}"
        then = when.args["then"]
        if isinstance(then, exp.Update):
            sets = ", ".join(f"{qualified(c)} = {written(c, i)}" for c, i in clause.assignments)
            parts.append(f"{head} THEN UPDATE SET {sets}")
        elif isinstance(then, exp.Insert):
            names = ", ".join(qualified(c) for c, _ in clause.assignments)
            vals = ", ".join(written(c, i) for c, i in clause.assignments)
            parts.append(f"{head} THEN INSERT ({names}) VALUES ({vals})")
        else:
            parts.append(f"{head} THEN DELETE")
    merge_sql = "\n".join(parts)
    check_sql = None
    if has_matched:
        check_sql = (
            f"SELECT 1 FROM {target_sql} "
            f"JOIN ({source_sql}) AS {qualified(source_alias)} ON {on_sql} "
            f"GROUP BY {qualified(target_alias)}.rowid HAVING count(*) > 1 LIMIT 1"
        )
    return CommandPlan(_runner(merge_sql, check_sql, info), "MERGE")


def _runner(merge_sql: str, check_sql: str | None, info: TableInfo):
    def run(c: ExecContext) -> Result:
        own_transaction = c.session.transaction is None and check_sql is not None
        try:
            if own_transaction:
                c.run_sql("BEGIN TRANSACTION", returns_rows=False)
            if check_sql is not None and c.run_sql(check_sql).rows:
                raise CrucibleError(ErrorKind.RUNTIME, DUPLICATE_MATCH)
            result = c.run_sql(merge_sql, returns_rows=False)
            if own_transaction:
                c.run_sql("COMMIT", returns_rows=False)
        except CrucibleError as exc:
            if own_transaction:
                c.run_sql("ROLLBACK", returns_rows=False)
            raise errors.reword(exc) from exc
        result.statement_type = "MERGE"
        return result

    return run


# UPDATE and DELETE run as a MERGE over one dummy row (or UPDATE's FROM source), so they share
# its translation, coercion and duplicate-match check. BigQuery requires the WHERE clause:
# https://cloud.google.com/bigquery/docs/reference/standard-sql/dml-syntax#update_statement
_ONE_ROW = exp.Subquery(this=exp.select(exp.alias_(exp.Literal.number(1), "crucible_row")))


def _dml_target(tree: exp.Update | exp.Delete, ctx: ExecContext, statement: str) -> exp.Table:
    target = tree.this
    if not isinstance(target, exp.Table):
        target = (tree.args.get("tables") or [None])[0]  # `DELETE ds.t WHERE ...`
    if not isinstance(target, exp.Table):
        raise CrucibleUnsupportedError(f"{statement} target {tree.sql('bigquery')}")
    if tree.args.get("where") is None:
        # Recorded: jobs_query_update_without_where / jobs_query_delete_without_where.
        raise errors.invalid_at_statement(tree, f"{statement} must have a WHERE clause")
    info = metadata.find_table(ctx, metadata.name_of(target, ctx))
    if info.kind != "table":
        raise CrucibleUnsupportedError(f"{statement} of a view")
    if info.properties.get(metadata.STREAMING_BUFFER):
        # Recorded (jobs_query_update_streamed): the table as the statement names it.
        written = ".".join(p for p in (target.catalog, target.db, target.name) if p)
        raise errors.invalid(
            f"UPDATE or DELETE statement over table {written} would affect rows in the "
            "streaming buffer, which is not supported"
        )
    return target


def _as(merge: exp.Merge, ctx: ExecContext, statement: str) -> Plan:
    planned = plan_merge(merge, ctx)
    assert isinstance(planned, CommandPlan)
    run = planned.run

    def renamed(c: ExecContext) -> Result:
        result = run(c)
        result.statement_type = statement
        return result

    return CommandPlan(renamed, statement)


def plan_update(tree: exp.Update, ctx: ExecContext) -> Plan:
    target = _dml_target(tree, ctx, "UPDATE")
    from_ = tree.args.get("from_")
    if tree.args.get("joins") or (from_ is not None and from_.args.get("joins")):
        raise CrucibleUnsupportedError("UPDATE ... FROM with a join")
    source = from_.this.copy() if from_ is not None else _ONE_ROW.copy()
    merge = exp.Merge(
        this=target.copy(),
        using=source,
        on=tree.args["where"].this.copy(),
        whens=exp.Whens(
            expressions=[
                exp.When(
                    matched=True,
                    then=exp.Update(expressions=[e.copy() for e in tree.expressions]),
                )
            ]
        ),
    )
    return _as(merge, ctx, "UPDATE")


def plan_delete(tree: exp.Delete, ctx: ExecContext) -> Plan:
    target = _dml_target(tree, ctx, "DELETE")
    if tree.args.get("using") is True or isinstance(tree.args.get("using"), exp.Expr):
        raise CrucibleUnsupportedError("DELETE ... USING")
    merge = exp.Merge(
        this=target.copy(),
        using=_ONE_ROW.copy(),
        on=tree.args["where"].this.copy(),
        whens=exp.Whens(expressions=[exp.When(matched=True, then=exp.var("DELETE"))]),
    )
    return _as(merge, ctx, "DELETE")


def plan_truncate(tree: exp.TruncateTable, ctx: ExecContext) -> Plan:
    """TRUNCATE TABLE deletes every row; the table, its schema and its properties stay."""
    [target] = tree.expressions
    assert isinstance(target, exp.Table)
    info = metadata.find_table(ctx, metadata.name_of(target, ctx))
    if info.kind != "table":
        raise CrucibleUnsupportedError("TRUNCATE TABLE of a view")
    sql = f"DELETE FROM {qualified(info.catalog, info.schema, info.name)}"

    def run(c: ExecContext) -> Result:
        # Recorded (jobs_get_truncate): numDmlAffectedRows and deletedRowCount are the rows removed.
        result = c.run_sql(sql, returns_rows=False)
        props = c.catalog.properties(info.catalog, info.schema, info.name)
        if props.pop(metadata.STREAMING_BUFFER, None):
            c.catalog.set_properties(info.catalog, info.schema, info.name, props)
        result.statement_type = "TRUNCATE_TABLE"
        result.info["target"] = (info.catalog, info.schema, info.name)
        return result

    return CommandPlan(run, "TRUNCATE_TABLE")
