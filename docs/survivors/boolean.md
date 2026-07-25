---
type: reference
status: active
---

# boolean survivor

**The change:** gdmutant swapped `and` ↔ `or`.

**Why it survived:** `and` and `or` return the same result **except** when the two operands disagree (one true, one false). No test exercised that case, so the connective is unchecked.

**How to kill it:** add a test where exactly one side is true and the other false, and assert the outcome — that is the only input that distinguishes `and` from `or`.

**Equivalent mutant?** If one operand can never be false (or never true) at this point, `and` and `or` are equivalent and the survivor is legitimate.
