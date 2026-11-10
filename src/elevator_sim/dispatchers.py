"""Dispatcher algorithms: which car answers which hall call, and where idle cars wait.

Each dispatcher only uses what a real group controller knows: which hall buttons are lit (and
when they were pressed), and each car's position, speed, direction, load and stops. It never
sees where waiting people want to go.

DISPATCHERS lists the ones you can pick in Settings and in the command-line tools:

  nearest    the baseline: the car that would get there soonest, assigned once; idle cars
             wait wherever they stopped. Each car turns around after its last stop (the
             variant of SCAN known as LOOK).
  scan       the classic elevator algorithm: cars sweep all the way to each end of the
             building, stopping for every call in their direction as they pass
  lookahead  looks ahead three ways: along each car's whole route (when it would really
             arrive, and who the extra stop would delay), to where the next calls will come
             from (idle cars park there), and to a better car for each call (calls move when
             one comes along)
"""

from __future__ import annotations

import math
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .enums import CarState, Direction

if TYPE_CHECKING:
    from .elevator import Elevator
    from .manager import ElevatorManager, HallCall


# --- the baseline ---------------------------------------------------------------------------
class NearestCarDispatcher:
    """Baseline: send the car that would reach the call soonest, as a rough time in seconds.

    A car already heading toward the call in the call's direction, with room to brake for it,
    just counts the travel. Otherwise it must finish its current run and turn around first.
    Travel time includes speeding up and slowing down, and each stop the car already has adds
    the time its doors take - so a car with many nearby stops looks slower than its distance.
    """

    name = "nearest"

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


# --- SCAN ------------------------------------------------------------------------------------
class ScanDispatcher:
    """The classic elevator algorithm. Each car sweeps from one end of the building to the
    other, stopping for every call in its direction as it passes, and turns around only at
    the ends (its lowest and highest served floors).

    - A hall call goes to the car that will pass it soonest going the call's way: a car
      already moving that way with the floor ahead (and room to brake). Until one is, the
      call waits; it is never given to a car that would have to turn around first.
    - Once a car starts a sweep, it runs to the end before turning, even when nobody is left
      aboard. At the end it rests until there is a reason to sweep back.
    - A resting car sets off when it would reach a waiting call sooner than any sweeping car,
      measuring both along their sweeps (up to the end and back, if need be).
    Car load and stop counts are ignored: SCAN is about distance along the sweep.
    """

    name = "scan"

    def __init__(self) -> None:
        self.sweep: dict[int, Direction] = {}     # each car's sweep direction

    def assign(self, call: HallCall, manager: ElevatorManager) -> Elevator | None:
        return self._passing_car(call, manager)

    def step(self, manager: ElevatorManager, now: float) -> None:
        cars = manager.elevators
        # Cars on the move keep sweeping to the end in their direction.
        for car in cars:
            if car.direction is not Direction.IDLE:
                self.sweep[car.id] = car.direction
                busy = car.state is CarState.MOVING or car.car_calls or car.hall_stops
                if car.sweep_to is None and busy:
                    end = self._end(car, car.direction)
                    if abs(end - car.position) > 1e-9:
                        car.sweep_to = end
        # Each call goes to the car that will pass it first going its way (if any yet).
        for call in list(manager.hall_calls.values()):
            best = self._passing_car(call, manager)
            if best is not None:
                if call.assigned_to != best.id:
                    manager.assign(call, best)
            elif call.assigned_to is not None:
                manager.car(call.assigned_to).hall_stops.discard(call.key)
                call.assigned_to = None
        # Start a resting car for a waiting call, if it would get there before any sweeping car.
        for call in list(manager.hall_calls.values()):
            if call.assigned_to is not None:
                continue
            best_car, best_dist, best_dir = None, math.inf, None
            for car in cars:
                if not car.serves(call.floor) or car.is_full:
                    continue
                if self._resting(car):
                    for d in (Direction.UP, Direction.DOWN):
                        if abs(self._end(car, d) - car.position) < 1e-9:
                            continue      # already at that end: the only way to go is back
                        dist = self._path(car, call, d)
                        if dist < best_dist - 1e-9:
                            best_car, best_dist, best_dir = car, dist, d
                else:
                    dist = self._path(car, call, self._direction(car))
                    if dist < best_dist - 1e-9:
                        best_car, best_dist, best_dir = car, dist, None
            if best_car is not None and best_dir is not None:
                self.sweep[best_car.id] = best_dir
                end = self._end(best_car, best_dir)
                best_car.sweep_to = end if abs(end - best_car.position) > 1e-9 else None
                if self._passes(best_car, call):
                    manager.assign(call, best_car)

    # --- geometry ------------------------------------------------------------------------
    @staticmethod
    def _end(car: Elevator, d: Direction) -> int:
        """The car's last floor in direction d."""
        if car.served_floors:
            return max(car.served_floors) if d is Direction.UP else min(car.served_floors)
        return (car.max_floor or 0) if d is Direction.UP else 0

    def _direction(self, car: Elevator) -> Direction:
        return car.direction if car.direction is not Direction.IDLE else self.sweep.get(car.id, Direction.UP)

    @staticmethod
    def _resting(car: Elevator) -> bool:
        return (car.state is CarState.IDLE and car.sweep_to is None
                and not car.car_calls and not car.hall_stops)

    def _passes(self, car: Elevator, call: HallCall) -> bool:
        """Will this car pass the call's floor going the call's way, on its current sweep?"""
        if car.is_full or not car.serves(call.floor):
            return False
        d = self._direction(car)
        if d is not call.direction:
            return False
        if car.state is CarState.MOVING:
            return car.can_stop_at(call.floor, d)
        return (call.floor - car.position) * d.value > -1e-9

    def _passing_car(self, call: HallCall, manager: ElevatorManager) -> Elevator | None:
        passing = [c for c in manager.elevators if self._passes(c, call)]
        if not passing:
            return None
        return min(passing, key=lambda c: (abs(call.floor - c.position), c.id))

    def _path(self, car: Elevator, call: HallCall, d: Direction) -> float:
        """Floors the car travels, sweeping in direction d, before it passes the call going its way."""
        p, f = car.position, call.floor
        moving = car.state is CarState.MOVING and car.direction is d
        ahead = car.can_stop_at(f, d) if moving else (f - p) * d.value > -1e-9
        if call.direction is d and ahead:
            return abs(f - p)
        end = self._end(car, d)
        if call.direction is not d:
            return abs(end - p) + abs(end - f)
        other = self._end(car, d.opposite)
        return abs(end - p) + abs(end - other) + abs(other - f)


