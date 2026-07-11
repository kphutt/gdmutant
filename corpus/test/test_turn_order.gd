extends GdUnitTestSuite

## GdUnit4 suite for the corpus module. Run live in CI (needs Godot + the GdUnit4 addon);
## gdmutant mutates turn_order.gd and reruns this suite per mutant.


func test_acts_before() -> void:
	assert_bool(TurnOrder.acts_before(5, 3)).is_true()
	assert_bool(TurnOrder.acts_before(3, 5)).is_false()
	assert_bool(TurnOrder.acts_before(4, 4)).is_false()


func test_clamp_initiative() -> void:
	assert_int(TurnOrder.clamp_initiative(-3, 10)).is_equal(0)
	assert_int(TurnOrder.clamp_initiative(12, 10)).is_equal(10)
	assert_int(TurnOrder.clamp_initiative(6, 10)).is_equal(6)


func test_is_adjacent() -> void:
	assert_bool(TurnOrder.is_adjacent(2, 2, 2, 3)).is_true()
	assert_bool(TurnOrder.is_adjacent(2, 2, 3, 2)).is_true()
	assert_bool(TurnOrder.is_adjacent(2, 2, 3, 3)).is_false()
	assert_bool(TurnOrder.is_adjacent(2, 2, 2, 2)).is_false()
