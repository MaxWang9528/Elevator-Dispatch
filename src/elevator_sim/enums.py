"""Small shared enums."""

from __future__ import annotations

from enum import Enum, auto


class Direction(Enum):
    UP = 1
    DOWN = -1
    IDLE = 0

    @property
    def opposite(self) -> Direction:
        return {Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP}.get(self, Direction.IDLE)

    @staticmethod
    def toward(from_pos: float, to_pos: float) -> Direction:
        if to_pos > from_pos:
            return Direction.UP
        if to_pos < from_pos:
            return Direction.DOWN
        return Direction.IDLE


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
