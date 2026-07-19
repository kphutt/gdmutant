# constant survivor

**The change:** gdmutant flipped a boolean literal (`true` ↔ `false`).

**Why it survived:** nothing your tests assert depends on this value, so its actual value is invisible to the suite.

**How to kill it:** add a test that exercises the behavior this flag/value controls and assert it matches the value.

**Equivalent mutant?** If the value never affects observable behavior (dead flag), the survivor is legitimate — consider removing the constant.
