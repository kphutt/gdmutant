# Survivor explainers

A **surviving mutant** is a change gdmutant made to your source that every test still passed —
proof that the behavior on that line isn't actually checked (coverage says the line *ran*; mutation
says the result isn't *asserted*). Each page here explains one mutation operator: what the change
is, why a survivor matters, how to kill it, and when it legitimately survives (an *equivalent
mutant*). The `more` link in each survivor points here.

The rule of thumb for every operator: **your tests pass whether the code is the original or the
mutant — so if the mutated behavior would be wrong, nothing guards against it.** Only you know the
intended result; gdmutant reports the gap, not the answer.
