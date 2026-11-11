"""Small shared enums."""

from __future__ import annotations

from enum import Enum, IntEnum, auto


class Direction(IntEnum):
    """An int enum only for speed: its members hash in C, which matters because (floor,
    direction) pairs fill the cars' stop sets and the dispatchers' lookups. Compare members
    with `is`, as everywhere else; don't use them as numbers (use `sign`)."""

    UP = 1
    DOWN = -1
    IDLE = 0

    @staticmethod
    def toward(from_pos: float, to_pos: float) -> Direction:
        if to_pos > from_pos:
            return Direction.UP
        if to_pos < from_pos:
            return Direction.DOWN
        return Direction.IDLE


# Plain attributes, set once, because hot loops read them constantly and Enum properties are
# Python-level calls: `d.sign` is 1, -1 or 0, and `d.opposite` is the reverse direction.
for _d in Direction:
    _d.sign = int(_d)
Direction.UP.opposite, Direction.DOWN.opposite, Direction.IDLE.opposite = (
    Direction.DOWN, Direction.UP, Direction.IDLE)


class CarState(Enum):
    """Motion and doors combined, so impossible combos (moving with doors open) can't exist."""

    IDLE = auto()            # stopped at a floor, doors closed, deciding what to do next
    MOVING = auto()
    DOORS_OPENING = auto()
    DOORS_OPEN = auto()      # people get off and on during this state
    DOORS_CLOSING = auto()


class FloorKind(Enum):
    RESIDENTIAL = auto()
    LOBBY = auto()
    PARKING = auto()
    AMENITY = auto()         # gym, pool, rooftop, lounge...
    RETAIL = auto()
    CLOSED = auto()          # nobody lives or works here and no elevator has to stop (mechanical floors)
