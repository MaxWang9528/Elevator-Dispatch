"""The Simulation: owns the clock, the people, the building and the elevator manager.

Each tick:
  1. traffic sources spawn people (who join a floor queue and press the hall button); in a run
     with a time of day (`day_start`) their rate and trip mix can follow rush hours
  2. waiting people whose button isn't lit press it again (e.g. left behind by a full car)
  3. each group's manager assigns its unassigned hall calls to its cars

Elevator groups: cars with the same `Elevator.group` form a bank with its own hall buttons and
its own manager and dispatcher, and each car may serve only some floors (`served_floors`). A
person rides one group's cars at a time. When no group serves both their floors they change cars
(at a sky lobby, say): `route` finds the fewest changes, and each ride is its own Person (a leg),
the next one pressing its group's button TRANSFER_WALK seconds after the last one arrives.
  4. each car steps; cars with open doors unload one person, else load one, else close
  5. the metrics collector records what happened (see metrics.py)
"""

from __future__ import annotations

import heapq
import random
from collections.abc import Callable, Iterator
from typing import Any, Protocol

from .building import Building
from .clock import DAY, format_time
from .dispatchers import NearestCarDispatcher
from .elevator import CarEvent, Elevator
from .enums import CarState, Direction, FloorKind
from .manager import Dispatcher, ElevatorManager
from .metrics import MetricsCollector
from .person import Arrived, AtFloor, InCar, Person

TRANSFER_WALK = 15.0    # seconds to walk from one elevator group to the next when changing cars
MAX_LEGS = 4            # the most rides a journey may take


class TrafficSource(Protocol):
    """Spawns people. A source may also define `on_arrive(sim, person)`, called whenever
    someone gets off an elevator at their floor (residents use it to plan their way back)."""

    def step(self, sim: Simulation, dt: float) -> None: ...


