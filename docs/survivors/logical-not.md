# logical-not survivor

**The change:** gdmutant removed a `not`, inverting a condition.

**Why it survived:** no test runs this branch with the condition both ways, so the inversion changes nothing your tests observe.

**How to kill it:** add a test that makes the condition true and another that makes it false, and assert which branch runs each time.

**Equivalent mutant?** If the guarded branch has no observable effect, the survivor is legitimate.
