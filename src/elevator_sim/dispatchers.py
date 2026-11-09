"""Dispatcher algorithms (which car answers which hall call)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .enums import CarState, Direction

if TYPE_CHECKING:
    from .elevator import Elevator
    from .manager import ElevatorManager, HallCall


class NearestCarDispatcher:
    """Baseline: send the car that would reach the call soonest, as a rough time in seconds.

    A car already heading toward the call in the call's direction, with room to brake for it,
    just counts the travel. Otherwise it must finish its current run and turn around first.
    Travel time includes speeding up and slowing down, and each stop the car already has adds
    the time its doors take - so a car with many nearby stops looks slower than its distance.
    """

    def __init__(self, stop_weight: float = 1.0) -> None:
        self.stop_weight = stop_weight

    def assign(self, call: HallCall, manager: ElevatorManager) -> Elevator | None:
        candidates = [c for c in manager.elevators if c.serves(call.floor) and not c.is_full]
        if not candidates:
            return None
        return min(candidates, key=lambda c: (self.cost(c, call), c.id))

    def cost(self, car: Elevator, call: HallCall) -> float:
        pos, d = car.position, car.direction
        stops = len(car.all_stop_floors()) * car.stop_time() * self.stop_weight

        if d is Direction.IDLE:
            return car.travel_time(call.floor - pos) + stops

        offset = (call.floor - pos) * d.value
        # A moving car can only stop where it has room to brake; a stopped one can open here.
        if car.state is CarState.MOVING:
            on_the_way = car.can_stop_at(call.floor, d)
        else:
            on_the_way = offset > -1e-9
        if on_the_way and call.direction is d:
            return car.travel_time(offset) + stops

        # Finish this run (to the farthest stop ahead, or the call itself), then come back.
        ahead = car.stops_ahead(d)
        if offset > 0:
            ahead.append(call.floor)
        turn = max(ahead, key=lambda f: abs(f - pos)) if ahead else pos
        return car.travel_time(turn - pos) + car.travel_time(turn - call.floor) + stops