# --- learning where calls come from ---------------------------------------------------------
class CallDemand:
    """How often each floor has called a car lately. Each hall call counts 1, fading by half
    every `half_life` seconds. `prior` mixes in an even spread over the floors, so the estimate
    is sensible before any calls (and never rules a floor out entirely)."""

    def __init__(self, n_floors: int, half_life: float = 600.0, prior: float = 0.05) -> None:
        self.n_floors = n_floors
        self.rate = math.log(2) / half_life
        self.prior = prior
        self.weights = [0.0] * n_floors
        self.origin = 0.0     # weights are stored scaled by exp(rate * (t - origin))

    def observe(self, floor: int, now: float) -> None:
        exponent = self.rate * (now - self.origin)
        if exponent > 50:     # rescale before the numbers get huge
            factor = math.exp(-exponent)
            self.weights = [w * factor for w in self.weights]
            self.origin, exponent = now, 0.0
        self.weights[floor] += math.exp(exponent)

    def probabilities(self) -> list[float]:
        """The chance that the next call comes from each floor."""
        total = sum(self.weights)
        even = 1 / self.n_floors
        if total <= 0:
            return [even] * self.n_floors
        return [(1 - self.prior) * w / total + self.prior * even for w in self.weights]


# --- parking idle cars ----------------------------------------------------------------------
class Parker:
    """Sends idle cars to wait where they can answer the next call soonest.

    A car that has had nothing to do for `idle_delay` seconds is offered every floor it serves
    (and offered them again every `recheck` seconds while it stays idle, as demand shifts).
    A floor is scored by the expected time for the nearest waiting car (this one or another
    idle or parking car) to reach the next call, weighted by `CallDemand`. The car moves only
    if that improves by at least `min_gain` seconds, so cars don't shuffle back and forth over
    small differences.
    """

    def __init__(self, idle_delay: float = 3.0, min_gain: float = 6.0, interval: float = 1.0,
                 recheck: float = 30.0) -> None:
        self.idle_delay = idle_delay
        self.min_gain = min_gain
        self.interval = interval
        self.recheck = recheck
        self._idle_since: dict[int, float] = {}
        self._checked: dict[int, float] = {}
        self._next_run = 0.0
        self._travel: dict[tuple[int, int], float] = {}

    def step(self, manager: ElevatorManager, demand: CallDemand, now: float) -> None:
        cars = manager.elevators
        for car in cars:
            if _has_nothing_to_do(car):
                self._idle_since.setdefault(car.id, now)
            else:
                self._idle_since.pop(car.id, None)
                self._checked.pop(car.id, None)
        if now < self._next_run:
            return
        self._next_run = now + self.interval

        due = [c for c in cars
               if (since := self._idle_since.get(c.id)) is not None and now - since >= self.idle_delay
               and now - self._checked.get(c.id, -math.inf) >= self.recheck]
        if not due:
            return
        # Cars that can take the next call from where they wait: idle ones, and ones on their
        # way to park (counted at their parking floor).
        waiting = {c.id: (c.park_floor if c.is_parking() else c.floor)
                   for c in cars if _has_nothing_to_do(c) or c.is_parking()}
        probs = demand.probabilities()
        n = len(probs)
        for car in due:
            self._checked[car.id] = now
            # Best time to each floor from the *other* waiting cars, then try this car everywhere.
            others = [math.inf] * n
            for cid, pos in waiting.items():
                if cid != car.id:
                    other = manager.car(cid)
                    for f in range(n):
                        if other.serves(f):
                            t = self._time(other, abs(pos - f))
                            if t < others[f]:
                                others[f] = t
            here = waiting[car.id]

            def score(at: int) -> float:
                total = 0.0
                for f in range(n):
                    t = self._time(car, abs(at - f)) if car.serves(f) else math.inf
                    best = t if t < others[f] else others[f]
                    if best < math.inf:
                        total += probs[f] * best
                return total

            current = score(here)
            best_floor, best_score = here, current
            for f in range(n):
                if f != here and car.serves(f):
                    sc = score(f)
                    if sc < best_score - 1e-9:
                        best_floor, best_score = f, sc
            if best_floor != here and current - best_score >= self.min_gain:
                car.park_floor = best_floor
                waiting[car.id] = best_floor

    def _time(self, car: Elevator, distance: int) -> float:
        key = (car.id, distance)
        t = self._travel.get(key)
        if t is None:
            t = self._travel[key] = car.travel_time(distance) if distance else 0.0
        return t


