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

import itertools
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
from crucible.engine.errors import Location

TEMP_STATE = "temp_functions"  # session.state: lower name -> definition
ROUTINES = "routines"  # dataset properties: lower name -> definition
_CHECK = "crucible_udf_check"  # Cast.meta: (name, definition, call number, argument, location)
_START = "crucible_udf_start"  # Dot.meta: where a qualified call's name starts
_MAX_DEPTH = 16
_CALLS = itertools.count()  # tells apart the argument casts of separate calls


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
    if isinstance(body, exp.Paren):
        body = body.this  # AS (x / 2) is stored as "x / 2" (routines_get_function)
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
            return Result("CREATE_FUNCTION", info={"temporary_function": True})

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
        return Result("CREATE_FUNCTION", info={"routine": (project, dataset, target.name)})

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
            stored: dict[str, Any] = (
                ctx.catalog.properties(project, dataset, "").get(ROUTINES) or {}
            )
            if call.name.lower() in stored:
                # A persistent function needs its dataset even in the default dataset
                # (recorded: udf/persistent_function_unqualified_not_found).
                raise errors.invalid_at(call, f"Function not found: {call.name}")
        return (call.name, definition, list(call.expressions)) if definition else None
    if isinstance(call, exp.Dot) and isinstance(call.expression, exp.Anonymous):
        parts: list[str] = []
        node: exp.Expr = call.this
        while isinstance(node, exp.Dot):
            parts[:0] = node.expression.name.split(".")
            node = node.this
        parts[:0] = node.name.split(".")  # `p.ds`.f(...) quotes both parts in one identifier
        call.meta[_START] = errors.location_of(node)  # BigQuery reports the name's start
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
        # Locations inside a body would point into the body's text, not the request.
        location = (node.meta.get(_START) or errors.location_of(node)) if depth == 0 else None
        if len(args) != len(params):
            raise _no_signature(name, definition, None, location)
        call = next(_CALLS)
        bound: dict[str, exp.Expr] = {}
        for index, (param, arg) in enumerate(zip(params, args, strict=True)):
            kind = exp.DataType.build(param["type"], dialect="bigquery")
            cast = exp.Cast(this=arg.copy(), to=kind)
            cast.meta[_CHECK] = (name, definition, call, index, location)
            bound[param["name"].lower()] = cast
        # Parenthesised, so a body that is only a parameter (`AS (x)`) has a parent to replace in.
        body = sqlglot.parse_one(f"({definition['body']})", read="bigquery")
        for column in list(body.find_all(exp.Column)):
            if not column.table and column.name.lower() in bound:
                column.replace(bound[column.name.lower()].copy())
        body = expand(body, ctx, depth + 1)
        if definition["return_type"]:
            ret = exp.Cast(
                this=body, to=exp.DataType.build(definition["return_type"], dialect="bigquery")
            )
            ret.meta[_CHECK] = (name, definition | {"returns": True}, call, 0, location)
            return exp.Paren(this=ret)
        return body

    return tree.transform(replace, copy=False)


def check(query: exp.Expr) -> None:
    """After annotation: each argument must coerce to its parameter's type, the body to RETURNS."""
    calls: dict[tuple[int, bool], list[exp.Cast]] = {}
    for cast in query.find_all(exp.Cast):
        marked = cast.meta.get(_CHECK)
        if marked is not None:
            calls.setdefault((marked[2], bool(marked[1].get("returns"))), []).append(cast)
    for casts in calls.values():
        casts.sort(key=lambda c: c.meta[_CHECK][3])
        name, definition, _, _, location = casts[0].meta[_CHECK]
        given = [c.this.type for c in casts]
        for cast, source in zip(casts, given, strict=True):
            if source is None or source.this in (exp.DType.UNKNOWN, exp.DType.NULL):
                continue
            literal = isinstance(cast.this, exp.Literal) and cast.this.is_string
            if types.coerces(source, cast.to, literal_string=literal):
                continue
            if definition.get("returns"):
                raise errors.invalid(
                    f"Function {name} declared to return {types.type_name(cast.to)} but the "
                    f"function body produces incompatible type {types.type_name(source)}"
                )
            index = cast.meta[_CHECK][3]
            shown = [types.type_name(t) if t is not None else "NULL" for t in given]
            mismatch = (
                f"Argument {index + 1}: Unable to coerce type {types.type_name(source)} to "
                f"expected type {types.type_name(cast.to)}"
            )
            raise _no_signature(name, definition, (shown, mismatch), location)


def _no_signature(
    name: str,
    definition: dict[str, Any],
    given: tuple[list[str], str] | None,
    location: Location | None,
) -> CrucibleError:
    """Recorded (udf/temp_function_signature_error): "No matching signature for function f\n
    Argument types: STRING\n  Signature: f(INT64)\n    Argument 1: Unable to coerce type STRING to
    expected type INT64 at [1:48]". The wrong-number-of-arguments detail is not recorded."""
    supported = ", ".join(a["type"] for a in definition["arguments"])
    shown, detail = given if given else (["(wrong number of arguments)"], "")
    message = (
        f"No matching signature for function {name}\n  Argument types: {', '.join(shown)}\n"
        f"  Signature: {name}({supported})"
    )
    if detail:
        message += f"\n    {detail}"
    return CrucibleError(ErrorKind.INVALID_QUERY, message, location=location)
