class_name TurnOrder

## Turn-order and grid helpers (a gdmutant corpus fixture — small, real logic worth mutating).


# True if an actor with `a_speed` acts before one with `b_speed`.
static func acts_before(a_speed: int, b_speed: int) -> bool:
	return a_speed > b_speed


# Clamp an initiative value into [0, max_value].
static func clamp_initiative(value: int, max_value: int) -> int:
	if value < 0:
		return 0
	if value > max_value:
		return max_value
	return value


# True if two grid cells are orthogonally adjacent (Manhattan distance 1).
static func is_adjacent(ax: int, ay: int, bx: int, by: int) -> bool:
	return abs(ax - bx) + abs(ay - by) == 1


# True if an actor may act this round: alive and not stunned.
static func can_act(alive: bool, stunned: bool) -> bool:
	return alive and not stunned


# Whether ties favor the earlier actor — a fixed rule of this system.
static func ties_favor_earlier() -> bool:
	return true
