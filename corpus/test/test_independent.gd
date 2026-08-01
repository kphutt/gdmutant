extends GdUnitTestSuite

## An independent GdUnit4 suite that does NOT reference TurnOrder — it compiles and passes entirely
## on its own. It exists to create the n>1 crash-safety shape the single-suite corpus never could:
## when turn_order.gd is made uncompilable, the TurnOrder-referencing suite fails to load while THIS
## one stays healthy. The live probe (tests/test_selftest_live.py) uses that split to check gdmutant
## never reports a PASS off the healthy suite alone (a false survivor). It adds only always-passing
## tests, so it changes no mutant's verdict — the pinned 18/11/7 GdUnit4 outcome is unaffected.
## It is the exact peer of corpus/gut_test/test_independent_gut.gd, so both frameworks are probed
## on the same shape.


func test_arithmetic_is_sane() -> void:
	assert_int(2 + 2).is_equal(4)
	assert_bool(1 < 2).is_true()


func test_string_ops() -> void:
	assert_str("ab" + "cd").is_equal("abcd")
	assert_str("HELLO".to_lower()).is_equal("hello")
