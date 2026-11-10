"""The ElevatorManager: bookkeeping for hall calls, plus a swappable Dispatcher.

The manager only knows what a real group controller knows: which hall buttons are lit and the
state of each car. It never sees where waiting people want to go.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .elevator import Elevator
from .enums import Direction


@dataclass(eq=False)
class HallCall:
    floor: int
    direction: Direction
    pressed_at: float
    assigned_to: int | None = None  # elevator id

    @property
    def key(self) -> tuple[int, Direction]:
        return (self.floor, self.direction)


class Dispatcher(Protocol):
    """Decides which car takes a hall call. Swap this to try different algorithms.

    A dispatcher may also define `step(manager, now)`. The manager calls it every tick after
    handing out new calls, so it can move calls between cars (`manager.assign`) or send idle
    cars somewhere to wait (`car.park_floor`).
    """

    def assign(self, call: HallCall, manager: ElevatorManager) -> Elevator | None:
        """Return the car to send, or None to leave the call unassigned for now."""
        ...


class ElevatorManager:
    def __init__(self, elevators: list[Elevator], dispatcher: Dispatcher) -> None:
        for i, car in enumerate(elevators):
            if car.id != i:
                raise ValueError(f"elevator at position {i} has id {car.id}; ids must be 0..n-1")
        self.elevators = elevators
        self.dispatcher = dispatcher
        self.hall_calls: dict[tuple[int, Direction], HallCall] = {}
        self.now = 0.0

    def is_lit(self, floor: int, direction: Direction) -> bool:
        return (floor, direction) in self.hall_calls

    def press(self, floor: int, direction: Direction, now: float) -> HallCall:
        """A hall button was pressed. Pressing a lit button does nothing."""
        key = (floor, direction)
        if key not in self.hall_calls:
            self.hall_calls[key] = HallCall(floor, direction, pressed_at=now)
        return self.hall_calls[key]

    def step(self, now: float) -> None:
        """Hand any unassigned calls to the dispatcher, then let it do anything else it does."""
        self.now = now
        for call in list(self.hall_calls.values()):
            if call.assigned_to is None:
                car = self.dispatcher.assign(call, self)
                if car is not None:
                    self.assign(call, car)
        hook = getattr(self.dispatcher, "step", None)
        if hook is not None:
            hook(self, now)

    def assign(self, call: HallCall, car: Elevator) -> None:
        """Give a call to a car, taking it from the car that had it, if any."""
        if call.assigned_to is not None:
            self.elevators[call.assigned_to].hall_stops.discard(call.key)
        call.assigned_to = car.id
        car.hall_stops.add(call.key)
        car.park_floor = None

    def call_served(self, floor: int, direction: Direction) -> HallCall | None:
        """A car opened its doors here going this way: turn the button off, whoever came.
        Returns the call that was answered, if the button was lit."""
        call = self.hall_calls.pop((floor, direction), None)
        if call is not None and call.assigned_to is not None:
            self.elevators[call.assigned_to].hall_stops.discard(call.key)
        return call