def _has_nothing_to_do(car: Elevator) -> bool:
    return car.state is CarState.IDLE and not car.car_calls and not car.hall_stops


# --- estimated time of arrival ----------------------------------------------------------------
@dataclass
class _Stop:
    key: tuple         # ("drop", floor) or ("pick", floor, direction)
    weight: float      # people the stop matters to: riders getting off, or 1 for a pickup
    is_call: bool = False


class LookaheadDispatcher:
    """Pick the car by its estimated time to reach the caller along its whole route, plus what
    the extra stop costs everyone already counting on that car. Idle cars park (see Parker).

    cost = time to arrive
         + delay_weight x extra delay to riders aboard and to the car's other pickups
         + tail_weight x (seconds past `tail_after` that the caller's total wait would reach)^2
         + near_full_penalty if the car has room for at most one more person
    Ties go to the car that has traveled least, to share the work.

    Every `interval` seconds, each lit call that its car isn't already braking or opening for
    moves to another car if that car is at least `margin` seconds cheaper.
    """

    name = "lookahead"

    def __init__(self, *, delay_weight: float = 0.5,
                 tail_after: float = 60.0, tail_weight: float = 0.05, near_full_penalty: float = 30.0,
                 margin: float = 5.0, interval: float = 1.0, parker: Parker | None = None,
                 half_life: float = 600.0, prior: float = 0.05) -> None:
        self.delay_weight = delay_weight
        self.tail_after = tail_after
        self.tail_weight = tail_weight
        self.near_full_penalty = near_full_penalty
        self.margin = margin
        self.interval = interval
        self.parker = parker or Parker()
        self.half_life, self.prior = half_life, prior
        self.demand: CallDemand | None = None
        self._seen: weakref.WeakSet[HallCall] = weakref.WeakSet()
        self._next_check = 0.0
        self.reassignments = 0

    # --- the manager calls these ---------------------------------------------------------
    def assign(self, call: HallCall, manager: ElevatorManager) -> Elevator | None:
        candidates = [c for c in manager.elevators if c.serves(call.floor) and not c.is_full]
        if not candidates:
            return None
        now = manager.now
        return min(candidates, key=lambda c: (self.cost(c, call, now), c.odometer, c.id))

    def step(self, manager: ElevatorManager, now: float) -> None:
        self.demand = _observe_calls(self.demand, self._seen, manager, now, self.half_life, self.prior)
        self.parker.step(manager, self.demand, now)
        if now >= self._next_check:
            self._next_check = now + self.interval
            self._reassign(manager, now)

    # --- cost ------------------------------------------------------------------------------
    def cost(self, car: Elevator, call: HallCall, now: float) -> float:
        eta, delay = self.estimate(car, call)
        cost = eta + self.delay_weight * delay
        late = (now - call.pressed_at) + eta - self.tail_after
        if late > 0:
            cost += self.tail_weight * late * late
        if car.capacity - car.load <= 1:
            cost += self.near_full_penalty
        return cost

    def estimate(self, car: Elevator, call: HallCall) -> tuple[float, float]:
        """(seconds until the car opens at the caller's floor, extra seconds of delay the stop
        adds for everyone else the car is carrying or picking up, weighted by how many)."""
        others = [k for k in car.hall_stops if k != call.key]
        before = {stop.key: t for stop, t in _route(car, others, None)}
        eta, delay = math.inf, 0.0
        for stop, t in _route(car, others, call):
            if stop.is_call:
                eta = t
            else:
                delay += stop.weight * max(0.0, t - before.get(stop.key, t))
        return eta, delay

    # --- reassignment ------------------------------------------------------------------------
    def _reassign(self, manager: ElevatorManager, now: float) -> None:
        for call in list(manager.hall_calls.values()):
            if call.assigned_to is None:
                continue
            current = manager.car(call.assigned_to)
            if _committed(current, call):
                continue
            current_cost = self.cost(current, call, now)
            best, best_cost = None, current_cost - self.margin
            for car in manager.elevators:
                if car is current or not car.serves(call.floor) or car.is_full:
                    continue
                c = self.cost(car, call, now)
                if c < best_cost:
                    best, best_cost = car, c
            if best is not None:
                manager.assign(call, best)
                self.reassignments += 1


