---
type: reference
status: active
---

# numeric survivor

**The change:** gdmutant changed a numeric literal (e.g. `0` → `1`, bumped a bound).

**Why it survived:** no test pins the exact value or the boundary this number sets.

**How to kill it:** add tests on each side of the boundary this number controls, and assert which side each input lands on.

**Equivalent mutant?** If the literal only affects an internal value that never changes an observable outcome, the survivor may be legitimate.
