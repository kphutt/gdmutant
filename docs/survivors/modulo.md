---
type: reference
status: active
---

# modulo survivor

**The change:** gdmutant swapped `%` with another operator (e.g. `*`, `/`).

**Why it survived:** every test input is a clean multiple, where `%`, `*`, and `/` can produce indistinguishable results.

**How to kill it:** add a test with a **non-multiple** input (one that leaves a remainder) and assert the exact result.

**Equivalent mutant?** Rare; possible if the operand is always a multiple by construction.
