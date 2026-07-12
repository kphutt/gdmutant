extends SceneTree

## Hand-rolled headless test harness for the gdmutant corpus — the CommandRunner path
## (ADR-0005): exit 0 = suite passed, non-zero = a test failed. It needs NO test framework
## and NO addon, so the live self-test can lead with it to isolate "Godot runs" from "GdUnit4
## runs". It asserts the SAME cases as test/test_turn_order.gd, so both runner paths expect the
## same per-mutant outcome (line 8 killed; can_act / ties_favor_earlier untested → survivors).
##
## Run:  godot --headless --script res://harness/run_tests.gd
##
## Uses explicit `if …: fail()` / quit codes, never GDScript `assert()` — `assert()` is stripped
## from release-template builds, so a harness built on it would silently pass everything there.

const TurnOrder := preload("res://turn_order.gd")

var _failures: int = 0


func _init() -> void:
	# acts_before — the (4, 4) case is what kills the line-8 `>` -> `>=` mutant.
	_check(TurnOrder.acts_before(5, 3) == true, "acts_before(5, 3) should be true")
	_check(TurnOrder.acts_before(3, 5) == false, "acts_before(3, 5) should be false")
	_check(TurnOrder.acts_before(4, 4) == false, "acts_before(4, 4) should be false")

	# clamp_initiative
	_check(TurnOrder.clamp_initiative(-3, 10) == 0, "clamp_initiative(-3, 10) should be 0")
	_check(TurnOrder.clamp_initiative(12, 10) == 10, "clamp_initiative(12, 10) should be 10")
	_check(TurnOrder.clamp_initiative(6, 10) == 6, "clamp_initiative(6, 10) should be 6")

	# is_adjacent
	_check(TurnOrder.is_adjacent(2, 2, 2, 3) == true, "is_adjacent orthogonal (down) should be true")
	_check(TurnOrder.is_adjacent(2, 2, 3, 2) == true, "is_adjacent orthogonal (right) should be true")
	_check(TurnOrder.is_adjacent(2, 2, 3, 3) == false, "is_adjacent diagonal should be false")
	_check(TurnOrder.is_adjacent(2, 2, 2, 2) == false, "is_adjacent same cell should be false")

	# NOTE: can_act and ties_favor_earlier are deliberately NOT tested — their mutants must survive,
	# mirroring the coverage gap the GdUnit4 suite also leaves.

	if _failures > 0:
		push_error("harness: %d assertion(s) failed" % _failures)
		quit(1)
	else:
		quit(0)


func _check(passed: bool, message: String) -> void:
	if not passed:
		_failures += 1
		push_error("FAIL: " + message)
