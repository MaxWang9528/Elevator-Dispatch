"""The Simulation: owns the clock, the people, the building and the elevator manager.

Each tick:
  1. traffic sources spawn people (who join a floor queue and press the hall button); in a run
     with a time of day (`day_start`) their rate and trip mix can follow rush hours
  2. waiting people whose button isn't lit press it again (e.g. left behind by a full car)
  3. the manager assigns unassigned hall calls to cars
  4. each car steps; cars with open doors unload one person, else load one, else close
  5. the metrics collector records what happened (see metrics.py)
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any, Protocol

from .building import Building
from .clock import DAY, format_time
from .dispatchers import NearestCarDispatcher
from .elevator import CarEvent, Elevator
from .enums import CarState, Direction
from .manager import Dispatcher, ElevatorManager
from .metrics import MetricsCollector
from .person import Arrived, AtFloor, InCar, Person


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
    ) -> None:
        self.building = building
        self.manager = ElevatorManager(elevators, dispatcher or NearestCarDispatcher())
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
        for car in elevators:
            if not 0 <= car.position <= building.top:
                raise ValueError(f"car {car.id} starts outside the building at {car.position}")
            car.max_floor = building.top

    @property
    def elevators(self) -> list[Elevator]:
        return self.manager.elevators

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
        if destination is None:
            destination = self.rng.randrange(n - 1)
            if destination >= origin:
                destination += 1
        for f in (origin, destination):
            if not 0 <= f < n:
                raise ValueError(f"floor {f} is outside the building (0..{n - 1})")

        person = Person(self._next_person_id, origin, destination, self.time, AtFloor(origin),
                        resident_id=resident_id, purpose=purpose)
        self._next_person_id += 1
        self.people[person.id] = person
        self.building[origin].queue(person.direction).append(person)
        self.metrics.on_spawn(person)
        self._press_if_needed(origin, person.direction)
        return person

    def board(self, person: Person, car: Elevator) -> None:
        """The only way onto a car. Updates the person, the floor queue and the car together."""
        if not (isinstance(person.location, AtFloor) and person.location.floor == car.floor):
            raise RuntimeError(f"{person} can't board car {car.id} at floor {car.floor}")
        if car.state is not CarState.DOORS_OPEN or car.is_full:
            raise RuntimeError(f"{person} can't board car {car.id} ({car.state}, load {car.load})")

        self.building[car.floor].queue(person.direction).remove(person)
        car.passengers.append(person)
        person.location = InCar(car.id)
        person.board_time = self.time
        person.car_id = car.id
        car.car_calls.add(person.requested_floor)
        self.metrics.on_board(person, car, self.time)
        if car.direction is Direction.IDLE:
            car.direction = person.direction
            self._answer_call(car.floor, person.direction)

    def exit_car(self, person: Person, car: Elevator) -> None:
        """The only way off a car."""
        if car.state is not CarState.DOORS_OPEN or person not in car.passengers:
            raise RuntimeError(f"{person} can't leave car {car.id}")
        car.passengers.remove(person)
        person.location = Arrived(car.floor)
        person.arrive_time = self.time
        self.metrics.on_arrive(person, car, self.time)
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
        for source in self.traffic:
            source.step(self, self.dt)
        for floor in self.building.floors:
            for d in (Direction.UP, Direction.DOWN):
                self._press_if_needed(floor.index, d)
        self.manager.step(self.time)

        for car in self.elevators:
            prev_state, prev_position = car.state, car.position
            if car.step(self.dt) is CarEvent.DOORS_OPENED:
                for rider in car.passengers:
                    if rider.requested_floor != car.floor:
                        rider.stops_en_route += 1
                if car.direction is not Direction.IDLE:
                    self._answer_call(car.floor, car.direction)
            if car.state is CarState.DOORS_OPEN:
                self._service_open_car(car)
            self.metrics.observe_car(car, prev_state, prev_position, self.time, self.dt)
        self.metrics.on_tick(self.time, self.dt)

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / self.dt)):
            self.step()

    def _answer_call(self, floor: int, direction: Direction) -> None:
        """A car is open at `floor` going `direction`: the hall button goes out."""
        call = self.manager.call_served(floor, direction)
        if call is not None:
            self.metrics.on_call_answered(call, self.time)

    def _press_if_needed(self, floor: int, direction: Direction) -> None:
        if not self.building[floor].queue(direction):
            return
        if self.manager.is_lit(floor, direction) or self._car_loading_at(floor, direction):
            return
        self.manager.press(floor, direction, self.time)

    def _car_loading_at(self, floor: int, direction: Direction) -> bool:
        return any(
            car.floor == floor
            and car.state in (CarState.DOORS_OPENING, CarState.DOORS_OPEN)
            and car.direction in (direction, Direction.IDLE)
            for car in self.elevators
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
                    if car.serves(person.requested_floor):
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
        return next((p for p in floor.queue(direction) if car.serves(p.requested_floor)), None)

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
                {"floor": c.floor, "dir": c.direction.name.lower(), "assigned_to": c.assigned_to}
                for c in self.manager.hall_calls.values()
            ],
        }

    def summary(self) -> dict[str, Any]:
        """Cheap live numbers for the side panel. See MetricsCollector.live()."""
        return self.metrics.live(self.time, self.building.floors)

    def report(self) -> dict[str, Any]:
        """Every metric, for comparing algorithms. See metrics.py for definitions."""
        report = self.metrics.report(self.time, self.people.values(), self.building.floors,
                                     self.elevators, self.manager.hall_calls.values())
        report["run"] = {
            "dispatcher": getattr(self.manager.dispatcher, "name", type(self.manager.dispatcher).__name__),
            "seed": self.seed,
            "day_start": self.day_start,
            "floors": len(self.building),
            "elevators": len(self.elevators),
        }
        return report

    TRIP_COLUMNS = ("person", "from_floor", "to_floor", "direction", "status", "measured",
                    "spawn_time", "board_time", "arrive_time", "wait", "ride", "trip",
                    "car", "stops_en_route", "left_behind", "resident", "purpose", "spawn_clock",
                    "dispatcher", "seed", "warmup", "long_wait", "chart_interval", "run_time")

    def trip_rows(self) -> Iterator[tuple[Any, ...]]:
        """One row per person (columns: TRIP_COLUMNS), for analysis outside the simulator.
        `wait` is blank until the person boards; times are simulated seconds since the run
        began, and `spawn_clock` is the time of day they pressed the button (timed runs only).
        The last six columns are the same on every row: the run's algorithm, seed and metric
        settings, and how far it had got, so the file can be imported again on its own."""
        def num(value: float | None) -> float | str:
            return "" if value is None else round(value, 2)

        m = self.metrics
        run = (getattr(self.manager.dispatcher, "name", type(self.manager.dispatcher).__name__), self.seed,
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
