"""People and where they are.

A person stores *which container* holds them (a floor queue or a car), never coordinates.
The containers store who is inside them. Only `Simulation.board` / `Simulation.exit_car`
move people, so both sides are always updated together.
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import Direction


@dataclass(frozen=True)
class AtFloor:
    """Waiting in a floor's up or down queue."""

    floor: int


@dataclass(frozen=True)
class InCar:
    """Riding an elevator."""

    elevator_id: int


@dataclass(frozen=True)
class Arrived:
    """Trip finished."""

    floor: int


Location = AtFloor | InCar | Arrived


@dataclass(eq=False)
class Person:
    id: int
    origin_floor: int
    requested_floor: int
    spawn_time: float
    location: Location
    board_time: float | None = None
    arrive_time: float | None = None
    car_id: int | None = None      # the car they rode (set when boarding)
    stops_en_route: int = 0        # times their car stopped at a floor that wasn't theirs
    left_behind: int = 0           # times a car going their way closed its doors full
    resident_id: int | None = None # which resident is making this trip (None: a visitor)
    purpose: str = ""              # commute_out, commute_in, outing, outing_return, lunch,
                                   # lunch_return, or "" (added by hand)
    group: int = 0                 # the elevator group whose hall button they press
    # A journey that changes cars is one Person per ride (leg). All its legs share `journey` (the
    # first leg's id) and `journey_start` (when the first button was pressed); `onward` lists the
    # floors still to come after this leg: the next changes, then where they're really going.
    journey: int = -1              # set to the person's own id for a first leg
    leg: int = 1
    onward: tuple[int, ...] = ()
    journey_start: float | None = None

    def __post_init__(self) -> None:
        if self.origin_floor == self.requested_floor:
            raise ValueError(f"person {self.id}: origin and requested floor are both {self.origin_floor}")
        if self.journey < 0:
            self.journey = self.id
        if self.journey_start is None:
            self.journey_start = self.spawn_time

    @property
    def final_floor(self) -> int:
        """Where this person's journey ends (this leg's floor unless they change cars)."""
        return self.onward[-1] if self.onward else self.requested_floor

    @property
    def direction(self) -> Direction:
        """Derived, so it can never disagree with the floors."""
        return Direction.UP if self.requested_floor > self.origin_floor else Direction.DOWN

    @property
    def wait_time(self) -> float | None:
        return None if self.board_time is None else self.board_time - self.spawn_time

    @property
    def ride_time(self) -> float | None:
        if self.board_time is None or self.arrive_time is None:
            return None
        return self.arrive_time - self.board_time

    @property
    def trip_time(self) -> float | None:
        """Button press to arrival: wait + ride."""
        return None if self.arrive_time is None else self.arrive_time - self.spawn_time

    def __repr__(self) -> str:
        return f"Person({self.id}, {self.origin_floor}->{self.requested_floor}, {self.location})"
