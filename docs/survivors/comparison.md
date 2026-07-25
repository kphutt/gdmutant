---
type: reference
status: active
---

# comparison survivor

**The change:** gdmutant swapped a comparison operator (e.g. `>` → `>=`, `<` → `<=`, `==` → `!=`).

**Why it survived:** `>` and `>=` (and their kin) differ on exactly one input — when the two sides are **equal**. Your tests run this line but never with equal operands, so the boundary is untested.

**How to kill it:** add a test that reaches this line with two equal operands (a value compared to itself) and assert the result you intend. That case fails under the mutant.

**Equivalent mutant?** Rare here, but possible if the equal case is genuinely unreachable (e.g. the two operands can never be equal by construction). If so, the survivor is legitimate.
