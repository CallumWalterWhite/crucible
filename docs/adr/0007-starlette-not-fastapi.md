# 7. Starlette, not FastAPI

Status: accepted

## Context

Clients parse error bodies. A framework-generated 404, 405, 422 or 500 in the wrong shape turns a
readable warehouse error into an opaque client exception.

## Decision

Use Starlette directly. `server/app.py` installs a catch-all route plus 404, 405 and exception
handlers that delegate to the dialect server, so every response, errors included, has the
warehouse's shape. Request bodies are validated by hand against the warehouse API, and validation
errors use the warehouse's own error format. Each response shape cites the client source or
official API documentation it follows in a one-line comment.

## Consequences

- No automatic OpenAPI or request models; the API surface is small and specified per endpoint.
