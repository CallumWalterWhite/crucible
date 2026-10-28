"""NET.REG_DOMAIN: the public suffix of a URL's host plus one preceding label.

BigQuery's rules (https://cloud.google.com/bigquery/docs/reference/standard-sql/net_functions):
- The host comes from a best-effort URL parse: surrounding whitespace is ignored, and a scheme
  (`x://`) or `//`, userinfo (`user@`), a port, and a path, query or fragment are dropped.
- Only the ICANN section of the Public Suffix List counts; a private-section suffix is an ordinary
  label (`foo.us.com` gives `us.com`). There is no implicit `*` rule, so a host whose last label is
  not listed has no suffix (`a.b` gives NULL).
- The host is lowercased and non-ASCII labels are Punycode-encoded for the lookup; the result keeps
  the input's spelling.
- NULL for an empty host, an IPv6 literal, or a host that is only a suffix.
- Labels before the result are not checked: `a..b.com`, `.example.com` and `my_host.example.com`
  give `b.com`, `example.com` and `example.com`. One trailing dot is kept: `example.com.` gives
  `example.com.` (recorded: net/reg_domain_invalid_and_null,
  net/reg_domain_trailing_and_leading_dot, net/reg_domain_underscore_in_host).

The list is vendored at a pinned commit (data/README.md). BigQuery's own snapshot may differ for
suffixes added or removed since. A result, or a host with no suffix, that holds an empty label or
characters other than letters, digits and hyphens is not recorded, so it is refused.

Runs as a DuckDB Python function (`FUNCTION`), registered on the engine on first use.
"""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files

from crucible.engine import CrucibleUnsupportedError
from crucible.engine.errors import UNSUPPORTED_ERROR_PREFIX

FUNCTION = "crucible_net_reg_domain"

_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_ASCII_LABEL = re.compile(r"[A-Za-z0-9-]+")


@cache
def _rules() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """The ICANN rules in lookup form: (normal, wildcard parents, exceptions)."""
    text = files("crucible.dialects.bigquery").joinpath("data/public_suffix_list.dat")
    lines = text.read_text(encoding="utf-8").splitlines()
    start = lines.index("// ===BEGIN ICANN DOMAINS===")
    end = lines.index("// ===END ICANN DOMAINS===")
    normal: set[str] = set()
    wildcard: set[str] = set()
    exception: set[str] = set()
    for line in lines[start + 1 : end]:
        rule = line.strip()
        if not rule or rule.startswith("//"):
            continue
        if rule.startswith("!"):
            exception.add(_key(rule[1:].split(".")))
        elif rule.startswith("*."):
            wildcard.add(_key(rule[2:].split(".")))
        else:
            normal.add(_key(rule.split(".")))
    return frozenset(normal), frozenset(wildcard), frozenset(exception)


def _key(labels: list[str]) -> str:
    return ".".join(
        label.lower() if label.isascii() else "xn--" + label.lower().encode("punycode").decode()
        for label in labels
    )


def host(url: str) -> str:
    text = url.strip()
    scheme = _SCHEME.match(text)
    if scheme:
        text = text[scheme.end() :]
    elif text.startswith("//"):
        text = text[2:]
    text = re.split(r"[/?#]", text, maxsplit=1)[0]
    text = text.rsplit("@", 1)[-1]
    if text.startswith("["):
        return text  # an IPv6 literal keeps its brackets
    return text.split(":", 1)[0]


def reg_domain(url: str) -> str | None:
    name = host(url)
    if not name or name.startswith("["):
        return None
    dot = "." if name.endswith(".") else ""
    labels = name.removesuffix(".").split(".")
    suffix = _suffix_length(labels)
    checked = labels if suffix is None else labels[-(suffix + 1) :]
    for label in checked:
        if label == "" or (label.isascii() and not _ASCII_LABEL.fullmatch(label)):
            raise CrucibleUnsupportedError(
                "NET.REG_DOMAIN of a host with an empty label or characters other than letters, "
                "digits and hyphens"
            )
    if suffix is None or suffix >= len(labels):
        return None
    return ".".join(checked) + dot


def _suffix_length(labels: list[str]) -> int | None:
    """The number of labels in the public suffix, by the PSL algorithm without its default rule."""
    normal, wildcard, exception = _rules()
    best: int | None = None
    for start in range(len(labels)):
        candidate = labels[start:]
        key = _key(candidate)
        if key in exception:
            return len(candidate) - 1  # an exception rule wins; its first label is registrable
        if best is None and (key in normal or _key(candidate[1:]) in wildcard):
            best = len(candidate)  # the first match from the left is the longest
    return best


def udf(url: str | None) -> str | None:
    """The DuckDB function: an unsupported host becomes Crucible's unsupported error."""
    if url is None:
        return None
    try:
        return reg_domain(url)
    except CrucibleUnsupportedError as exc:
        raise ValueError(UNSUPPORTED_ERROR_PREFIX + exc.construct) from None
