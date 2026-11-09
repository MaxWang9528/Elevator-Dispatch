"""A single elevator car.

The car owns its motion, doors and stop list. It does *not* move people itself: when its doors
are open, the Simulation unloads/loads people through `Simulation.board` / `exit_car`.

Stops come from two places:
  car_calls   floors pressed inside the car by riders (always stopped at, either direction)
  hall_stops  (floor, direction) pickups assigned by the ElevatorManager; only served when the
              car is travelling in that direction (or turning around there)

Picking the next stop from these two sets + the current direction *is* the basic
"collective" / SCAN algorithm: keep going while there are stops ahead, then reverse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto

from .enums import CarState, Direction
from .person import Person

EPS = 1e-9


class CarEvent(Enum):
    DOORS_OPENED = auto()
    DOORS_CLOSED = auto()


@dataclass(eq=False)
class Elevator:
    id: int
    # --- configuration -----------------------------------------------------------------
    capacity: int = 16
    speed: float = 1.0                         # top speed, floors per second
    acceleration: float = 0.35                 # floors per second², used to speed up and to brake
    served_floors: frozenset[int] | None = None  # None = every floor; a subset = express car
    door_time: float = 2.0                     # seconds to open, and again to close
    dwell_time: float = 2.0                    # minimum time doors stay open
    transfer_time: float = 1.0                 # seconds per person getting on or off
    max_floor: int | None = None               # top floor index; set by the Simulation

    # --- live state --------------------------------------------------------------------
    position: float = 0.0                      # floor index; 14.3 = 30% of the way 14 -> 15
    velocity: float = 0.0                      # floors per second; + is up, - is down
    state: CarState = CarState.IDLE
    direction: Direction = Direction.IDLE      # travel direction AND the arrow people see
    state_timer: float = 0.0                   # door opening/closing countdown, or dwell left
    transfer_timer: float = 0.0                # time until the next person can get on/off

    passengers: list[Person] = field(default_factory=list)
    car_calls: set[int] = field(default_factory=set)
    hall_stops: set[tuple[int, Direction]] = field(default_factory=set)

    # --- simple queries ----------------------------------------------------------------
    @property
    def floor(self) -> int:
        """Nearest floor index (exact when stopped)."""
        return round(self.position)

    @property
    def load(self) -> int:
        return len(self.passengers)

    @property
    def is_full(self) -> bool:
        return len(self.passengers) >= self.capacity

    @property
    def door_openness(self) -> float:
        """0 = closed, 1 = fully open; for animating doors."""
        match self.state:
            case CarState.DOORS_OPENING:
                return 1.0 - max(self.state_timer, 0.0) / self.door_time
            case CarState.DOORS_OPEN:
                return 1.0
            case CarState.DOORS_CLOSING:
                return max(self.state_timer, 0.0) / self.door_time
        return 0.0

    def serves(self, floor: int) -> bool:
        return self.served_floors is None or floor in self.served_floors

    def all_stop_floors(self) -> set[int]:
        return self.car_calls | {f for f, _ in self.hall_stops}

    def has_stop_at(self, floor: int) -> bool:
        return (
            floor in self.car_calls
            or (floor, Direction.UP) in self.hall_stops
            or (floor, Direction.DOWN) in self.hall_stops
        )

    def is_ahead(self, floor: float, direction: Direction) -> bool:
        return (floor - self.position) * direction.value > EPS

    def stops_ahead(self, direction: Direction) -> list[int]:
        return [f for f in self.all_stop_floors() if self.is_ahead(f, direction)]

    # --- motion ------------------------------------------------------------------------
    @property
    def braking_distance(self) -> float:
        """Floors needed to come to a stop from the current speed."""
        return self.velocity ** 2 / (2 * self.acceleration)

    def can_stop_at(self, floor: float, direction: Direction) -> bool:
        """Is `floor` ahead in `direction` AND far enough away to brake for it?

        A car moving at full speed can't stop at the very next floor: it shoots past and
        serves that stop later. This is why moving cars skip calls that appear just ahead.
        """
        offset = (floor - self.position) * direction.value
        return offset > EPS and offset >= self.braking_distance - 1e-6

    def travel_time(self, distance: float) -> float:
        """Seconds to travel `distance` floors from standing still to standing still."""
        distance = abs(distance)
        ramp = self.speed ** 2 / self.acceleration   # floors spent speeding up + slowing down
        if distance >= ramp:
            return distance / self.speed + self.speed / self.acceleration
        return 2 * math.sqrt(distance / self.acceleration)  # never reaches top speed

    def stop_time(self) -> float:
        """Rough seconds a stop costs beyond travel: doors open, one person, doors close."""
        return 2 * self.door_time + max(self.dwell_time, self.transfer_time)

    # --- the basic algorithm -----------------------------------------------------------
    def next_target(self) -> int | None:
        """Floor to head for next, given the current direction. None = nothing ahead."""
        stops = self.all_stop_floors()
        if not stops:
            return None
        if self.direction is Direction.IDLE:
            return min(stops, key=lambda f: (abs(f - self.position), f))

        d = self.direction
        dist = lambda f: abs(f - self.position)  # noqa: E731
        reachable = lambda f: self.can_stop_at(f, d)  # noqa: E731
        # 1. Drop-offs and same-direction pickups ahead that we can still brake for: nearest first.
        same = [f for f in self.car_calls if reachable(f)]
        same += [f for f, hd in self.hall_stops if hd is d and reachable(f)]
        if same:
            return min(same, key=dist)
        # 2. Only opposite-direction pickups ahead: go to the farthest one and turn around there.
        opposite = [f for f, hd in self.hall_stops if hd is not d and reachable(f)]
        if opposite:
            return max(opposite, key=dist)
        return None

    def _direction_on_arrival(self, floor: int) -> Direction:
        """Which arrow to light when stopping at `floor`."""
        d = self.direction
        if d is Direction.IDLE:
            for hd in (Direction.UP, Direction.DOWN):
                if (floor, hd) in self.hall_stops:
                    return hd
            return Direction.IDLE
        if (floor, d) in self.hall_stops or self.stops_ahead(d):
            return d
        if (floor, d.opposite) in self.hall_stops or self.stops_ahead(d.opposite):
            return d.opposite
        return Direction.IDLE

    # --- state machine -----------------------------------------------------------------
    def step(self, dt: float) -> CarEvent | None:
        match self.state:
            case CarState.IDLE:
                self._step_idle()
            case CarState.MOVING:
                self._step_moving(dt)
            case CarState.DOORS_OPENING:
                self.state_timer -= dt
                if self.state_timer <= EPS:
                    self.state = CarState.DOORS_OPEN
                    self.state_timer = self.dwell_time
                    self.transfer_timer = 0.0
                    self.car_calls.discard(self.floor)
                    return CarEvent.DOORS_OPENED
            case CarState.DOORS_OPEN:
                # Loading/unloading is driven by the Simulation, which calls close_doors().
                self.state_timer -= dt
                self.transfer_timer -= dt
            case CarState.DOORS_CLOSING:
                self.state_timer -= dt
                if self.state_timer <= EPS:
                    self.state = CarState.IDLE
                    return CarEvent.DOORS_CLOSED
        return None

    def _step_idle(self) -> None:
        if not self.car_calls and not self.hall_stops:
            self.direction = Direction.IDLE
            return
        here = self.floor
        d = self.direction

        if d is Direction.IDLE:
            for hd in (Direction.UP, Direction.DOWN):
                if (here, hd) in self.hall_stops:
                    self.direction = hd
                    self._start_opening()
                    return
        else:
            # Pickup at this floor in our direction (e.g. assigned just as the doors closed).
            if (here, d) in self.hall_stops:
                self._start_opening()
                return
            if self.next_target() is None:
                # Nothing ahead: turn around, serving a pickup here first if there is one.
                self.direction = d.opposite
                if (here, self.direction) in self.hall_stops:
                    self._start_opening()
                    return

        target = self.next_target()
        if target is None:
            self.car_calls.discard(here)  # only stale stops at this floor were left
            self.direction = Direction.IDLE
            return
        self.direction = Direction.toward(self.position, target)
        self.state = CarState.MOVING

    def _step_moving(self, dt: float) -> None:
        """Trapezoidal motion: speed up at `acceleration`, cruise at `speed`, brake at
        `acceleration` so the car stops exactly on the target floor."""
        d = self.direction
        target = self.next_target()
        if target is None:
            # Our stops were taken by another car (or are too close to brake for):
            # stop at the nearest floor we can still brake for, then idle.
            reach = self.position + d.value * self.braking_distance
            target = math.ceil(reach - 1e-6) if d is Direction.UP else math.floor(reach + 1e-6)
            if self.max_floor is not None:
                target = min(max(target, 0), self.max_floor)

        remaining = (target - self.position) * d.value   # floors left, >= braking distance
        u = abs(self.velocity)
        a = self.acceleration
        # Fastest speed w at the end of this tick that still lets us stop at the target:
        # after moving (u + w) / 2 * dt, the braking distance w² / 2a must fit in what's left.
        disc = (a * dt) ** 2 - 4 * a * dt * u + 8 * a * remaining
        w_brake = (-a * dt + math.sqrt(disc)) / 2 if disc > 0 else 0.0
        w = max(0.0, min(self.speed, u + a * dt, w_brake))
        moved = (u + w) / 2 * dt

        if w_brake <= 1e-9 or moved >= remaining - 1e-6:
            self.position = float(target)
            self.velocity = 0.0
            self._arrive(target)
        else:
            self.position += d.value * moved
            self.velocity = d.value * w

    def _arrive(self, floor: int) -> None:
        self.velocity = 0.0
        stopping_for_something = self.has_stop_at(floor)
        self.direction = self._direction_on_arrival(floor)
        if stopping_for_something:
            self._start_opening()
        else:
            self.state = CarState.IDLE

    def _start_opening(self) -> None:
        self.state = CarState.DOORS_OPENING
        self.state_timer = self.door_time

    # --- called by the Simulation while doors are open ---------------------------------
    def note_transfer(self) -> None:
        """Someone just got on or off: wait for them, and keep the doors open meanwhile."""
        self.transfer_timer = self.transfer_time
        self.state_timer = max(self.state_timer, self.transfer_time)

    def close_doors(self) -> None:
        if self.state is not CarState.DOORS_OPEN:
            raise RuntimeError(f"car {self.id}: close_doors() while {self.state}")
        self.state = CarState.DOORS_CLOSING
        self.state_timer = self.door_time
