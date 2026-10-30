"""Helpers every transform uses to build DuckDB-bound AST nodes.

Raising: generated SQL raises through DuckDB's `error()` with a prefix the engine recognises
(engine/errors.py), so the client sees the warehouse's error kind and message (ADR 0011), or an
explicit unsupported error (ADR 0009) for a value Crucible cannot handle exactly.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlglot import exp

from crucible.engine.errors import RUNTIME_ERROR_PREFIX, UNSUPPORTED_ERROR_PREFIX

T = exp.DType
# Calls whose value differs between two evaluations, so a check must not evaluate them twice.
VOLATILE = re.compile(r"\b(rand|generate_uuid)\s*\(", re.IGNORECASE)


def typed(node: exp.Expr, data_type: exp.DataType | str) -> exp.Expr:
    """Set a node's BigQuery type (later transforms and the projection casts read it)."""
    node.type = (
        data_type
        if isinstance(data_type, exp.DataType)
        else exp.DataType.build(data_type, dialect="bigquery")
    )
    return node


def duck_cast(value: exp.Expr, duckdb_type: str) -> exp.Cast:
    """A CAST to a DuckDB type, exempt from the BigQuery storage mapping (translate.py)."""
    cast = exp.Cast(this=value, to=exp.DataType.build(duckdb_type, dialect="duckdb"))
    cast.meta["crucible_storage"] = True
    return cast


def numeric_text(value: exp.Expr) -> exp.Expr:
    """A NUMERIC as BigQuery writes it: no trailing zeros, no trailing point ('4.1', '4', '-0.5';
    recorded: json/to_json_string_numeric*). DuckDB writes a DECIMAL with its own scale, which
    can be 0 (ROUND gives DECIMAL(38,0)), so the value is first rescaled to NUMERIC's 9 places."""
    written = duck_cast(duck_cast(value, "DECIMAL(38,9)"), "VARCHAR")
    trimmed = exp.Anonymous(this="rtrim", expressions=[written, exp.Literal.string("0")])
    return typed(
        exp.Anonymous(this="rtrim", expressions=[trimmed, exp.Literal.string(".")]), "STRING"
    )


def text(*parts: exp.Expr | str) -> exp.Expr:
    """A VARCHAR concatenation of literal strings and expressions (cast to VARCHAR)."""
    pieces: list[exp.Expr] = [
        exp.Literal.string(p) if isinstance(p, str) else duck_cast(p, "VARCHAR") for p in parts
    ]
    out = pieces[0]
    for piece in pieces[1:]:
        out = exp.DPipe(this=out, expression=piece, safe=True)
    return out


def raise_runtime(*message: exp.Expr | str) -> exp.Expr:
    """`error(...)`: raises the warehouse's runtime error with this message."""
    return exp.Anonymous(this="error", expressions=[text(RUNTIME_ERROR_PREFIX, *message)])


def raise_unsupported(construct: str) -> exp.Expr:
    """`error(...)`: raises CrucibleUnsupportedError at run time, naming the construct."""
    return exp.Anonymous(
        this="error", expressions=[exp.Literal.string(UNSUPPORTED_ERROR_PREFIX + construct)]
    )


def check(condition: exp.Expr, failure: exp.Expr, value: exp.Expr) -> exp.Expr:
    """CASE WHEN condition THEN failure ELSE value END, typed like `value`."""
    case = exp.Case(ifs=[exp.If(this=condition, true=failure)], default=value)
    if value.type is not None:
        case.type = value.type
    return case


def rewrite_bottom_up(tree: exp.Expr, rewrite: Callable[[exp.Expr], exp.Expr]) -> exp.Expr:
    """Apply `rewrite` to every node, children before parents.

    SQLGlot's `Expr.transform` does not visit the children of a node it replaces, so a check that
    wraps its node (CASE WHEN ... ELSE <node> END) would leave nested cases unchecked:
    `(1 / 0) / 2` must raise. Each node's slot in its parent is captured before `rewrite` runs,
    because a rewrite that wraps the node re-parents it. Nodes the rewrite creates are not visited.
    """
    for node in reversed(list(tree.walk())):
        parent, key, index = node.parent, node.arg_key, node.index
        new = rewrite(node)
        if new is node:
            continue
        if parent is None or key is None:
            tree = new
        else:
            parent.set(key, new, index)
    return tree
