"""SQL user-defined functions: CREATE TEMP FUNCTION, CREATE [OR REPLACE] FUNCTION, DROP FUNCTION.

A call is expanded into the function's body before translation, each parameter replaced by its
argument cast to the parameter's type, so the body is qualified, typed and transformed like any
other expression (rather than stored as a DuckDB macro, whose body would skip the BigQuery
transforms). After type annotation every argument is checked against its parameter, and the body
against RETURNS, with BigQuery's "No matching signature" error
(https://cloud.google.com/bigquery/docs/user-defined-functions). Temporary functions live for the
request; persistent ones are stored in their dataset's properties and listed by the routines API.
JavaScript, remote, table-valued and templated (ANY TYPE) functions are refused.
"""

from __future__ import annotations

import time
from typing import Any

import sqlglot
from sqlglot import exp

from crucible.dialects.bigquery import errors, metadata, types
from crucible.engine import (
    CommandPlan,
    CrucibleError,
    CrucibleUnsupportedError,
    ErrorKind,
    ExecContext,
    Plan,
    Result,
)

TEMP_STATE = "temp_functions"  # session.state: lower name -> definition
ROUTINES = "routines"  # dataset properties: lower name -> definition
_CHECK = "crucible_udf_check"  # Cast.meta: (function name, "argument" | "return")
_MAX_DEPTH = 16


def _definition(tree: exp.Create) -> dict[str, Any]:
    udf = tree.this
    assert isinstance(udf, exp.UserDefinedFunction)
    props = tree.args.get("properties")
    returns: str | None = None
    for prop in props.expressions if isinstance(props, exp.Properties) else []:
        if isinstance(prop, exp.LanguageProperty):
            raise CrucibleUnsupportedError(f"{prop.name.upper()} user-defined functions")
        if isinstance(prop, exp.ReturnsProperty):
            if not isinstance(prop.this, exp.DataType):
                raise CrucibleUnsupportedError("table-valued functions")
            returns = prop.this.sql("bigquery")
        elif not isinstance(prop, exp.TemporaryProperty):
            raise CrucibleUnsupportedError(f"function property {prop.sql('bigquery')}")
    body = tree.expression
    if not isinstance(body, exp.Expr) or isinstance(body, exp.Literal):
        raise CrucibleUnsupportedError("a function body that is not a SQL expression")
    arguments: list[dict[str, str]] = []
    for param in udf.expressions:
        assert isinstance(param, exp.ColumnDef)
        kind = param.args.get("kind")
        if not isinstance(kind, exp.DataType) or kind.this == exp.DType.VARIANT:
            raise CrucibleUnsupportedError("templated (ANY TYPE) function parameters")
        arguments.append({"name": param.name, "type": kind.sql("bigquery")})
    target = udf.this
    assert isinstance(target, exp.Table)
    return {
        "name": target.name,
        "arguments": arguments,
        "return_type": returns,
        "body": body.sql("bigquery"),
        "created_ms": int(time.time() * 1000),
    }


def plan_create(tree: exp.Create, ctx: ExecContext) -> Plan:
    definition = _definition(tree)
    target = tree.this.this
    assert isinstance(target, exp.Table)
    props = tree.args.get("properties")
    listed: list[exp.Expr] = props.expressions if isinstance(props, exp.Properties) else []
    temporary = any(isinstance(p, exp.TemporaryProperty) for p in listed)
    if temporary:
        if target.db:
            raise errors.invalid("A temporary function name cannot be qualified")

        def run_temp(c: ExecContext) -> Result:
            functions: dict[str, Any] = c.session.state.setdefault(TEMP_STATE, {})
            functions[target.name.lower()] = definition
            return Result("CREATE_FUNCTION")

        return CommandPlan(run_temp, "CREATE_FUNCTION")
    project = target.catalog or ctx.session.default_catalog
    dataset = target.db or ctx.session.default_schema
    if not project or not dataset:
        raise errors.invalid(f"Function {target.name} must be qualified with a dataset")
    metadata.check_dataset(ctx, project, dataset)

    def run(c: ExecContext) -> Result:
        props = c.catalog.properties(project, dataset, "")
        routines: dict[str, Any] = dict(props.get(ROUTINES) or {})
        key = target.name.lower()
        if key in routines:
            if tree.args.get("exists"):
                return Result("CREATE_FUNCTION")
            if not tree.args.get("replace"):
                raise CrucibleError(
                    ErrorKind.ALREADY_EXISTS,
                    f"Already Exists: Function {project}:{dataset}.{target.name}",
                )
        routines[key] = definition
        c.catalog.set_properties(project, dataset, "", props | {ROUTINES: routines})
        return Result("CREATE_FUNCTION")

    return CommandPlan(run, "CREATE_FUNCTION")


def plan_drop(tree: exp.Drop, ctx: ExecContext) -> Plan:
    [target] = tree.expressions if tree.expressions else tree.args.get("tables") or [None]
    if not isinstance(target, exp.Table):
        raise CrucibleUnsupportedError(f"DROP FUNCTION {tree.sql('bigquery')}")
    project = target.catalog or ctx.session.default_catalog
    dataset = target.db or ctx.session.default_schema
    if not project or not dataset:
        raise errors.invalid(f"Function {target.name} must be qualified with a dataset")

    def run(c: ExecContext) -> Result:
        metadata.check_dataset(c, project, dataset)
        props = c.catalog.properties(project, dataset, "")
        routines: dict[str, Any] = dict(props.get(ROUTINES) or {})
        if routines.pop(target.name.lower(), None) is None and not tree.args.get("exists"):
            raise CrucibleError(
                ErrorKind.NOT_FOUND, f"Not found: Function {project}:{dataset}.{target.name}"
            )
        c.catalog.set_properties(project, dataset, "", props | {ROUTINES: routines})
        return Result("DROP_FUNCTION")

    return CommandPlan(run, "DROP_FUNCTION")


