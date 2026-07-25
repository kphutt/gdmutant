---
type: reference
status: active
---

# arithmetic survivor

**The change:** gdmutant swapped an arithmetic operator (e.g. `+` → `-`, `*` → `/`). Note `+` may also concatenate strings.

**Why it survived:** nothing pins the exact result, so the two operators produce values your tests treat the same (they check the sign, or "non-zero", but not the number).

**How to kill it:** add a test with concrete inputs and assert the **exact** expected result.

**Equivalent mutant?** Possible when the operands make both operators yield the same value (e.g. `x * 1` vs `x / 1`, or `x + 0`).