def _observe_calls(demand: CallDemand | None, seen: weakref.WeakSet, manager: ElevatorManager,
                   now: float, half_life: float, prior: float) -> CallDemand:
    if demand is None:
        top = max(c.max_floor or 0 for c in manager.elevators)
        demand = CallDemand(top + 1, half_life, prior)
    for call in manager.hall_calls.values():
        if call not in seen:
            seen.add(call)
            demand.observe(call.floor, call.pressed_at)
    return demand


def _committed(car: Elevator, call: HallCall) -> bool:
    """The car is already opening at the call's floor, or braking to stop there."""
    if car.floor == call.floor and car.state in (CarState.DOORS_OPENING, CarState.DOORS_OPEN):
        return True
    if car.state is CarState.MOVING and car.next_target() == call.floor:
        return abs(call.floor - car.position) <= car.braking_distance + 0.5
    return False


# --- route model ------------------------------------------------------------------------------
def _route(car: Elevator, hall_stops: Iterable[tuple[int, Direction]], call: HallCall | None
           ) -> list[tuple[_Stop, float]]:
    """The order a car will visit its stops (collective control, as in Elevator.next_target),
    with the estimated time it opens at each. `call`, if given, is added as a pickup.

    Riders aren't tracked by destination (a real controller can't see that), so the car's
    load is spread evenly over the floors pressed inside it.
    """
    pos = car.position
    d = car.direction
    car_calls = set(car.car_calls)
    picks = set(hall_stops)
    call_key = call.key if call is not None else None
    if call_key is not None:
        picks.add(call_key)
    if d is Direction.IDLE:
        floors = car_calls | {f for f, _ in picks}
        if not floors:
            return []
        nearest = min(floors, key=lambda f: (abs(f - pos), f))
        d = Direction.toward(pos, nearest) if nearest != pos else (
            call.direction if call is not None and call.floor == nearest else Direction.UP)
    riders = car.load / len(car_calls) if car_calls else 0.0

    def ahead(f: float, direction: Direction, origin: float) -> bool:
        return (f - origin) * direction.value > -1e-9

    order: list[tuple[int, list[_Stop]]] = []

    def visit(floor: int, stops: list[_Stop]) -> None:
        if order and order[-1][0] == floor:
            order[-1][1].extend(stops)
        else:
            order.append((floor, stops))

    def stops_at(floor: int, direction: Direction, include_car_calls: bool) -> list[_Stop]:
        out = []
        if include_car_calls and floor in car_calls:
            out.append(_Stop(("drop", floor), riders))
        if (floor, direction) in picks:
            out.append(_Stop(("pick", floor, direction), 1.0, is_call=(floor, direction) == call_key))
        return out

    moving = car.state is CarState.MOVING and car.direction is d

    def reachable(f: float) -> bool:
        """In the first sweep: ahead, and (if moving) with room to brake."""
        return car.can_stop_at(f, d) if moving else ahead(f, d, pos)

    # Sweep 1: onward in d, serving car calls and pickups going d. Sweep 2: back the other
    # way, serving pickups going that way (and any car calls left behind). Sweep 3: d again,
    # for pickups going d that were behind the car.
    origin = pos
    sweeps = [(d, origin, True), (d.opposite, None, True), (d, None, True), (d.opposite, None, True)]
    remaining_calls = set(car_calls)
    remaining_picks = set(picks)
    for direction, start, use_calls in sweeps:
        floors = set()
        for f in remaining_calls:
            if start is None or reachable(f):
                floors.add(f)
        for f, hd in remaining_picks:
            if hd is direction and (start is None or reachable(f)):
                floors.add(f)
        # In the first sweep, the car also runs on to turn around at the farthest pickup
        # going the other way, if that is beyond everything else.
        for f in sorted(floors, key=lambda x: x * direction.value):
            got = stops_at(f, direction, use_calls and f in remaining_calls)
            remaining_calls.discard(f)
            remaining_picks.discard((f, direction))
            visit(f, got)
        if direction is d and start is not None:
            turn = [f for f, hd in remaining_picks if hd is not d and reachable(f)]
            if turn:
                far = max(turn, key=lambda x: x * d.value)
                if not order or (far - order[-1][0]) * d.value > 0:
                    visit(far, [])
        if not remaining_calls and not remaining_picks:
            break
    # Anything left (shouldn't happen) is served last.
    for f, hd in remaining_picks:
        visit(f, [_Stop(("pick", f, hd), 1.0, is_call=(f, hd) == call_key)])

    # Times along the route.
    result: list[tuple[_Stop, float]] = []
    t = 0.0
    if car.state in (CarState.DOORS_OPENING, CarState.DOORS_OPEN, CarState.DOORS_CLOSING):
        t += max(car.state_timer, 0.0) + (car.door_time if car.state is not CarState.DOORS_CLOSING else 0.0)
    here = pos
    stop_cost = car.stop_time()
    for floor, stops in order:
        if abs(floor - here) > 1e-9:
            t += car.travel_time(floor - here)
            here = floor
        if not stops:
            continue          # a turnaround point: the car opens there for the pickup going back
        for stop in stops:
            result.append((stop, t + car.door_time))
        t += stop_cost
    return result


