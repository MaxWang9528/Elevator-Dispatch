"""Reading a saved run back in, for the Import tab.

Two kinds of file can be imported, and both come back as a full report - the same shape the
live Metrics tab shows (see MetricsCollector.report), so the Import tab can show it identically:

  report JSON  (Download report, `--json`) is already a report; it is checked and returned.
  trips CSV    (Download trips, `--trips`) is replayed: every person's button press, boarding
               and arrival goes through a MetricsCollector, tick by tick, exactly as during the
               run. Passenger figures, queues, the charts over time and by floor come out the
               same as the live report had them. A trips file doesn't record the cars or the
               hall buttons, so those parts of the report are empty, and `source.missing` says so.

`source` describes where a report came from: {"name", "kind": "report" | "trips", "missing", "note"}.
"""

from __future__ import annotations

import csv
import io
import json
import math
from types import SimpleNamespace
from typing import Any

from .building import Floor
from .clock import DAY, format_time
from .dispatchers import DISPATCHERS, RETIRED_DISPATCHERS
from .enums import FloorKind
from .metrics import CarStats, MetricsCollector, distribution
from .person import Arrived, AtFloor, InCar, Person

MAX_BYTES = 50 * 1024 * 1024
DT = 0.1                         # the simulation's tick, used to replay a trips file
TRIP_REQUIRED = ("from_floor", "to_floor", "spawn_time", "board_time", "arrive_time")
REPORT_REQUIRED = ("passengers", "by_direction", "hall_calls", "system", "fleet", "cars", "timeline", "by_floor", "run")


class ImportFileError(ValueError):
    """The file can't be read as a saved run. The message says why, for the person importing it."""


