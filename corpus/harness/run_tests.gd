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
##
## Robustness ([ticket]): the target is loaded at RUNTIME and gated on `can_instantiate()`, NOT
## `preload`ed. A `preload` of a target that fails to compile makes THIS script fail to compile too,
## and `godot --script` then exits 0 on that load failure — a silent false PASS (a broken mutant
## would wrongly survive). `load()` sidesteps that, but it returns a NON-null *broken* GDScript on a
## compile error (so `if T == null` never fires), and directly *calling* such a script hangs the
## process. `GDScript.can_instantiate()` is a safe discriminator that never hangs: for THIS target —
## a concrete class — false ⇒ it didn't compile ⇒ quit(1) before calling into it. (Caveat: a
## cleanly-compiled `@abstract` class also reports can_instantiate() == false since Godot 4.5, so a
## harness whose target is `@abstract` must gate on a concrete subclass instead; turn_order.gd is
## concrete, so it's an exact proxy here.) gdmutant's generation-time guards (NF-5 re-parse + the
## return-path guard) already block most uncompilable mutants; this is defense-in-depth for the
## residual (a parse gdtoolkit accepts but Godot rejects), backed by the external timeout.

const TARGET_PATH := "res://turn_order.gd"

var _failures: int = 0


func _init() -> void:
	var target: Variant = load(TARGET_PATH)
	# load() returns a non-null broken GDScript on a compile failure; can_instantiate() is the only
	# gate that neither hangs nor lies. Bail non-zero BEFORE calling into the target.
	if target == null or not (target is GDScript) or not (target as GDScript).can_instantiate():
		push_error("harness: target %s failed to compile/load" % TARGET_PATH)
		quit(1)
		return

	# Dynamic `.call()` (not a static `TurnOrder.method`) so the harness never statically couples to
	# the target's API — a coupling that would reintroduce the compile-time-preload false PASS.

	# acts_before — the (4, 4) case is what kills the line-8 `>` -> `>=` mutant.
	_check(target.call("acts_before", 5, 3) == true, "acts_before(5, 3) should be true")
	_check(target.call("acts_before", 3, 5) == false, "acts_before(3, 5) should be false")
	_check(target.call("acts_before", 4, 4) == false, "acts_before(4, 4) should be false")

	# clamp_initiative
	_check(target.call("clamp_initiative", -3, 10) == 0, "clamp_initiative(-3, 10) should be 0")
	_check(target.call("clamp_initiative", 12, 10) == 10, "clamp_initiative(12, 10) should be 10")
	_check(target.call("clamp_initiative", 6, 10) == 6, "clamp_initiative(6, 10) should be 6")

	# is_adjacent
	_check(target.call("is_adjacent", 2, 2, 2, 3) == true, "is_adjacent (down) should be true")
	_check(target.call("is_adjacent", 2, 2, 3, 2) == true, "is_adjacent (right) should be true")
	_check(target.call("is_adjacent", 2, 2, 3, 3) == false, "is_adjacent diagonal should be false")
	_check(target.call("is_adjacent", 2, 2, 2, 2) == false, "is_adjacent same cell should be false")

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
