extends GutTest

## GUT suite for the corpus module — the GUT counterpart of test/test_turn_order.gd.
## Mirrors the SAME cases so the per-mutant outcome should match the pinned expectation
## (line 8 `>`->`>=` killed; can_act / ties_favor_earlier untested -> survivors; clamp
## boundary equivalents survive). gdmutant mutates turn_order.gd and reruns this per mutant.


func test_acts_before() -> void:
	assert_true(TurnOrder.acts_before(5, 3))
	assert_false(TurnOrder.acts_before(3, 5))
	assert_false(TurnOrder.acts_before(4, 4))


func test_clamp_initiative() -> void:
	assert_eq(TurnOrder.clamp_initiative(-3, 10), 0)
	assert_eq(TurnOrder.clamp_initiative(12, 10), 10)
	assert_eq(TurnOrder.clamp_initiative(6, 10), 6)


func test_is_adjacent() -> void:
	assert_true(TurnOrder.is_adjacent(2, 2, 2, 3))
	assert_true(TurnOrder.is_adjacent(2, 2, 3, 2))
	assert_false(TurnOrder.is_adjacent(2, 2, 3, 3))
	assert_false(TurnOrder.is_adjacent(2, 2, 2, 2))