class Simulation:
    def __init__(
        self,
        building: Building,
        elevators: list[Elevator],
        dispatcher: Dispatcher | None = None,
        *,
        traffic: list[TrafficSource] | None = None,
        metrics: MetricsCollector | None = None,
        seed: int = 0,
        dt: float = 0.1,
        day_start: float | None = None,
        dispatcher_factory: Callable[[], Dispatcher] | None = None,
    ) -> None:
        """With more than one elevator group, pass `dispatcher_factory` (each group gets its own
        dispatcher) rather than `dispatcher`."""
        self.building = building
        self._elevators = list(elevators)
        group_ids = sorted({car.group for car in elevators}) or [0]
        if len(group_ids) > 1 and dispatcher is not None:
            raise ValueError("several elevator groups need a dispatcher_factory: one dispatcher each")
        make = dispatcher_factory or (lambda: dispatcher or NearestCarDispatcher())
        self.managers = [ElevatorManager([c for c in elevators if c.group == g], make(), group=g)
                         for g in group_ids]
        self._manager_of = {m.group: m for m in self.managers}
        self._group_floors = {m.group: _served(m.elevators) for m in self.managers}
        self._routes: dict[tuple[int, int], tuple[int, ...] | None] = {}
        self._legs_due: list[tuple[float, int, Person]] = []   # (when, order, leg just finished)
        self._legs_seq = 0
        self.reach = self._reach(len(building))
        self.direct = self._direct(len(building))
        self.traffic: list[TrafficSource] = list(traffic or [])
        self.metrics = metrics or MetricsCollector()
        self.seed = seed
        self.rng = random.Random(seed)
        self.dt = dt
        self.time = 0.0
        self._ticks = 0
        # The time of day at time 0, in seconds after midnight. None = this run has no time of
        # day, so traffic stays at its baseline (no rush hours).
        self.day_start = day_start
        self.people: dict[int, Person] = {}
        self._next_person_id = 0
        self._waiting_floors: set[int] = set()   # floors with someone in a queue (a speed-up)
        for car in elevators:
            if not 0 <= car.position <= building.top:
                raise ValueError(f"car {car.id} starts outside the building at {car.position}")
            car.max_floor = building.top

    @property
    def elevators(self) -> list[Elevator]:
        """Every car in the building, in id order."""
        return self._elevators

    @property
    def manager(self) -> ElevatorManager:
        """The manager of a building with a single elevator group."""
        if len(self.managers) != 1:
            raise RuntimeError("this building has several elevator groups; use sim.managers")
        return self.managers[0]

    @property
    def dispatcher_name(self) -> str:
        d = self.managers[0].dispatcher
        return getattr(d, "name", type(d).__name__)

    # --- elevator groups ---------------------------------------------------------------
    def _reach(self, n: int) -> dict[int, frozenset[int]] | None:
        """Floor -> the floors it can reach, changing cars if need be. None when every car serves
        every floor."""
        if all(floors is None for floors in self._group_floors.values()):
            return None
        every = frozenset(range(n))
        parent = list(range(n))

        def find(f: int) -> int:
            while parent[f] != f:
                parent[f] = parent[parent[f]]
                f = parent[f]
            return f

        served: set[int] = set()
        for floors in self._group_floors.values():
            floors = sorted(every if floors is None else floors)
            served.update(floors)
            for f in floors[1:]:
                parent[find(f)] = find(floors[0])
        parts: dict[int, set[int]] = {}
        for f in served:
            parts.setdefault(find(f), set()).add(f)
        return {f: frozenset(parts[find(f)]) if f in served else frozenset() for f in range(n)}

    def _direct(self, n: int) -> dict[int, frozenset[int]] | None:
        """Floor -> the floors one ride reaches from it. None when every car serves every floor."""
        if all(floors is None for floors in self._group_floors.values()):
            return None
        every = frozenset(range(n))
        direct: dict[int, set[int]] = {f: set() for f in range(n)}
        for floors in self._group_floors.values():
            floors = every if floors is None else floors
            for f in floors:
                direct[f] |= floors
        return {f: frozenset(r) for f, r in direct.items()}

    def route(self, origin: int, destination: int) -> tuple[int, ...] | None:
        """The floors to change cars at between two floors: () for one ride, None if no route.
        The fewest changes, then the shortest distance; changes are never at closed floors."""
        if len(self.managers) == 1:
            return ()
        key = (origin, destination)
        if key not in self._routes:
            self._routes[key] = self._find_route(origin, destination)
        return self._routes[key]

    def _find_route(self, origin: int, destination: int) -> tuple[int, ...] | None:
        n = len(self.building)
        every = frozenset(f for f in range(n) if self.building[f].kind is not FloorKind.CLOSED)
        floors = {g: every if s is None else s - {f for f in s if f not in every}
                  for g, s in self._group_floors.items()}
        serves = lambda g, f: self._group_floors[g] is None or f in self._group_floors[g]  # noqa: E731
        goal = {g for g in floors if serves(g, destination)}
        paths = [[g] for g in floors if serves(g, origin)]
        if any(p[0] in goal for p in paths):
            return ()
        for _ in range(MAX_LEGS - 1):
            paths = [p + [g] for p in paths for g in floors if g not in p and floors[p[-1]] & floors[g]]
            done = [p for p in paths if p[-1] in goal]
            if done:
                best: tuple[float, tuple[int, ...]] | None = None
                for p in done:
                    at, changes, distance = origin, [], 0
                    for a, b in zip(p, p[1:]):
                        t = min(floors[a] & floors[b], key=lambda f: (abs(at - f) + abs(f - destination), f))
                        distance += abs(at - t)
                        changes.append(t)
                        at = t
                    distance += abs(at - destination)
                    if best is None or distance < best[0]:
                        best = (distance, tuple(changes))
                return best[1]
        return None

    def groups_serving(self, a: int, b: int) -> list[int]:
        """The elevator groups that serve both floors."""
        return [g for g, floors in self._group_floors.items()
                if floors is None or (a in floors and b in floors)]

    def _pick_group(self, origin: int, destination: int) -> int:
        """The group a person uses: the most specific one that serves both floors (fewest
        floors), so an express bank isn't crowded by riders a local bank serves. Ties go by
        chance, weighted by the number of cars."""
        if len(self.managers) == 1:
            return self.managers[0].group
        groups = self.groups_serving(origin, destination)
        if not groups:
            raise ValueError(f"no elevator group serves both floor {self.building[origin].label} "
                             f"and floor {self.building[destination].label}")
        size = lambda g: len(self.building) if self._group_floors[g] is None else len(self._group_floors[g])  # noqa: E731
        fewest = min(size(g) for g in groups)
        groups = [g for g in groups if size(g) == fewest]
        if len(groups) == 1:
            return groups[0]
        weights = [len(self._manager_of[g].elevators) for g in groups]
        return self.rng.choices(groups, weights=weights)[0]

    @property
    def time_of_day(self) -> float | None:
        """Seconds after midnight right now, or None in a run without a time of day."""
        return None if self.day_start is None else (self.day_start + self.time) % DAY

    # --- people ------------------------------------------------------------------------
    def spawn_person(self, origin: int | None = None, destination: int | None = None, *,
                     resident_id: int | None = None, purpose: str = "") -> Person:
        """Add a person waiting at `origin` who wants `destination`; random where omitted."""
        n = len(self.building)
        if origin is None:
            origin = self.rng.randrange(n)
        if not 0 <= origin < n:
            raise ValueError(f"floor {origin} is outside the building (0..{n - 1})")
        if destination is None and self.reach is None:
            destination = self.rng.randrange(n - 1)
            if destination >= origin:
                destination += 1
        elif destination is None:
            choices = sorted(self.reach[origin] - {origin})
            if not choices:
                raise ValueError(f"no elevator stops at floor {self.building[origin].label}")
            destination = self.rng.choice(choices)
        if not 0 <= destination < n:
            raise ValueError(f"floor {destination} is outside the building (0..{n - 1})")
        changes = self.route(origin, destination)
        if changes is None:
            raise ValueError(f"no elevators connect floor {self.building[origin].label} "
                             f"and floor {self.building[destination].label}")
        stops = (*changes, destination)
        return self._add_person(origin, stops[0], onward=stops[1:], resident_id=resident_id,
                                purpose=purpose)

    def _add_person(self, origin: int, destination: int, *, onward: tuple[int, ...] = (),
                    resident_id: int | None = None, purpose: str = "", journey: int = -1,
                    leg: int = 1, journey_start: float | None = None,
                    prior_wait: float = 0.0, prior_ride: float = 0.0) -> Person:
        """A person starts waiting at `origin` for a ride to `destination` (one leg)."""
        group = self._pick_group(origin, destination)
        person = Person(self._next_person_id, origin, destination, self.time, AtFloor(origin),
                        resident_id=resident_id, purpose=purpose, group=group, journey=journey,
                        leg=leg, onward=onward, journey_start=journey_start,
                        prior_wait=prior_wait, prior_ride=prior_ride)
        self._next_person_id += 1
        self.people[person.id] = person
        self.building[origin].queue(person.direction).append(person)
        self._waiting_floors.add(origin)
        self.metrics.on_spawn(person)
        self._press_if_needed(origin, person.direction)
        return person

    def board(self, person: Person, car: Elevator) -> None:
        """The only way onto a car. Updates the person, the floor queue and the car together."""
        if not (isinstance(person.location, AtFloor) and person.location.floor == car.floor):
            raise RuntimeError(f"{person} can't board car {car.id} at floor {car.floor}")
        if car.state is not CarState.DOORS_OPEN or car.is_full:
            raise RuntimeError(f"{person} can't board car {car.id} ({car.state}, load {car.load})")

        floor = self.building[car.floor]
        floor.queue(person.direction).remove(person)
        if not floor.up_queue and not floor.down_queue:
            self._waiting_floors.discard(floor.index)
        car.passengers.append(person)
        person.location = InCar(car.id)
        person.board_time = self.time
        person.car_id = car.id
        car.car_calls.add(person.requested_floor)
        self.metrics.on_board(person, car, self.time)
        if car.direction is Direction.IDLE:
            car.direction = person.direction
            self._answer_call(car.floor, person.direction, car.group)

    def exit_car(self, person: Person, car: Elevator) -> None:
        """The only way off a car."""
        if car.state is not CarState.DOORS_OPEN or person not in car.passengers:
            raise RuntimeError(f"{person} can't leave car {car.id}")
        car.passengers.remove(person)
        person.location = Arrived(car.floor)
        person.arrive_time = self.time
        self.metrics.on_arrive(person, car, self.time)
        if person.onward:           # changing cars: on to the next group after a short walk
            self._legs_seq += 1
            heapq.heappush(self._legs_due, (self.time + TRANSFER_WALK, self._legs_seq, person))
            return
        for source in self.traffic:
            hook = getattr(source, "on_arrive", None)
            if hook is not None:
                hook(self, person)

    # --- main loop ---------------------------------------------------------------------
    def step(self) -> None:
        # Counted in ticks, so times stay clean decimals (5400.1, not 5400.1000000003) and
        # a saved trips file reproduces the live report exactly.
        self._ticks += 1
        self.time = round(self._ticks * self.dt, 6)
        while self._legs_due and self._legs_due[0][0] <= self.time + 1e-9:
            prev = heapq.heappop(self._legs_due)[2]
            self._add_person(prev.requested_floor, prev.onward[0], onward=prev.onward[1:],
                             resident_id=prev.resident_id, purpose=prev.purpose, journey=prev.journey,
                             leg=prev.leg + 1, journey_start=prev.journey_start,
                             prior_wait=prev.prior_wait + prev.wait_time,
                             prior_ride=prev.prior_ride + prev.ride_time)
        for source in self.traffic:
            source.step(self, self.dt)
        # Only floors with people waiting can need a button pressed, in the same order as
        # checking every floor from the bottom (up before down) would.
        for index in sorted(self._waiting_floors):
            self._press_if_needed(index, Direction.UP)
            self._press_if_needed(index, Direction.DOWN)
        for manager in self.managers:
            manager.step(self.time)
        recording = self.metrics._recording(self.time - self.dt)

        for car in self.elevators:
            prev_state, prev_position = car.state, car.position
            if car.step(self.dt) is CarEvent.DOORS_OPENED:
                for rider in car.passengers:
                    if rider.requested_floor != car.floor:
                        rider.stops_en_route += 1
                if car.direction is not Direction.IDLE:
                    self._answer_call(car.floor, car.direction, car.group)
            if car.state is CarState.DOORS_OPEN:
                self._service_open_car(car)
            self.metrics.observe_car(car, prev_state, prev_position, self.time, self.dt, recording)
        self.metrics.on_tick(self.time, self.dt)

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / self.dt)):
            self.step()

    def _answer_call(self, floor: int, direction: Direction, group: int) -> None:
        """A car is open at `floor` going `direction`: its group's hall button goes out."""
        call = self._manager_of[group].call_served(floor, direction)
        if call is not None:
            self.metrics.on_call_answered(call, self.time)

    def _press_if_needed(self, floor: int, direction: Direction) -> None:
        queue = self.building[floor].queue(direction)
        if not queue:
            return
        groups = (self.managers[0].group,) if len(self.managers) == 1 else {p.group for p in queue}
        for group in groups:
            manager = self._manager_of[group]
            if manager.is_lit(floor, direction) or self._car_loading_at(floor, direction, manager):
                continue
            manager.press(floor, direction, self.time)

    @staticmethod
    def _car_loading_at(floor: int, direction: Direction, manager: ElevatorManager) -> bool:
        return any(
            car.floor == floor
            and car.state in (CarState.DOORS_OPENING, CarState.DOORS_OPEN)
            and car.direction in (direction, Direction.IDLE)
            for car in manager.elevators
        )

    def _service_open_car(self, car: Elevator) -> None:
        """One person at a time: everyone getting off first, then people getting on."""
        if car.transfer_timer > 0:
            return
        leaving = next((p for p in car.passengers if p.requested_floor == car.floor), None)
        if leaving is not None:
            self.exit_car(leaving, car)
            car.note_transfer()
            return
        boarding = self._next_boarder(car)
        if boarding is not None:
            self.board(boarding, car)
            car.note_transfer()
            return
        if car.state_timer <= 0:
            if car.is_full and car.direction is not Direction.IDLE:
                # Anyone still queued for this direction wanted this car and couldn't fit.
                for person in self.building[car.floor].queue(car.direction):
                    if person.group == car.group and car.serves(person.requested_floor):
                        person.left_behind += 1
            car.close_doors()

    def _next_boarder(self, car: Elevator) -> Person | None:
        if car.is_full:
            return None
        floor = self.building[car.floor]
        direction = car.direction
        if direction is Direction.IDLE:
            # An idle car takes whoever is first, preferring up.
            if floor.up_queue:
                direction = Direction.UP
            elif floor.down_queue:
                direction = Direction.DOWN
            else:
                return None
        return next((p for p in floor.queue(direction)
                     if p.group == car.group and car.serves(p.requested_floor)), None)

    # --- output ------------------------------------------------------------------------
    def layout(self) -> dict[str, Any]:
        """Static description of the building, sent to the renderer once."""
        return {
            "floors": [
                {"i": f.index, "label": f.label, "kind": f.kind.name.lower(), "name": f.name}
                for f in self.building.floors
            ],
            "cars": [
                {
                    "id": c.id,
                    "capacity": c.capacity,
                    "max_speed": c.speed,
                    "served_floors": None if c.served_floors is None else sorted(c.served_floors),
                    "group": c.group,
                }
                for c in self.elevators
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        """Everything that changes, grouped by container, for one rendered frame."""
        clock = self.time_of_day
        return {
            "t": round(self.time, 3),
            "clock": None if clock is None else round(clock, 3),
            "cars": [
                {
                    "id": c.id,
                    "pos": round(c.position, 4),
                    "vel": round(c.velocity, 3),
                    "state": c.state.name.lower(),
                    "dir": c.direction.name.lower(),
                    "door": round(c.door_openness, 3),
                    "riders": [p.id for p in c.passengers],
                    "car_calls": sorted(c.car_calls),
                    "hall_stops": sorted([f, d.name.lower()] for f, d in c.hall_stops),
                }
                for c in self.elevators
            ],
            "floors": [
                {"i": f.index, "up": [p.id for p in f.up_queue], "down": [p.id for p in f.down_queue]}
                for f in self.building.floors
                if f.up_queue or f.down_queue
            ],
            "hall_calls": [
                {"floor": c.floor, "dir": c.direction.name.lower(), "assigned_to": c.assigned_to,
                 "group": m.group}
                for m in self.managers for c in m.hall_calls.values()
            ],
        }

    def summary(self) -> dict[str, Any]:
        """Cheap live numbers for the side panel. See MetricsCollector.live()."""
        return self.metrics.live(self.time, self.building.floors)

    def report(self) -> dict[str, Any]:
        """Every metric, for comparing algorithms. See metrics.py for definitions."""
        calls = [c for m in self.managers for c in m.hall_calls.values()]
        report = self.metrics.report(self.time, self.people.values(), self.building.floors,
                                     self.elevators, calls)
        report["run"] = {
            "dispatcher": self.dispatcher_name,
            "seed": self.seed,
            "day_start": self.day_start,
            "floors": len(self.building),
            "elevators": len(self.elevators),
        }
        return report

    TRIP_COLUMNS = ("person", "from_floor", "to_floor", "direction", "status", "measured",
                    "spawn_time", "board_time", "arrive_time", "wait", "ride", "trip",
                    "car", "stops_en_route", "left_behind", "resident", "purpose", "spawn_clock",
                    "journey", "leg", "journey_to",
                    "dispatcher", "seed", "warmup", "long_wait", "chart_interval", "run_time")

    def trip_rows(self) -> Iterator[tuple[Any, ...]]:
        """One row per person (columns: TRIP_COLUMNS), for analysis outside the simulator.
        A journey that changes cars has a row per ride, linked by `journey` (its first ride's
        person) and numbered by `leg`; `journey_to` is where the whole journey ends. Each row's
        times are for that ride alone (the report adds a journey's rides together).
        `wait` is blank until the person boards; times are simulated seconds since the run
        began, and `spawn_clock` is the time of day they pressed the button (timed runs only).
        The last six columns are the same on every row: the run's algorithm, seed and metric
        settings, and how far it had got, so the file can be imported again on its own."""
        def num(value: float | None) -> float | str:
            return "" if value is None else round(value, 2)

        m = self.metrics
        run = (self.dispatcher_name, self.seed,
               m.warmup, m.long_wait, m.bucket_seconds, round(self.time, 1))
        for p in self.people.values():
            status = "waiting" if p.board_time is None else "riding" if p.arrive_time is None else "delivered"
            yield (
                p.id, self.building[p.origin_floor].label, self.building[p.requested_floor].label,
                p.direction.name.lower(), status, int(self.metrics.measures(p)),
                num(p.spawn_time), num(p.board_time), num(p.arrive_time),
                num(p.wait_time), num(p.ride_time), num(p.trip_time),
                "" if p.car_id is None else p.car_id, p.stops_en_route, p.left_behind,
                "" if p.resident_id is None else p.resident_id, p.purpose,
                "" if self.day_start is None else format_time(self.day_start + p.spawn_time, with_seconds=True),
                p.journey, p.leg, self.building[p.final_floor].label,
                *run,
            )

    # --- debugging ---------------------------------------------------------------------
    def check_invariants(self) -> None:
        """Raise AssertionError if any person's location disagrees with the containers."""
        in_queue: dict[int, int] = {}
        for floor in self.building.floors:
            for d in (Direction.UP, Direction.DOWN):
                for p in floor.queue(d):
                    assert p.id not in in_queue, f"{p} is in two queues"
                    assert p.direction is d, f"{p} is in the {d.name} queue"
                    in_queue[p.id] = floor.index
        in_car: dict[int, int] = {}
        for car in self.elevators:
            assert car.load <= car.capacity, f"car {car.id} over capacity"
            assert -1e-9 <= car.position <= self.building.top + 1e-9, f"car {car.id} left the shaft"
            for p in car.passengers:
                assert p.id not in in_car and p.id not in in_queue, f"{p} is in two places"
                assert p.requested_floor in car.car_calls or car.floor == p.requested_floor, (
                    f"{p} rides car {car.id} but their floor isn't pressed"
                )
                in_car[p.id] = car.id
        for p in self.people.values():
            match p.location:
                case AtFloor(floor):
                    assert in_queue.get(p.id) == floor, f"{p} isn't in floor {floor}'s queue"
                case InCar(eid):
                    assert in_car.get(p.id) == eid, f"{p} isn't in car {eid}"
                case Arrived():
                    assert p.id not in in_queue and p.id not in in_car, f"{p} arrived but is still somewhere"


def _served(cars: list[Elevator]) -> frozenset[int] | None:
    """The floors a group serves: None if any of its cars serves every floor."""
    if any(c.served_floors is None for c in cars):
        return None
    return frozenset().union(*(c.served_floors for c in cars))