def report_from_file(name: str, data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise ImportFileError(f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError(f"{name} isn't a text file. Import a report (JSON) or a trips file (CSV).") from None
    if not text.strip():
        raise ImportFileError(f"{name} is empty.")
    if name.lower().endswith(".json") or text.lstrip().startswith("{"):
        try:
            report = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImportFileError(f"{name} isn't valid JSON ({exc.msg}, line {exc.lineno}).") from None
        return check_report(name, report)
    return report_from_trips(name, text)


# --- report JSON ------------------------------------------------------------------------------
def check_report(name: str, report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ImportFileError(f"{name} doesn't look like a report from the simulator.")
    missing = [k for k in REPORT_REQUIRED if k not in report]
    if missing:
        raise ImportFileError(f"{name} doesn't look like a report from the simulator: it has no "
                              f"{', '.join(missing)} section{'s' if len(missing) > 1 else ''}.")
    report["source"] = {"name": name, "kind": "report", "missing": [], "note": ""}
    return report


# --- trips CSV --------------------------------------------------------------------------------
def report_from_trips(name: str, text: str) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(text))
    columns = reader.fieldnames or []
    missing = [c for c in TRIP_REQUIRED if c not in columns]
    if missing:
        raise ImportFileError(f"{name} doesn't look like a trips file from the simulator: it has no "
                              f"{', '.join(missing)} column{'s' if len(missing) > 1 else ''}.")
    people, measured_flags, clocks = [], [], []
    run_row: dict[str, str] = {}
    for line, row in enumerate(reader, start=2):
        people.append(_person(name, line, len(people), row))
        measured_flags.append((row.get("measured") or "").strip())
        clocks.append((row.get("spawn_clock") or "").strip())
        run_row = row
    if not people:
        raise ImportFileError(f"{name} has no trips in it.")

    # Files from this version carry the run's settings on every row; older ones don't, so
    # those are worked out from the trips (the warm-up, the end) or take the defaults.
    def setting(key: str) -> float | None:
        try:
            return float(run_row.get(key) or "")
        except ValueError:
            return None

    n_floors = max(max(p.origin_floor, p.requested_floor) for p in people) + 1
    floors = [Floor(i, str(i + 1), FloorKind.RESIDENTIAL) for i in range(n_floors)]
    last = max(max(p.spawn_time, p.board_time or 0, p.arrive_time or 0) for p in people)
    end = setting("run_time") or math.ceil(last / DT) * DT
    warmup = setting("warmup")
    if warmup is None:
        warmup = _warmup(people, measured_flags)
    if not any(p.spawn_time >= warmup - 1e-9 for p in people):
        raise ImportFileError(f"{name} has no measured trips (all of them are from the warm-up).")

    metrics = MetricsCollector(warmup=warmup, long_wait=setting("long_wait") or 60.0,
                               bucket_seconds=setting("chart_interval") or 300.0)
    _replay(metrics, people, end)
    report = metrics.report(end, people, floors, [], [])

    # The cars and hall buttons aren't in a trips file: show those parts as unknown, not as zero.
    report["fleet"] = {key: None for key in CarStats().to_dict()}
    report["cars"] = []
    report["hall_calls"] = {"answered": None, "response": distribution([]), "open": None, "oldest_open": None}

    day_start = _day_start(people, clocks)
    recorded = (run_row.get("dispatcher") or "").strip()
    recorded = RETIRED_DISPATCHERS.get(recorded, recorded)
    dispatcher = recorded if recorded in DISPATCHERS else _dispatcher_from_name(name)
    seed = setting("seed")
    if day_start is not None:
        description = f"{format_time(day_start)} to {format_time(day_start + end)}"
    else:
        description = "a run without a time of day"
    report["run"] = {
        "dispatcher": dispatcher,
        "dispatcher_label": DISPATCHERS[dispatcher].label if dispatcher else None,
        "seed": None if seed is None else int(seed),
        "day_start": day_start,
        "description": description,
        "floors": n_floors,
        "elevators": len({p.car_id for p in people if p.car_id is not None}),
    }
    report["source"] = {
        "name": name, "kind": "trips", "missing": ["cars", "hall_calls"],
        "note": "This is a trips file: it has every passenger but doesn't record the elevators or the "
                "hall buttons, so those figures are blank. Import the report (JSON) from the same run "
                "for them." + ("" if dispatcher else " The algorithm isn't recorded in a trips file either."),
    }
    return report


def _person(name: str, line: int, index: int, row: dict[str, str]) -> Person:
    def num(key: str) -> float | None:
        value = (row.get(key) or "").strip()
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            raise ImportFileError(f"{name}, line {line}: {key} must be a number, not {value!r}.") from None

    def floor(key: str) -> int:
        value = num(key)
        if value is None or value < 1 or not value.is_integer():
            raise ImportFileError(f"{name}, line {line}: {key} must be a floor number from 1 up.")
        return int(value) - 1

    spawn = num("spawn_time")
    if spawn is None:
        raise ImportFileError(f"{name}, line {line}: spawn_time is missing.")
    origin, dest = floor("from_floor"), floor("to_floor")
    if origin == dest:
        raise ImportFileError(f"{name}, line {line}: from_floor and to_floor are the same.")
    board, arrive = num("board_time"), num("arrive_time")
    car = (row.get("car") or "").strip()
    person = Person(
        id=index, origin_floor=origin, requested_floor=dest, spawn_time=spawn,
        location=Arrived(dest) if arrive is not None else InCar(int(car or 0)) if board is not None else AtFloor(origin),
        board_time=board, arrive_time=arrive,
        car_id=int(car) if car else None,
        stops_en_route=int(num("stops_en_route") or 0),
        left_behind=int(num("left_behind") or 0),
        resident_id=int(num("resident")) if num("resident") is not None else None,
        purpose=(row.get("purpose") or "").strip(),
    )
    return person


def _warmup(people: list[Person], flags: list[str]) -> float:
    """The warm-up the run had, worked out from the `measured` column (0 = before it)."""
    if not any(flags):
        return 0.0
    before = [p.spawn_time for p, f in zip(people, flags) if f == "0"]
    after = [p.spawn_time for p, f in zip(people, flags) if f != "0"]
    if not after:
        return math.inf
    first = min(after)
    latest_before = max(before, default=-math.inf)
    whole_minute = math.floor(first / 60) * 60       # warm-ups are set in whole seconds, usually minutes
    return whole_minute if whole_minute > latest_before else first


def _replay(metrics: MetricsCollector, people: list[Person], end: float) -> None:
    """Feed every press, boarding and arrival to the collector in time order, ticking as the
    simulation does, so queue figures integrate exactly as they did live."""
    events: list[tuple[float, int, Person]] = []
    for p in people:
        events.append((p.spawn_time, 0, p))
        if p.board_time is not None:
            events.append((p.board_time, 1, p))
        if p.arrive_time is not None:
            events.append((p.arrive_time, 2, p))
    events.sort(key=lambda e: (e[0], e[1]))
    cars: dict[int | None, SimpleNamespace] = {}
    i = 0
    for tick in range(1, round(end / DT) + 1):
        now = tick * DT
        while i < len(events) and events[i][0] <= now + 1e-6:
            t, kind, p = events[i]
            if kind == 0:
                metrics.on_spawn(p)
            else:
                car = cars.setdefault(p.car_id, SimpleNamespace(id=p.car_id))
                if kind == 1:
                    metrics.on_board(p, car, t)
                else:
                    metrics.on_arrive(p, car, t)
            i += 1
        metrics.on_tick(now, DT)


def _day_start(people: list[Person], clocks: list[str]) -> float | None:
    for p, clock in zip(people, clocks):
        if clock:
            parts = clock.split(":")
            try:
                h, m, s = (int(x) for x in (parts + ["0"])[:3])
            except ValueError:
                continue
            return round(h * 3600 + m * 60 + s - p.spawn_time) % DAY
    return None


def _dispatcher_from_name(name: str) -> str | None:
    """Downloads are named like lookahead_hours_0600-2000_trips.csv: recover the algorithm."""
    first = name.rsplit("/", 1)[-1].split("_", 1)[0]
    first = RETIRED_DISPATCHERS.get(first, first)
    return first if first in DISPATCHERS else None
