# 6. Security hardening

Status: accepted

## Context

Crucible runs arbitrary SQL from any client that can reach it, with no authentication.

## Decision

- Bind `127.0.0.1` by default. The CLI reads `CRUCIBLE_HOST` for the default; the Docker image
  sets `CRUCIBLE_HOST=0.0.0.0` because container port publishing needs it, and all documentation
  publishes ports as `-p 127.0.0.1:<port>:<port>` only.
- DuckDB starts with extension autoinstall and autoload off, then `allowed_directories` set to the
  catalog directory (persistent mode only), `enable_external_access = false`, the base TimeZone,
  and finally `lock_configuration = true`. After that, SQL cannot read or write host files, ATTACH
  outside the catalog directory, install or load extensions, or change any setting. The unit suite
  checks each of these.
- Load jobs ingest data through the API in Python, never through file paths in SQL.
- Credentials and tokens sent by clients are accepted and ignored. They are never stored or
  logged; uvicorn's access log is off.
- The image runs as a non-root user.

## Consequences

- Locking the configuration also locks DuckDB's TimeZone, so session timezones are applied in
  translation instead (ADR 0012).
- Extensions that are not built into the DuckDB wheel (e.g. spatial) are unavailable, so the
  constructs that need them raise `CrucibleUnsupportedError`.
