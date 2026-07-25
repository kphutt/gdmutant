---
type: reference
status: active
---

# compound-assign survivor

**The change:** gdmutant swapped a compound-assignment operator (e.g. `+=` → `-=`).

**Why it survived:** nothing pins the accumulated value, so the two updates look the same to your tests.

**How to kill it:** add a test that drives several updates through this line and asserts the exact accumulated value.

**Equivalent mutant?** Possible when the accumulator is never observed, or the update amount is zero.