def routines(ctx: ExecContext, project: str, dataset: str) -> list[dict[str, Any]]:
    """The persistent functions of a dataset, in creation order."""
    stored: dict[str, Any] = ctx.catalog.properties(project, dataset, "").get(ROUTINES) or {}
    return sorted(stored.values(), key=lambda d: (d["created_ms"], d["name"]))


def _lookup(call: exp.Expr, ctx: ExecContext) -> tuple[str, dict[str, Any], list[exp.Expr]] | None:
    """A call to a user-defined function: (name as written, definition, arguments), else None."""
    if isinstance(call, exp.Anonymous) and not isinstance(call.parent, exp.Dot):
        temp: dict[str, Any] = ctx.session.state.get(TEMP_STATE) or {}
        definition = temp.get(call.name.lower())
        project, dataset = ctx.session.default_catalog, ctx.session.default_schema
        if definition is None and project and dataset and dataset in ctx.catalog.schemas(project):
            # shortcut: an unqualified name falls back to the default dataset's functions; the
            # corpus case udf/persistent_function_over_table settles it when recorded.
            stored: dict[str, Any] = (
                ctx.catalog.properties(project, dataset, "").get(ROUTINES) or {}
            )
            definition = stored.get(call.name.lower())
        return (call.name, definition, list(call.expressions)) if definition else None
    if isinstance(call, exp.Dot) and isinstance(call.expression, exp.Anonymous):
        parts: list[str] = []
        node: exp.Expr = call.this
        while isinstance(node, exp.Dot):
            parts[:0] = node.expression.name.split(".")
            node = node.this
        parts[:0] = node.name.split(".")  # `p.ds`.f(...) quotes both parts in one identifier
        if len(parts) not in (1, 2):
            return None
        dataset = parts[-1]
        project = parts[0] if len(parts) == 2 else ctx.session.default_catalog
        function = call.expression
        written = ".".join([*parts, function.name])
        if not project or dataset not in ctx.catalog.schemas(project):
            return None
        stored: dict[str, Any] = ctx.catalog.properties(project, dataset, "").get(ROUTINES) or {}
        definition = stored.get(function.name.lower())
        if definition is None:
            raise errors.invalid(f"Function not found: {written}")
        return (written, definition, list(function.expressions))
    return None


def expand(tree: exp.Expr, ctx: ExecContext, depth: int = 0) -> exp.Expr:
    """Replace every user-defined function call in `tree` with its body."""
    if depth > _MAX_DEPTH:
        raise CrucibleUnsupportedError("user-defined functions nested more than 16 deep")

    def replace(node: exp.Expr) -> exp.Expr:
        found = _lookup(node, ctx)
        if found is None:
            return node
        name, definition, args = found
        params: list[dict[str, str]] = definition["arguments"]
        if len(args) != len(params):
            raise _no_signature(name, definition, [])
        bound: dict[str, exp.Expr] = {}
        for param, arg in zip(params, args, strict=True):
            kind = exp.DataType.build(param["type"], dialect="bigquery")
            cast = exp.Cast(this=arg.copy(), to=kind)
            cast.meta[_CHECK] = (name, definition)
            bound[param["name"].lower()] = cast
        body = sqlglot.parse_one(definition["body"], read="bigquery")
        for column in list(body.find_all(exp.Column)):
            if not column.table and column.name.lower() in bound:
                column.replace(bound[column.name.lower()].copy())
        body = expand(body, ctx, depth + 1)
        if definition["return_type"]:
            ret = exp.Cast(
                this=body, to=exp.DataType.build(definition["return_type"], dialect="bigquery")
            )
            ret.meta[_CHECK] = (name, definition | {"returns": True})
            return exp.Paren(this=ret)
        return exp.Paren(this=body)

    return tree.transform(replace, copy=False)


def check(query: exp.Expr) -> None:
    """After annotation: each argument must coerce to its parameter's type, the body to RETURNS."""
    for cast in query.find_all(exp.Cast):
        marked = cast.meta.get(_CHECK)
        if marked is None:
            continue
        name, definition = marked
        source = cast.this.type
        if source is None or source.this in (exp.DType.UNKNOWN, exp.DType.NULL):
            continue
        literal = isinstance(cast.this, exp.Literal) and cast.this.is_string
        if types.coerces(source, cast.to, literal_string=literal):
            continue
        if definition.get("returns"):
            raise errors.invalid(
                f"Function {name} declared to return {types.type_name(cast.to)} but the function "
                f"body produces incompatible type {types.type_name(source)}"
            )
        raise _no_signature(name, definition, [types.type_name(source)])


def _no_signature(name: str, definition: dict[str, Any], given: list[str]) -> CrucibleError:
    supported = ", ".join(a["type"] for a in definition["arguments"])
    shown = ", ".join(given) if given else "(wrong number of arguments)"
    return errors.invalid(
        f"No matching signature for function {name} for argument types: {shown}. "
        f"Supported signature: {name}({supported})"
    )
