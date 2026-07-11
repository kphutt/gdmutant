# The version number tracks the built feature set; releasing is separately gated

## Status
Accepted

## Context
gdmutant's in-repo version was `0.0.0`, under a policy of "bump to `0.1.0` only after the two
remaining v0.1 items land (live Godot/GdUnit4 CI validation + the statement-deletion operator)."

But `0.0.0` reads as a placeholder — vaporware — to anyone who sees `gdmutant --version` or the
package metadata, when in fact the engine, GDScript adapter, GdUnit4 runner, Stryker reporter, and
`gdmutant run` CLI are all built, tested, and mutation-tested against themselves. A version number
can mean one of two things:

- **(a) what is built** — the feature set that exists, or
- **(b) what is released/shipped** — a tagged, published artifact.

Conflating the two forced `0.0.0` to stand in for "not shipped yet," which mislabels a substantial,
working v0.1 feature set as nothing.

## Decision
**The in-repo version number tracks the built feature set (meaning (a)).** It is **`0.1.0`** now —
the complete v0.1 engine + CLI.

**Releasing is a separate, gated event.** A tagged PyPI release (and flipping the repo public) still
require the two v0.1 completion items in `ROADMAP.md` — **live Godot/GdUnit4 CI validation** and the
**statement-deletion operator** — plus the naming decision. The version *string* is decoupled from
the *ship* gate.

## Consequences
- `gdmutant --version` and the package metadata read `0.1.0` — honest about what's built, not a
  placeholder.
- **No tag / release / `twine upload` / public flip happens** until the `ROADMAP.md` gates clear. The
  version number is *not* a claim of "shipped."
- `ROADMAP.md`'s "Remaining to finish v0.1 → public" list stays the authoritative ship gate; this ADR
  governs only what the *version string* means, not when a release happens.
