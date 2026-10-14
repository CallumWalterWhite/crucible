# 12. Timezones

Status: accepted (revisit in Phase 5)

## Context

BigQuery evaluates `CURRENT_DATE()` and timestamp-to-date conversions in UTC unless a timezone
argument is given. Snowflake sessions default to America/Los_Angeles and can change it with
`ALTER SESSION SET TIMEZONE`. DuckDB has one `TimeZone` setting, and `lock_configuration`
(ADR 0006) stops any cursor from changing it.

## Decision

- DuckDB's TimeZone is pinned to UTC for the whole process. It is a neutral base, not a warehouse
  default.
- The session timezone is session state (`Session.timezone`), set by each dialect's
  `new_session` (BigQuery `UTC`, Snowflake `America/Los_Angeles`) and changed by session commands.
- Each dialect's translation applies the session timezone explicitly wherever the result depends
  on it (current date/time functions, TIMESTAMP WITH TIME ZONE to DATE/TIME/string conversions,
  date parts of timezone-aware values). For BigQuery the session timezone is always UTC, so this is
  a no-op for Phase 2.

## Consequences

- Timezone behaviour is per session, as in the warehouses, and the configuration lock stays.
- A timezone-dependent construct that the translation does not handle must raise
  `CrucibleUnsupportedError` for non-UTC sessions; the Snowflake dialect owns that list.

## Revisit in Phase 5

Before building the Snowflake dialect, check whether DuckDB accepts a TimeZone setting at cursor
creation, before `lock_configuration` takes effect for that cursor. If it does, per-session
timezones could live in DuckDB instead of in translation.
