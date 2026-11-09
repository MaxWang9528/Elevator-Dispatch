"""Metrics for comparing dispatch algorithms.

Vocabulary
  wait      button press (spawn) -> boarding. People who are still waiting count with their wait
            so far, so an algorithm can't look good by leaving people standing on a floor.
  ride      boarding -> arrival
  trip      button press -> arrival (wait + ride)
  measured  people who appeared at or after the warm-up. Car, queue and hall-call figures
            likewise only cover time after the warm-up, so the empty-building start-up
            (every car parked at the lobby, nobody waiting) doesn't skew a comparison.

The Simulation calls the `on_*` / `observe_car` hooks as things happen; they keep cheap running
totals for the live view. `report()` computes everything else (percentiles, per-floor and
per-interval breakdowns) on demand from the people themselves.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import fmean
from typing import TYPE_CHECKING, Any

from .enums import CarState, Direction

if TYPE_CHECKING:
    from .building import Floor
    from .elevator import Elevator
    from .manager import HallCall
    from .person import Person

_DOOR_STATES = (CarState.DOORS_OPENING, CarState.DOORS_OPEN, CarState.DOORS_CLOSING)
_EPS = 1e-9


# --- small statistics helpers -----------------------------------------------------------
def percentile(sorted_values: Sequence[float], q: float) -> float:
    """The q-th percentile (0-100) of an already sorted, non-empty sequence, interpolated."""
    if not sorted_values:
        raise ValueError("percentile of no values")
    k = (len(sorted_values) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    """Mean, median, tail percentiles and max, rounded to 0.1. All None when there are no values."""
    data = sorted(values)
    if not data:
        return {"n": 0, "mean": None, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    return {
        "n": len(data),
        "mean": round(fmean(data), 1),
        "p50": round(percentile(data, 50), 1),
        "p90": round(percentile(data, 90), 1),
        "p95": round(percentile(data, 95), 1),
        "p99": round(percentile(data, 99), 1),
        "max": round(data[-1], 1),
    }


def _ratio(a: float, b: float, digits: int = 3) -> float | None:
    return round(a / b, digits) if b else None


# --- running totals -----------------------------------------------------------------------
@dataclass
class CarStats:
    """What one car did during the measured period."""

    distance: float = 0.0            # floors traveled
    empty_distance: float = 0.0      # ...of which with nobody aboard
    passenger_distance: float = 0.0  # riders x floors, for the average load while moving
    starts: int = 0                  # times it set off from standstill (each costs energy)
    door_cycles: int = 0             # times it stopped and opened its doors
    time_moving: float = 0.0
    time_doors: float = 0.0          # opening, open or closing
    time_idle: float = 0.0
    boarded: int = 0
    delivered: int = 0
    departures: int = 0              # times the doors closed
    departure_load: int = 0          # riders aboard, summed over departures
    full_departures: int = 0         # departures at capacity
    peak_load: int = 0

    def add(self, other: CarStats) -> None:
        for name in self.__dataclass_fields__:
            if name == "peak_load":
                self.peak_load = max(self.peak_load, other.peak_load)
            else:
                setattr(self, name, getattr(self, name) + getattr(other, name))

    def to_dict(self) -> dict[str, Any]:
        total_time = self.time_moving + self.time_doors + self.time_idle
        return {
            "distance": round(self.distance, 1),
            "empty_distance": round(self.empty_distance, 1),
            "empty_share": _ratio(self.empty_distance, self.distance),
            "starts": self.starts,
            "door_cycles": self.door_cycles,
            "boarded": self.boarded,
            "delivered": self.delivered,
            "share_moving": _ratio(self.time_moving, total_time),
            "share_doors": _ratio(self.time_doors, total_time),
            "share_idle": _ratio(self.time_idle, total_time),
            "avg_load_moving": _ratio(self.passenger_distance, self.distance, 2),
            "avg_load_departing": _ratio(self.departure_load, self.departures, 2),
            "full_departures": self.full_departures,
            "full_departure_share": _ratio(self.full_departures, self.departures),
            "peak_load": self.peak_load,
            "floors_per_delivery": _ratio(self.distance, self.delivered, 1),
        }


@dataclass
class _Bucket:
    """Queue totals for one chart interval."""

    seconds: float = 0.0           # measured time that fell in this interval
    waiting_seconds: float = 0.0   # people waiting, integrated over that time
    peak_waiting: int = 0


class MetricsCollector:
    def __init__(self, warmup: float = 0.0, long_wait: float = 60.0, bucket_seconds: float = 300.0) -> None:
        if warmup < 0 or long_wait <= 0 or bucket_seconds <= 0:
            raise ValueError("warmup must be >= 0; long_wait and bucket_seconds must be > 0")
        self.warmup = warmup
        self.long_wait = long_wait
        self.bucket_seconds = bucket_seconds

        # Everyone since t=0: the live "what is in the building right now" counts.
        self.spawned = 0
        self.boarded = 0
        self.arrived = 0
        # Measured people only: running totals behind the live averages.
        self._m_spawned = 0
        self._m_boarded = 0
        self._m_arrived = 0
        self._m_wait_sum = 0.0            # finished waits
        self._m_wait_max = 0.0
        self._m_ride_sum = 0.0
        self._m_waiting_spawn_sum = 0.0   # spawn times of measured people still waiting
        # Measured period.
        self.measured_seconds = 0.0
        self._in_system_at_start: int | None = None
        self._waiting_seconds = 0.0
        self._riding_seconds = 0.0
        self._peak_waiting = 0
        self._buckets: dict[int, _Bucket] = {}
        self._call_responses: list[float] = []
        self.cars: dict[int, CarStats] = {}

    def measures(self, person: Person) -> bool:
        return person.spawn_time >= self.warmup - _EPS

    def _recording(self, now: float) -> bool:
        return now >= self.warmup - _EPS

    # --- hooks called by the Simulation --------------------------------------------------
    def on_spawn(self, person: Person) -> None:
        self.spawned += 1
        if self.measures(person):
            self._m_spawned += 1
            self._m_waiting_spawn_sum += person.spawn_time

    def on_board(self, person: Person, car: Elevator, now: float) -> None:
        self.boarded += 1
        if self.measures(person):
            wait = now - person.spawn_time
            self._m_boarded += 1
            self._m_waiting_spawn_sum -= person.spawn_time
            self._m_wait_sum += wait
            self._m_wait_max = max(self._m_wait_max, wait)
        if self._recording(now):
            self._car(car).boarded += 1

    def on_arrive(self, person: Person, car: Elevator, now: float) -> None:
        self.arrived += 1
        if self.measures(person):
            self._m_arrived += 1
            self._m_ride_sum += now - (person.board_time or now)
        if self._recording(now):
            self._car(car).delivered += 1

    def on_call_answered(self, call: HallCall, now: float) -> None:
        """A car opened its doors for a lit hall button."""
        if call.pressed_at >= self.warmup - _EPS:
            self._call_responses.append(now - call.pressed_at)

    def on_tick(self, now: float, dt: float) -> None:
        """Once per simulation step, after everything has moved. The step covers (now - dt, now],
        and only steps that lie wholly after the warm-up are counted."""
        if not self._recording(now - dt):
            return
        waiting = self.spawned - self.boarded
        riding = self.boarded - self.arrived
        if self._in_system_at_start is None:
            self._in_system_at_start = waiting + riding
        self.measured_seconds += dt
        self._waiting_seconds += waiting * dt
        self._riding_seconds += riding * dt
        self._peak_waiting = max(self._peak_waiting, waiting)
        bucket = self._buckets.setdefault(self._bucket_index(now - dt / 2), _Bucket())
        bucket.seconds += dt
        bucket.waiting_seconds += waiting * dt
        bucket.peak_waiting = max(bucket.peak_waiting, waiting)

    def observe_car(self, car: Elevator, prev_state: CarState, prev_position: float,
                    now: float, dt: float) -> None:
        """Once per car per step: what changed since before the step."""
        if not self._recording(now - dt):
            return
        stats = self._car(car)
        moved = abs(car.position - prev_position)
        if moved:
            stats.distance += moved
            stats.passenger_distance += moved * car.load
            if not car.passengers:
                stats.empty_distance += moved
        state = car.state
        if state is CarState.MOVING:
            stats.time_moving += dt
            if prev_state is not CarState.MOVING:
                stats.starts += 1
        elif state in _DOOR_STATES:
            stats.time_doors += dt
            if prev_state not in _DOOR_STATES:
                stats.door_cycles += 1
            if state is CarState.DOORS_CLOSING and prev_state is not CarState.DOORS_CLOSING:
                stats.departures += 1
                stats.departure_load += car.load
                stats.full_departures += car.is_full
                stats.peak_load = max(stats.peak_load, car.load)
        else:
            stats.time_idle += dt

    def _car(self, car: Elevator) -> CarStats:
        stats = self.cars.get(car.id)
        if stats is None:
            stats = self.cars[car.id] = CarStats()
        return stats

    def _bucket_index(self, t: float) -> int:
        return int(max(t, 0.0) // self.bucket_seconds)

    # --- live view (cheap; called for every frame sent to the browser) ---------------------
    def live(self, now: float, floors: Iterable[Floor]) -> dict[str, Any]:
        """Counts are everyone in the building right now. Averages cover measured people only,
        and include the wait so far of people who are still waiting."""
        m_waiting = self._m_spawned - self._m_boarded
        waited = self._m_wait_sum + (m_waiting * now - self._m_waiting_spawn_sum)
        longest = self._m_wait_max
        if m_waiting:
            for floor in floors:
                for queue in (floor.up_queue, floor.down_queue):
                    # Queues are in arrival order, so the first measured person waited longest.
                    oldest = next((p for p in queue if self.measures(p)), None)
                    if oldest is not None:
                        longest = max(longest, now - oldest.spawn_time)
        return {
            "time": round(now, 1),
            "spawned": self.spawned,
            "waiting": self.spawned - self.boarded,
            "riding": self.boarded - self.arrived,
            "arrived": self.arrived,
            "mean_wait": round(waited / self._m_spawned, 1) if self._m_spawned else None,
            "max_wait": round(longest, 1) if self._m_spawned else None,
            "mean_ride": round(self._m_ride_sum / self._m_arrived, 1) if self._m_arrived else None,
            "warming_up": not self._recording(now),
            "warmup": self.warmup,
        }

    # --- the full report --------------------------------------------------------------------
    def report(self, now: float, people: Iterable[Person], floors: Sequence[Floor],
               elevators: Sequence[Elevator], open_calls: Iterable[HallCall]) -> dict[str, Any]:
        n_floors = len(floors)
        waits: list[float] = []
        rides: list[float] = []
        trips: list[float] = []
        stops: list[int] = []
        by_dir = {d: {"waits": [], "trips": []} for d in (Direction.UP, Direction.DOWN)}
        floor_waits: list[list[float]] = [[] for _ in range(n_floors)]
        floor_to = [0] * n_floors
        bucket_waits: dict[int, list[float]] = {}
        bucket_delivered: dict[int, int] = {}
        bucket_still_waiting: dict[int, int] = {}
        waiting = riding = delivered = 0
        long_waits = left_people = left_events = 0

        for p in people:
            if not self.measures(p):
                continue
            if p.board_time is None:
                wait = now - p.spawn_time      # still waiting: counts as at least this long
                waiting += 1
                index = self._bucket_index(p.spawn_time)
                bucket_still_waiting[index] = bucket_still_waiting.get(index, 0) + 1
            else:
                wait = p.board_time - p.spawn_time
            waits.append(wait)
            long_waits += wait > self.long_wait
            direction = by_dir[p.direction]
            direction["waits"].append(wait)
            floor_waits[p.origin_floor].append(wait)
            floor_to[p.requested_floor] += 1
            bucket_waits.setdefault(self._bucket_index(p.spawn_time), []).append(wait)
            if p.left_behind:
                left_people += 1
                left_events += p.left_behind
            if p.arrive_time is not None:
                delivered += 1
                rides.append(p.arrive_time - p.board_time)
                trip = p.arrive_time - p.spawn_time
                trips.append(trip)
                direction["trips"].append(trip)
                stops.append(p.stops_en_route)
                index = self._bucket_index(p.arrive_time)
                bucket_delivered[index] = bucket_delivered.get(index, 0) + 1
            elif p.board_time is not None:
                riding += 1

        measured = self.measured_seconds
        hours = measured / 3600
        in_system_now = (self.spawned - self.arrived) if self._in_system_at_start is not None else 0
        backlog_change = in_system_now - (self._in_system_at_start or 0)
        # "Falling behind" needs enough time to tell a trend from noise, and a real pile-up.
        overloaded = measured >= 600 and backlog_change >= max(20, 0.1 * len(waits))

        open_ages = [now - c.pressed_at for c in open_calls]
        fleet = CarStats()
        cars = []
        for car in elevators:
            stats = self.cars.get(car.id, CarStats())
            fleet.add(stats)
            cars.append({"id": car.id, "capacity": car.capacity, **stats.to_dict()})

        timeline = []
        indices = set(self._buckets) | set(bucket_waits) | set(bucket_delivered)
        for index in range(min(indices), max(indices) + 1) if indices else ():
            bucket = self._buckets.get(index, _Bucket())
            dist = distribution(bucket_waits.get(index, ()))
            end = (index + 1) * self.bucket_seconds
            still_waiting = bucket_still_waiting.get(index, 0)
            complete = end <= now + 1e-6
            timeline.append({
                "t0": index * self.bucket_seconds,
                "t1": end,
                "complete": complete,
                # Right after an interval ends, its latest arrivals are still waiting and count
                # with short waits-so-far, which drags its numbers down. It is "settled" once
                # they have all boarded - or after one more interval, so that an overloaded
                # building (where they may never board) still shows up.
                "settled": complete and (still_waiting == 0 or now >= end + self.bucket_seconds),
                "still_waiting": still_waiting,
                "arrived": dist["n"],
                "delivered": bucket_delivered.get(index, 0),
                "wait_mean": dist["mean"],
                "wait_p95": dist["p95"],
                "avg_waiting": _ratio(bucket.waiting_seconds, bucket.seconds, 2),
                "peak_waiting": bucket.peak_waiting,
            })

        by_floor = []
        for floor in floors:
            dist = distribution(floor_waits[floor.index])
            by_floor.append({
                "i": floor.index, "label": floor.label, "name": floor.name,
                "kind": floor.kind.name.lower(),
                "trips_from": dist["n"], "trips_to": floor_to[floor.index],
                "wait_mean": dist["mean"], "wait_p95": dist["p95"], "wait_max": dist["max"],
            })

        return {
            "time": round(now, 1),
            "warmup": self.warmup,
            "warming_up": not self._recording(now),
            "measured_seconds": round(measured, 1),
            "long_wait": self.long_wait,
            "bucket_seconds": self.bucket_seconds,
            "passengers": {
                "measured": len(waits),
                "waiting": waiting,
                "riding": riding,
                "delivered": delivered,
                "wait": distribution(waits),
                "ride": distribution(rides),
                "trip": distribution(trips),
                "long_waits": int(long_waits),
                "long_wait_share": _ratio(long_waits, len(waits)),
                "left_behind_people": left_people,
                "left_behind_events": left_events,
                "stops_per_ride": round(fmean(stops), 2) if stops else None,
            },
            "by_direction": {
                d.name.lower(): {
                    "people": len(v["waits"]),
                    "wait_mean": distribution(v["waits"])["mean"],
                    "wait_p95": distribution(v["waits"])["p95"],
                    "trip_mean": distribution(v["trips"])["mean"],
                }
                for d, v in by_dir.items()
            },
            "hall_calls": {
                "answered": len(self._call_responses),
                "response": distribution(self._call_responses),
                "open": len(open_ages),
                "oldest_open": round(max(open_ages), 1) if open_ages else None,
            },
            "system": {
                "arrivals_per_hour": _ratio(len(waits), hours, 1),
                "delivered_per_hour": _ratio(delivered, hours, 1),
                "avg_waiting": _ratio(self._waiting_seconds, measured, 2),
                "peak_waiting": self._peak_waiting,
                "avg_riding": _ratio(self._riding_seconds, measured, 2),
                "peak_delivered_per_interval": max(
                    (b["delivered"] for b in timeline if b["complete"]), default=None),
                "backlog_change": backlog_change,
                "overloaded": overloaded,
            },
            "fleet": fleet.to_dict(),
            "cars": cars,
            "timeline": timeline,
            "by_floor": by_floor,
        }
