---
type: how-to
status: active
created: 2026-07-10
---

# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report vulnerabilities privately via GitHub's **[Report a vulnerability](https://github.com/kphutt/gdmutant/security/advisories/new)**
button (repository → **Security** → **Advisories** → **Report a vulnerability**). This opens a
private advisory visible only to the maintainers.

We aim to acknowledge a report within a few days and will keep you updated on the fix and
disclosure timeline. Please give us a reasonable window to address the issue before any public
disclosure.

## Scope

`gdmutant` runs a project's own test suite against mutated copies of its source. Treat these as
the primary risk surfaces:

- It executes code (the target project's tests, plus `godot --headless`). Only run it on code
  you trust — the same trust boundary as running the project's own test suite.
- Report handling and any file it writes (mutated sources, reports) should stay within the
  target project's tree.

## Supported versions

Pre-1.0: only the latest `main` is supported. Security fixes land there first.
