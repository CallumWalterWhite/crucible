# Security Policy

## Supported versions

Crucible is pre-1.0. Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately, through GitHub private vulnerability reporting:
<https://github.com/CallumWalterWhite/crucible/security/advisories/new>

Do not open a public issue for a suspected vulnerability.

## Deployment model

Crucible is a local development and CI tool. It is not a production service.

- Crucible has **no authentication**. Anyone who can reach its port can run SQL against it.
- It binds `127.0.0.1` by default. Never expose it on a network, a shared host, or a public interface.
- Docker examples publish the port only on the loopback interface, as `-p 127.0.0.1:<port>:<port>`. Do not publish it
  without the `127.0.0.1` prefix.
- SQL cannot read or write files on the host, and cannot load DuckDB extensions. DuckDB external access is disabled and
  its configuration is locked.
- Credentials sent by clients are ignored. They are never stored and never logged.
