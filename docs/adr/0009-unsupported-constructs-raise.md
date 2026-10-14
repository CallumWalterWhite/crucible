# 9. Unsupported constructs raise CrucibleUnsupportedError

Status: accepted

## Decision

A construct the warehouse accepts but Crucible does not emulate raises `CrucibleUnsupportedError`,
which names the construct and, when known, its line and column. Crucible never emits a best guess.
Servers return it in the warehouse's native error format (BigQuery: `invalidQuery`-shaped error
with the message "Not supported by Crucible: ..."), so clients show a readable error.

This covers SQL constructs, types (BIGNUMERIC, GEOGRAPHY, INTERVAL in the corpus), API
features, and names Crucible cannot represent (ADR 0002).

## Consequences

- A model that runs on Crucible either matches the warehouse or is a known divergence listed in
  `docs/compatibility.md`.
