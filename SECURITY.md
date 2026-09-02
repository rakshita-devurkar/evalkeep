# Security policy

## Supported versions

Evalkeep is at 0.1 and has no releases yet. Only `main` is supported.

## Reporting a vulnerability

Please **do not open a public issue** for a suspected vulnerability.

Report it through GitHub's private vulnerability reporting on this repository:
[Security → Report a vulnerability](https://github.com/rakshita-devurkar/evalkeep/security/advisories/new).
That channel is private to the maintainer until a fix is published.

Expect an acknowledgement within a week. This is a personal project, so please
be patient with timelines; a fix will be prioritised over anything else.

## What counts as a vulnerability here

Evalkeep is designed to be pointed at production traces, so the security
surface is mostly about **data that should never leave**:

- Unredacted values reaching the database, an export, a log line, or a model
- A credential surviving into `targets.yaml`, which is committed
- Anything from a trace being interpreted as code — a shell command, a
  JavaScript assertion, a YAML tag
- Path traversal via a trace, target or export path

Anything in [docs/security.md](docs/security.md) that the code does not
actually do is a vulnerability report, not a documentation bug.

## Out of scope

- The agents you point Evalkeep at. It calls them; it does not vet them.
- Promptfoo itself — report those to [promptfoo](https://github.com/promptfoo/promptfoo).
- Findings that require an attacker who already has write access to your
  project directory or your database.