# --- registry -------------------------------------------------------------------------------
@dataclass(frozen=True)
class DispatcherInfo:
    label: str
    description: str
    make: Callable[[], object]


DISPATCHERS: dict[str, DispatcherInfo] = {
    "nearest": DispatcherInfo(
        "Nearest car", "The car that would get there soonest, assigned once. Idle cars wait where they stopped.",
        lambda: NearestCarDispatcher()),
    "scan": DispatcherInfo(
        "SCAN", "The classic elevator algorithm: each car sweeps all the way to each end of the "
        "building, stopping for every call in its direction as it passes, and turns around only "
        "at the ends.",
        lambda: ScanDispatcher()),
    "lookahead": DispatcherInfo(
        "Lookahead", "Estimates when each car would really arrive along its whole route and who an "
        "extra stop would delay, parks idle cars where calls are expected, and moves calls to a "
        "better car when one comes along.",
        lambda: LookaheadDispatcher()),
}

# Names used by earlier versions, so settings saved then still load.
RETIRED_DISPATCHERS = {"eta+park+reassign": "lookahead", "eta+park": "lookahead", "nearest+park": "lookahead"}


def make_dispatcher(name: str):
    try:
        return DISPATCHERS[name].make()
    except KeyError:
        raise ValueError(f"no dispatcher called {name!r}; choose from {', '.join(DISPATCHERS)}") from None
