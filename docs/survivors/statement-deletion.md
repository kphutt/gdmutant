---
type: reference
status: active
---

# statement-deletion survivor

**The change:** gdmutant removed a whole statement (replaced it with `pass`).

**Why it survived:** nothing your tests assert depends on this statement running, so its entire effect is unchecked.

**How to kill it:** add a test that asserts the effect of this line — a signal emitted, a field set, a call made — something that fails if the line is gone.

**Equivalent mutant?** Legitimate if the statement genuinely has no observable effect (dead code) — consider removing it.
