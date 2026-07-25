extends GutTest

## An independent GUT suite that does NOT reference TurnOrder — it compiles and passes entirely on
## its own. It exists to create the n>1 crash-safety shape the single-file corpus never could: when
## turn_order.gd is made uncompilable, the TurnOrder-referencing suite fails while THIS one stays
## healthy. The live probe (tests/test_selftest_live.py) uses that split to check gdmutant never
## reports a PASS off the healthy suite alone (a false survivor). It adds only always-passing tests,
## so it changes no mutant's verdict — the pinned 18/11/7 GUT outcome is unaffected.


func test_arithmetic_is_sane() -> void:
	assert_eq(2 + 2, 4)
	assert_true(1 < 2)


func test_string_ops() -> void:
	assert_eq("ab" + "cd", "abcd")
	assert_eq("HELLO".to_lower(), "hello")
