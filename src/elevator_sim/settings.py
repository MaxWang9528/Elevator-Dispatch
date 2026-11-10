"""User-editable settings, saved to a JSON file so they survive restarts.

To add a setting, add a field to `Settings` using `setting(...)`. The web Settings tab is built
from that metadata, and settings files saved before the field existed still load (the missing
field just takes its default).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .building import Building, Floor
from .clock import DAY, RunPlan, parse_time
from .dispatchers import DISPATCHERS, RETIRED_DISPATCHERS, make_dispatcher
from .elevator import Elevator
from .enums import FloorKind
from .metrics import MetricsCollector
from .simulation import Simulation
from .traffic import ResidentTraffic, RushHour, VisitorTraffic

log = logging.getLogger(__name__)

SETTINGS_VERSION = 1

# The rush buttons run from this long before a rush starts to this long after it ends, so a
# run shows the baseline on both sides and the queues have time to clear.
RUSH_MARGIN = 30 * 60

RUSH_NAMES = ("morning", "evening")

# Floor types that count as ways out of the building: where rush-hour commutes go to and from.
EXIT_KINDS = ("lobby", "parking")

# Floor types a special floor can have, with the names shown in the UI.
SPECIAL_KINDS: dict[str, str] = {
    "lobby": "Lobby",
    "parking": "Parking / transit",
    "retail": "Retail",
    "amenity": "Amenity",
}


class SettingsError(ValueError):
    """Invalid settings. `field` names the offending input, e.g. "floors" or "special_floors.2.share"."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def setting(default: int | float, *, group: str, label: str, minimum: float, maximum: float,
            step: float = 1, unit: str = "", help: str = "", enabled_by: str | None = None,
            disabled_by: str | None = None, error_name: str | None = None,
            office_label: str | None = None, office_help: str | None = None) -> Any:
    """A number. `enabled_by` names an on/off setting; while that is off the UI greys this out.
    `disabled_by` is the reverse: greyed out while that setting is on.
    `error_name` replaces the label in error messages when the label alone is ambiguous.
    `office_label` and `office_help` replace the label and help in an office building."""
    return field(default=default, metadata={
        "type": "int" if type(default) is int else "float",
        "group": group, "label": label, "min": minimum, "max": maximum,
        "step": step, "unit": unit, "help": help, "enabled_by": enabled_by,
        "disabled_by": disabled_by, "error_name": error_name or label,
        "office_label": office_label, "office_help": office_help,
    })


def choice(default: str, *, group: str, label: str, options: dict[str, str], help: str = "") -> Any:
    """One of a fixed set of values. `options` maps each value to the name shown for it."""
    return field(default=default, metadata={
        "type": "choice", "group": group, "label": label, "help": help, "error_name": label,
        "options": [{"value": v, "label": name} for v, name in options.items()],
    })


def floor_list(default: str, *, group: str, label: str, help: str = "", placeholder: str = "") -> Any:
    """A list of floor numbers and ranges, kept tidy: "2-19, 91-99". Empty means none."""
    return field(default=default, metadata={"type": "floors", "group": group, "label": label,
                                            "help": help, "error_name": label, "placeholder": placeholder})


def switch(default: bool, *, group: str, label: str, help: str = "") -> Any:
    """An on/off setting."""
    return field(default=default, metadata={"type": "bool", "group": group, "label": label,
                                            "help": help, "error_name": label})


def time_of_day(default: str, *, group: str, label: str, help: str = "",
                enabled_by: str | None = None, error_name: str | None = None) -> Any:
    """A time of day, kept as "HH:MM" (24-hour)."""
    return field(default=default, metadata={
        "type": "time", "group": group, "label": label, "help": help, "enabled_by": enabled_by,
        "error_name": error_name or label,
    })


# Shown under a group's heading on the Settings tab.
_RUSH_HELP = ("Used by runs that have a time of day: the rush buttons and the start and stop "
              "times on the Simulation tab. Steady traffic has no commutes.")
GROUP_HELP: dict[str, str] = {
    "Elevators": "Cars are arranged in groups (banks). Each group has its own hall buttons and "
                 "serves its own floors, so a tall building can have local and express banks. "
                 "A trip uses one car, so it needs a group that serves both of its floors.",
    "Residents": "Every trip is made by a resident, and everyone who goes somewhere comes back, "
                 "so traffic follows from how many people live here. A typical resident makes "
                 "3 to 5 elevator trips a day.",
    "Morning rush": "Residents leaving for the day. " + _RUSH_HELP,
    "Evening rush": "Residents coming home. " + _RUSH_HELP,
    "Visitors": "People from outside the building who come to see one floor, like an observatory. "
                "They ride up from their entrance in groups of 1 to 5, stay a while, and ride back "
                "down together, never after closing time. Only in runs that have a time of day.",
    "Lunch": "People who are home go out for lunch to a Lobby, Parking / transit or Retail floor "
             "(split by those floors' shares) and come back after about 45 minutes. "
             "Only in runs that have a time of day.",
}
# The same groups in an office building (Building -> Office building): (title, help).
GROUP_OFFICE: dict[str, tuple[str, str]] = {
    "Residents": ("People", "Everyone who works here. Each has a floor they work on, spread evenly "
                            "over the floors that aren't special floors."),
    "Morning rush": ("Morning rush", "People arriving for work, up from the Lobby and Parking / "
                                     "transit floors. " + _RUSH_HELP),
    "Evening rush": ("Evening rush", "People leaving work, down to the floor they came in by. "
                                     + _RUSH_HELP),
    "Lunch": ("Lunch", "People at work go out for lunch to a Lobby, Parking / transit or Retail "
                       "floor (split by those floors' shares) and come back after about 45 "
                       "minutes. Only in runs that have a time of day."),
}

MAX_GROUPS = 20
MAX_GROUP_CARS = 40
MAX_CARS = 100


@dataclass
class SpecialFloor:
    floor: int      # floor number as people see it (1 = ground)
    name: str
    kind: str       # a key of SPECIAL_KINDS
    share: float    # percent of residents' outings that go here


@dataclass
class ElevatorGroup:
    """A bank of identical cars with its own hall buttons."""
    name: str
    cars: int
    floors: str      # floors served, as people number them: "all", or like "1, 20-38"
    capacity: int    # people per car
    speed: float     # top speed, floors per second

    def served(self, n_floors: int) -> frozenset[int] | None:
        """Floor indices (0-based) this group serves, or None for every floor."""
        floors = _floor_numbers(self.floors)
        if floors is None or floors >= set(range(1, n_floors + 1)):
            return None
        return frozenset(f - 1 for f in floors if f <= n_floors)


def default_elevator_groups() -> list[ElevatorGroup]:
    """Marquee at Block 37: one bank of 6 cars."""
    return [ElevatorGroup("Elevators", 6, "all", 9, 2.5)]


def default_special_floors() -> list[SpecialFloor]:
    """Marquee at Block 37. Shares are estimates, not measurements."""
    return [
        SpecialFloor(1, "Lobby", "lobby", 58),
        SpecialFloor(4, "Parking, mall, CTA Red and Blue lines", "parking", 25),
        SpecialFloor(5, "Common areas", "amenity", 5),
        SpecialFloor(38, "Roof deck and gym", "amenity", 7),
    ]


@dataclass
class Settings:
    # --- Building ---
    floors: int = setting(38, group="Building", label="Floors served", minimum=2, maximum=120,
                          help="Numbered from 1. Leave out levels the main elevators can't reach, "
                               "like garage basements reached from another elevator.")
    closed_floors: str = floor_list(
        "", group="Building", label="Floors with no stops", placeholder="none",
        help="Floors nobody lives or works on and no elevator has to stop at, like mechanical "
             "floors or floor numbers the building skips. Floor numbers and ranges, like 2-19, 91-99.")
    office: bool = switch(False, group="Building", label="Office building",
                          help="Swaps the rushes: people arrive in the morning rush, up from the "
                               "Lobby and Parking / transit floors, and leave in the evening rush. "
                               "Residents become the people who work here, outside working hours "
                               "they are out of the building, and outings follow working hours.")
    # --- Elevators (the groups themselves are elevator_groups, below) ---
    acceleration: float = setting(0.35, group="Elevators", label="Acceleration", minimum=0.05,
                                  maximum=5, step=0.05, unit="floors/s²",
                                  help="Used to speed up and to brake. Comfortable cars accelerate "
                                       "at about 1 m/s², roughly 0.3 floors/s² with 3.2 m floors. "
                                       "Lower values make short hops between nearby floors slower.")
    door_time: float = setting(2.0, group="Elevators", label="Door opening time", minimum=0.5,
                               maximum=10, step=0.1, unit="s", help="Closing takes the same time.")
    dwell_time: float = setting(2.0, group="Elevators", label="Minimum time doors stay open",
                                minimum=0, maximum=20, step=0.1, unit="s")
    transfer_time: float = setting(1.0, group="Elevators", label="Time for one person to get on or off",
                                   minimum=0.2, maximum=5, step=0.1, unit="s")
    # --- Dispatching ---
    dispatcher: str = choice(
        "lookahead", group="Dispatching", label="Algorithm",
        options={key: info.label for key, info in DISPATCHERS.items()},
        help=" ".join(f"{info.label}: {info.description}" for info in DISPATCHERS.values()))
    # --- Residents ---
    residents: int = setting(1000, group="Residents", label="Residents", minimum=1, maximum=20000,
                             unit="people",
                             help="Spread evenly over the residential floors. The default is an "
                                  "estimate for Marquee at Block 37.",
                             office_label="People who work here",
                             office_help="Spread evenly over the floors that aren't special floors.")
    outings: float = setting(1.5, group="Residents", label="Outings per resident per day",
                             minimum=0, maximum=50, step=0.1, unit="per day",
                             help="Trips away from home other than commuting: errands, the gym, "
                                  "the mail room, visiting a neighbor. An average over all residents: "
                                  "those home more make more of them. Each one is two elevator "
                                  "trips, there and back. Where they go follows the special "
                                  "floors' shares. The slider on the Simulation tab changes it for "
                                  "the current run only.",
                             office_label="Outings per person per day",
                             office_help="Trips from someone's floor while they're at work: "
                                         "meetings, a coffee, the mail room. On average each "
                                         "person who comes in makes this many, spread over their "
                                         "hours at work. Each one is two elevator trips, there "
                                         "and back, and they follow the special floors' shares. "
                                         "Commutes and lunch are on top of these.")
    # --- Morning rush ---
    morning_enabled: bool = switch(True, group="Morning rush", label="Simulate the morning rush")
    morning_start: str = time_of_day("07:00", group="Morning rush", label="Starts",
                                   error_name="Morning rush start time",
                                     enabled_by="morning_enabled",
                                     help="The first commuters set off.")
    morning_peak: str = time_of_day("08:30", group="Morning rush", label="Peaks",
                                   error_name="Morning rush peak time",
                                    enabled_by="morning_enabled")
    morning_end: str = time_of_day("10:00", group="Morning rush", label="Ends",
                                   error_name="Morning rush end time",
                                   enabled_by="morning_enabled",
                                   help="The last commuters have set off.")
    morning_share: float = setting(
        65, group="Morning rush", label="Residents who leave", minimum=0, maximum=100,
        unit="%", enabled_by="morning_enabled", error_name="Morning rush share of residents",
        help="Share of residents who leave for work or school, through a Lobby or Parking / "
             "transit floor (split by those floors' shares). Most leave around the peak.",
        office_label="People who arrive",
        office_help="Share of the people who work here who come in, through a Lobby or Parking / "
                    "transit floor (split by those floors' shares). The rest aren't in today.")
    # --- Evening rush ---
    evening_enabled: bool = switch(True, group="Evening rush", label="Simulate the evening rush")
    evening_start: str = time_of_day("16:00", group="Evening rush", label="Starts",
                                   error_name="Evening rush start time",
                                     enabled_by="evening_enabled",
                                     help="The first commuters set off.")
    evening_peak: str = time_of_day("17:30", group="Evening rush", label="Peaks",
                                   error_name="Evening rush peak time",
                                    enabled_by="evening_enabled")
    evening_end: str = time_of_day("19:00", group="Evening rush", label="Ends",
                                   error_name="Evening rush end time",
                                   enabled_by="evening_enabled",
                                   help="The last commuters have set off.")
    evening_share: float = setting(
        55, group="Evening rush", label="Residents who come home", minimum=0, maximum=100,
        unit="%", enabled_by="evening_enabled", error_name="Evening rush share of residents",
        help="Share of residents who come home in this rush, through the exit they left by. "
             "Commuters who don't (more left than come home here) come home later in the "
             "evening. With this rush off, they come home spread over the afternoon and evening.",
        office_label="People who leave",
        office_help="Share of the people who work here who leave in this rush, through the floor "
                    "they came in by. Anyone who came in and doesn't leave now works late and "
                    "leaves later in the evening. With this rush off, they leave spread over "
                    "the afternoon.")
    # --- Lunch ---
    lunch_enabled: bool = switch(False, group="Lunch", label="Simulate a lunch peak")
    lunch_start: str = time_of_day("11:30", group="Lunch", label="Starts", enabled_by="lunch_enabled",
                                   error_name="Lunch start time", help="The first people go out.")
    lunch_peak: str = time_of_day("12:15", group="Lunch", label="Peaks", enabled_by="lunch_enabled",
                                  error_name="Lunch peak time")
    lunch_end: str = time_of_day("13:30", group="Lunch", label="Ends", enabled_by="lunch_enabled",
                                 error_name="Lunch end time", help="The last people have gone out.")
    lunch_share: float = setting(
        40, group="Lunch", label="People who go out", minimum=0, maximum=100, unit="%",
        enabled_by="lunch_enabled", error_name="Share of people who go out for lunch",
        help="Share of everyone who goes out for lunch, if they are home when it's time.",
        office_help="Share of the people who work here who go out for lunch, if they're at "
                    "work when it's time.")
    # --- Visitors ---
    visitors_enabled: bool = switch(False, group="Visitors", label="Simulate visitors")
    visitors_per_day: int = setting(
        2000, group="Visitors", label="Visitors a day", minimum=0, maximum=50000, unit="people",
        enabled_by="visitors_enabled", help="On average; each day varies a little.")
    visitor_entrance: int = setting(
        1, group="Visitors", label="Visitors come in on floor", minimum=1, maximum=1000,
        enabled_by="visitors_enabled", error_name="Visitors' entrance floor",
        help="Where they enter the building and leave it again.")
    visitor_floor: int = setting(
        38, group="Visitors", label="Visitors go up to floor", minimum=1, maximum=1000,
        enabled_by="visitors_enabled", error_name="The floor visitors go up to")
    visitor_exit_floor: int = setting(
        38, group="Visitors", label="Visitors ride down from floor", minimum=1, maximum=1000,
        enabled_by="visitors_enabled", error_name="The floor visitors ride down from",
        help="The same floor, or one they walk down to on their way out (an observatory's "
             "lower level, say).")
    visitors_open: str = time_of_day("09:00", group="Visitors", label="Opens", enabled_by="visitors_enabled",
                                     error_name="Visitors' opening time", help="The first visitors arrive.")
    visitors_peak: str = time_of_day("15:00", group="Visitors", label="Busiest", enabled_by="visitors_enabled",
                                     error_name="Visitors' busiest time")
    visitors_last_entry: str = time_of_day("20:15", group="Visitors", label="Last entry",
                                           enabled_by="visitors_enabled", error_name="Visitors' last entry",
                                           help="The last visitors arrive.")
    visitors_close: str = time_of_day("21:00", group="Visitors", label="Closes", enabled_by="visitors_enabled",
                                      error_name="Visitors' closing time",
                                      help="Everyone has gone back down by now.")
    visit_minutes: int = setting(
        75, group="Visitors", label="Average visit", minimum=10, maximum=600, unit="min",
        enabled_by="visitors_enabled", error_name="Average visit length",
        help="From the whole group getting there to riding back down. Visits last at least half "
             "of this (at most 30 minutes); a few last much longer.")
    # --- Metrics ---
    warmup: int = setting(300, group="Metrics", label="Warm-up period", minimum=0, maximum=86400,
                          unit="s",
                          help="Stats ignore people who appear before this much simulated time has "
                               "passed, so the start from an empty building doesn't skew them.")
    long_wait: int = setting(60, group="Metrics", label="Long wait threshold", minimum=5,
                             maximum=3600, unit="s",
                             help="Waits longer than this count toward the share of long waits.")
    bucket_minutes: int = setting(5, group="Metrics", label="Chart interval", minimum=1, maximum=60,
                                  unit="min",
                                  help="Charts over time group people by when they appeared, in "
                                       "intervals this long.")
    # --- Simulation ---
    random_seed: bool = switch(True, group="Simulation", label="New random seed on every restart",
                               help="Each restart, new run and save picks a fresh seed, so no two runs "
                                    "are alike. The seed in use shows on the Simulation and Metrics "
                                    "tabs: to repeat a run, turn this off and enter that seed. "
                                    "The command-line tools always use the seed below.")
    seed: int = setting(1, group="Simulation", label="Random seed", minimum=0, maximum=2**31 - 1,
                        disabled_by="random_seed",
                        help="The same seed and settings always produce the same run.")

    special_floors: list[SpecialFloor] = field(default_factory=default_special_floors)
    elevator_groups: list[ElevatorGroup] = field(default_factory=default_elevator_groups)

    @property
    def elevators(self) -> int:
        """Cars in every group."""
        return sum(g.cars for g in self.elevator_groups)

    # --- conversion ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Settings:
        """Validate and build. Unknown keys are ignored; missing keys keep their defaults."""
        if not isinstance(data, dict):
            raise SettingsError("Settings must be a JSON object.")
        if data.get("dispatcher") in RETIRED_DISPATCHERS:
            data = {**data, "dispatcher": RETIRED_DISPATCHERS[data["dispatcher"]]}
        s = cls()
        for f in fields(cls):
            if not f.metadata or f.name not in data:
                continue
            setattr(s, f.name, _parse_value(data[f.name], f.name, f.metadata))
        if "special_floors" in data:
            s.special_floors = _parse_special_floors(data["special_floors"])
        if "elevator_groups" in data:
            s.elevator_groups = _parse_elevator_groups(data["elevator_groups"])
        elif any(k in data for k in ("elevators", "capacity", "speed")):
            s.elevator_groups = [_legacy_group(data)]     # saved before elevator groups existed
        s.validate()
        return s

    @classmethod
    def schema(cls) -> list[dict[str, Any]]:
        """Field descriptions the web UI renders into a form."""
        return [{"key": f.name, "default": f.default, **f.metadata} for f in fields(cls) if f.metadata]

    # --- rush hours ---------------------------------------------------------------------
    def rush_hour(self, name: str) -> RushHour:
        """The "morning" or "evening" rush as configured, whether or not it is switched on."""
        if name not in RUSH_NAMES:
            raise ValueError(f"no rush called {name!r}")
        get = lambda key: getattr(self, f"{name}_{key}")  # noqa: E731
        # Residents leave in the morning; office workers leave in the evening.
        return RushHour(
            name=name, start=parse_time(get("start")), peak=parse_time(get("peak")),
            end=parse_time(get("end")), share=get("share") / 100,
            leaving=(name == "morning") != self.office,
        )

    def lunch_hour(self, even_if_off: bool = False) -> RushHour | None:
        """The lunch peak, if it is switched on (or as configured, with `even_if_off`)."""
        if not (self.lunch_enabled or even_if_off):
            return None
        return RushHour(name="lunch", start=parse_time(self.lunch_start), peak=parse_time(self.lunch_peak),
                        end=parse_time(self.lunch_end), share=self.lunch_share / 100, leaving=True)

    def rush_enabled(self, name: str) -> bool:
        return bool(getattr(self, f"{name}_enabled"))

    def rush_hours(self) -> list[RushHour]:
        """The rushes that are switched on."""
        return [self.rush_hour(name) for name in RUSH_NAMES if self.rush_enabled(name)]

    def rush_plan(self, name: str) -> RunPlan:
        """A run around one rush: from RUSH_MARGIN before it starts to RUSH_MARGIN after it ends."""
        rush = self.rush_hour(name)
        if not self.rush_enabled(name):
            raise SettingsError(f"The {name} rush is turned off in Settings.", f"{name}_enabled")
        start = max(0, int(rush.start) - RUSH_MARGIN)
        stop = min(DAY, int(rush.end) + RUSH_MARGIN)
        return RunPlan(name, start, stop - start)

    def lunch_plan(self) -> RunPlan:
        """A run around the lunch peak, from RUSH_MARGIN before it to RUSH_MARGIN after it."""
        lunch = self.lunch_hour(even_if_off=True)
        start = max(0, int(lunch.start) - RUSH_MARGIN)
        stop = min(DAY, int(lunch.end) + RUSH_MARGIN)
        return RunPlan("lunch", start, stop - start)

    def validate(self) -> None:
        for name in (*RUSH_NAMES, "lunch"):
            what = "lunch peak" if name == "lunch" else f"{name} rush"
            start, peak, end = (parse_time(getattr(self, f"{name}_{key}")) for key in ("start", "peak", "end"))
            if not start < end:
                raise SettingsError(f"The {what} has to end after it starts.", f"{name}_end")
            if not start < peak < end:
                raise SettingsError(f"The {what} has to peak between its start and its end.",
                                    f"{name}_peak")
        closed = self.closed_numbers()
        beyond = sorted(f for f in closed if f > self.floors)
        if beyond:
            raise SettingsError(f"Floor {beyond[0]} doesn't exist; floors go from 1 to {self.floors}.",
                                "closed_floors")
        seen: dict[int, int] = {}
        for i, sf in enumerate(self.special_floors):
            if sf.floor in closed:
                raise SettingsError(f"Floor {sf.floor} is a special floor, so it can't be a floor with "
                                    "no stops.", f"special_floors.{i}.floor")
            if not 1 <= sf.floor <= self.floors:
                raise SettingsError(
                    f"Special floor {sf.floor} doesn't exist; floors go from 1 to {self.floors}.",
                    f"special_floors.{i}.floor")
            if sf.floor in seen:
                raise SettingsError(f"Floor {sf.floor} is listed twice in special floors.",
                                    f"special_floors.{i}.floor")
            seen[sf.floor] = i
        total = sum(sf.share for sf in self.special_floors)
        if total > 100 + 1e-9:
            raise SettingsError(f"Special floor shares add up to {total:g}%; the most is 100%.",
                                "special_floors.share_total")
        if len(self.special_floors) + len(closed) >= self.floors:
            raise SettingsError("At least one floor has to be residential.", "special_floors")
        self._validate_groups()
        if self.visitors_enabled:
            self._validate_visitors(closed)

    def visitor_hours(self) -> RushHour:
        """When visitors arrive: from opening to last entry, most around the busiest time."""
        return RushHour(name="visitors", start=parse_time(self.visitors_open),
                        peak=parse_time(self.visitors_peak), end=parse_time(self.visitors_last_entry),
                        share=1.0, leaving=False)

    def _validate_visitors(self, closed: set[int]) -> None:
        opens, peak, last, close = (parse_time(getattr(self, f"visitors_{k}"))
                                    for k in ("open", "peak", "last_entry", "close"))
        if not opens < last:
            raise SettingsError("Visitors' last entry has to be after opening time.", "visitors_last_entry")
        if not opens < peak < last:
            raise SettingsError("Visitors' busiest time has to be between opening and last entry.",
                                "visitors_peak")
        if not last <= close:
            raise SettingsError("Closing time can't be before the last entry.", "visitors_close")
        floors = {"visitor_entrance": self.visitor_entrance, "visitor_floor": self.visitor_floor,
                  "visitor_exit_floor": self.visitor_exit_floor}
        for key, floor in floors.items():
            if floor > self.floors:
                raise SettingsError(f"Floor {floor} doesn't exist; floors go from 1 to {self.floors}.", key)
            if floor in closed:
                raise SettingsError(f"Floor {floor} is a floor with no stops.", key)
        if self.visitor_entrance in (self.visitor_floor, self.visitor_exit_floor):
            raise SettingsError("Visitors' entrance has to be a different floor from the one they visit.",
                                "visitor_entrance")
        reach = self.reachable()
        for key, floor in (("visitor_floor", self.visitor_floor), ("visitor_exit_floor", self.visitor_exit_floor)):
            if floor - 1 not in reach[self.visitor_entrance - 1]:
                raise SettingsError(f"The elevators don't connect floor {self.visitor_entrance} and "
                                    f"floor {floor}, even changing cars, so visitors can't make that trip.", key)

    def closed_numbers(self) -> set[int]:
        """Floors with no stops, as people number them."""
        return (_floor_numbers(self.closed_floors) or set()) if self.closed_floors else set()

    def _validate_groups(self) -> None:
        if not self.elevator_groups:
            raise SettingsError("Add at least one elevator group.", "elevator_groups")
        if self.elevators > MAX_CARS:
            raise SettingsError(f"The groups have {self.elevators} cars in all; the most is {MAX_CARS}.",
                                "elevator_groups.cars_total")
        every = set(range(self.floors))
        served_by: dict[int, list[frozenset[int]]] = {f: [] for f in every}
        for i, g in enumerate(self.elevator_groups):
            numbers = _floor_numbers(g.floors)
            if numbers is not None:
                beyond = sorted(f for f in numbers if f > self.floors)
                if beyond:
                    raise SettingsError(
                        f"{g.name} serves floor {beyond[0]}, but floors go from 1 to {self.floors}.",
                        f"elevator_groups.{i}.floors")
                if len(numbers) < 2:
                    raise SettingsError(f"{g.name} has to serve at least two floors.",
                                        f"elevator_groups.{i}.floors")
            floors = frozenset(every) if numbers is None else frozenset(f - 1 for f in numbers)
            for f in floors:
                served_by[f].append(floors)
        closed = {f - 1 for f in self.closed_numbers()}
        unserved = [f + 1 for f, groups in served_by.items() if not groups and f not in closed]
        if unserved:
            them = "it" if len(unserved) == 1 else "them"
            raise SettingsError(f"No elevator group serves {_describe_floors(unserved)}. "
                                f"Add {them} to a group, or lower the number of floors.", "elevator_groups")
        # Every home floor needs a way to a way out (see make_simulation for the exits), changing
        # cars if need be.
        specials = {sf.floor - 1: sf for sf in self.special_floors}
        exits = {i for i, sf in specials.items() if sf.kind in EXIT_KINDS} or {
            min((i for i, sf in specials.items() if sf.kind == "lobby"), default=0)}
        reach = self.reachable()
        stranded = [f + 1 for f in range(self.floors) if f not in specials and f not in closed
                    and not exits & reach[f]]
        if stranded:
            names = " or ".join(f"floor {e + 1}" for e in sorted(exits))
            raise SettingsError(f"The elevators don't connect {_describe_floors(stranded)} with {names}, "
                                "the way in and out of the building, even changing cars. Give "
                                f"{'it' if len(stranded) == 1 else 'them'} a group that shares a floor "
                                f"with one that serves {names}.", "elevator_groups")

    def reachable(self) -> dict[int, frozenset[int]]:
        """Floor index -> the floor indices people can get to from it, changing cars if need be
        (Simulation.reach, worked out from the settings). Floors no group serves reach nothing."""
        parent = list(range(self.floors))

        def find(f: int) -> int:
            while parent[f] != f:
                parent[f] = parent[parent[f]]
                f = parent[f]
            return f

        served: set[int] = set()
        for g in self.elevator_groups:
            floors = sorted(range(self.floors) if g.served(self.floors) is None else g.served(self.floors))
            served.update(floors)
            for f in floors[1:]:
                parent[find(f)] = find(floors[0])
        parts: dict[int, set[int]] = {}
        for f in served:
            parts.setdefault(find(f), set()).add(f)
        return {f: frozenset(parts[find(f)]) if f in served else frozenset() for f in range(self.floors)}


def _parse_value(raw: Any, name: str, meta: dict[str, Any]) -> Any:
    label = meta["error_name"]
    if meta["type"] == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        raise SettingsError(f"{label} must be on or off.", name)
    if meta["type"] == "choice":
        allowed = [o["value"] for o in meta["options"]]
        if raw not in allowed:
            raise SettingsError(f"{label} must be one of: {', '.join(o['label'] for o in meta['options'])}.", name)
        return raw
    if meta["type"] == "floors":
        text = str(raw if raw is not None else "").strip()
        if text.lower() in ("", "none"):
            return ""
        if text.lower() == "all":
            raise SettingsError(f"{label} can't be every floor.", name)
        return _parse_floor_spec(text, name)
    if meta["type"] == "time":
        try:
            seconds = parse_time(raw)
        except ValueError:
            raise SettingsError(f"{label} must be a time of day, like 07:30.", name) from None
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"
    return _parse_number(raw, name, int if meta["type"] == "int" else float, {**meta, "label": label})


def _parse_number(raw: Any, name: str, kind: type, meta: dict[str, Any]) -> int | float:
    label = meta["label"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise SettingsError(f"{label} must be a number.", name)
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{label} must be a number.", name) from None
    if kind is int:
        if not value.is_integer():
            raise SettingsError(f"{label} must be a whole number.", name)
        value = int(value)
    if not meta["min"] <= value <= meta["max"]:
        raise SettingsError(f"{label} must be between {meta['min']:g} and {meta['max']:g}.", name)
    return value


def _floor_numbers(spec: str) -> set[int] | None:
    """ "1, 20-38" -> {1, 20, ..., 38}; "all" -> None. The spec must already be valid."""
    if spec.strip().lower() in ("", "all"):
        return None
    numbers: set[int] = set()
    for part in re.split(r"[,\s]+", spec.strip()):
        lo, _, hi = part.partition("-")
        numbers.update(range(int(lo), int(hi or lo) + 1))
    return numbers


def _describe_floors(floors: list[int]) -> str:
    """[3, 4, 5, 9] -> "floors 3-5 and 9"."""
    runs: list[str] = []
    for f in sorted(floors):
        if runs and runs[-1].split("-")[-1] == str(f - 1):
            runs[-1] = f"{runs[-1].split('-')[0]}-{f}"
        else:
            runs.append(str(f))
    word = "floor" if len(floors) == 1 else "floors"
    return f"{word} {', '.join(runs[:-1])} and {runs[-1]}" if len(runs) > 1 else f"{word} {runs[0]}"


def _parse_floor_spec(raw: Any, where: str) -> str:
    """Check and tidy a floors-served list: "1,20 - 38" -> "1, 20-38"; blank or "all" -> "all"."""
    text = str(raw if raw is not None else "").strip()
    if text.lower() in ("", "all"):
        return "all"
    numbers: set[int] = set()
    for part in filter(None, re.split(r"[,\s]+", re.sub(r"\s*-\s*", "-", text))):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise SettingsError(f"Floors served must be floor numbers and ranges, like 1, 20-38 "
                                f"(or all), not {part!r}.", where)
        lo, hi = int(m[1]), int(m[2] or m[1])
        if lo < 1 or hi < lo:
            raise SettingsError(f"{part} isn't a range of floors: they start at 1 and go up.", where)
        numbers.update(range(lo, hi + 1))
    runs: list[list[int]] = []
    for f in sorted(numbers):
        if runs and runs[-1][1] == f - 1:
            runs[-1][1] = f
        else:
            runs.append([f, f])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _parse_elevator_groups(raw: Any) -> list[ElevatorGroup]:
    if not isinstance(raw, list):
        raise SettingsError("Elevator groups must be a list.", "elevator_groups")
    if len(raw) > MAX_GROUPS:
        raise SettingsError(f"There can be at most {MAX_GROUPS} elevator groups.", "elevator_groups")
    result = []
    for i, item in enumerate(raw):
        where = f"elevator_groups.{i}"
        if not isinstance(item, dict):
            raise SettingsError("Each elevator group must be an object.", where)
        name = " ".join(str(item.get("name") or "").split())[:40] or f"Group {i + 1}"
        cars = _parse_number(item.get("cars"), f"{where}.cars", int,
                             {"label": "Cars", "min": 1, "max": MAX_GROUP_CARS})
        floors = _parse_floor_spec(item.get("floors"), f"{where}.floors")
        capacity = _parse_number(item.get("capacity"), f"{where}.capacity", int,
                                 {"label": "People per car", "min": 1, "max": 40})
        speed = _parse_number(item.get("speed"), f"{where}.speed", float,
                              {"label": "Speed", "min": 0.2, "max": 5})
        result.append(ElevatorGroup(name, cars, floors, capacity, speed))
    return result


def _legacy_group(data: dict[str, Any]) -> ElevatorGroup:
    """Settings saved before elevator groups: one group of every car, serving every floor."""
    g = default_elevator_groups()[0]
    if "elevators" in data:
        g.cars = _parse_number(data["elevators"], "elevators", int,
                               {"label": "Number of elevators", "min": 1, "max": MAX_GROUP_CARS})
    if "capacity" in data:
        g.capacity = _parse_number(data["capacity"], "capacity", int,
                                   {"label": "People per car", "min": 1, "max": 40})
    if "speed" in data:
        g.speed = _parse_number(data["speed"], "speed", float, {"label": "Travel speed", "min": 0.2, "max": 5})
    return g


def _parse_special_floors(raw: Any) -> list[SpecialFloor]:
    if not isinstance(raw, list):
        raise SettingsError("Special floors must be a list.", "special_floors")
    result = []
    for i, item in enumerate(raw):
        where = f"special_floors.{i}"
        if not isinstance(item, dict):
            raise SettingsError("Each special floor must be an object.", where)
        floor = _parse_number(item.get("floor"), f"{where}.floor", int,
                              {"label": "Floor", "min": 1, "max": 1000})
        name = str(item.get("name") or "").strip()[:60] or f"Floor {floor}"
        kind = str(item.get("kind") or "").lower()
        if kind not in SPECIAL_KINDS:
            raise SettingsError(f"Type must be one of: {', '.join(SPECIAL_KINDS.values())}.", f"{where}.kind")
        share = _parse_number(item.get("share", 0), f"{where}.share", float,
                              {"label": "Share of trips", "min": 0, "max": 100})
        result.append(SpecialFloor(floor, name, kind, share))
    return result


class SettingsStore:
    """Reads and writes settings as JSON. Writes are atomic, so a crash never leaves half a file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            return Settings.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, SettingsError) as exc:
            log.warning("Couldn't use %s (%s). Starting with default settings; "
                        "the file is left alone until you save.", self.path, exc)
            return Settings()

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SETTINGS_VERSION, **settings.to_dict()}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def make_simulation(settings: Settings,
                    plan: RunPlan | None = None) -> tuple[Simulation, ResidentTraffic, int]:
    """Build a ready-to-run simulation. Returns (simulation, its traffic source, lobby index).

    `plan` says whether the run has a time of day (see RunPlan). Without one it is a steady run:
    outings at an even rate, no commutes and no rush hours.
    """
    plan = plan or RunPlan()
    specials = {sf.floor - 1: sf for sf in settings.special_floors}
    closed = {f - 1 for f in settings.closed_numbers()}
    floors = [
        Floor(i, str(i + 1),
              FloorKind[specials[i].kind.upper()] if i in specials
              else FloorKind.CLOSED if i in closed else FloorKind.RESIDENTIAL,
              name=specials[i].name if i in specials else "")
        for i in range(settings.floors)
    ]
    lobby = min((i for i, sf in specials.items() if sf.kind == "lobby"), default=0)
    cars: list[Elevator] = []
    for group, g in enumerate(settings.elevator_groups):
        served = g.served(settings.floors)
        start = lobby if served is None or lobby in served else min(served)
        cars += [
            Elevator(id=len(cars) + k, capacity=g.capacity, speed=g.speed,
                     acceleration=settings.acceleration, door_time=settings.door_time,
                     dwell_time=settings.dwell_time, transfer_time=settings.transfer_time,
                     served_floors=served, group=group, position=float(start))
            for k in range(g.cars)
        ]
    # Commutes use the lobby and parking floors in proportion to their share of outings
    # (equally if none has a share). A building without either uses the ground floor.
    exits = {i: sf.share for i, sf in specials.items() if sf.kind in EXIT_KINDS}
    if not any(exits.values()):
        exits = {i: 1.0 for i in exits} or {lobby: 1.0}
    traffic = ResidentTraffic(
        settings.residents,
        home_floors=[i for i in range(settings.floors) if i not in specials and i not in closed],
        shares={i: sf.share / 100 for i, sf in specials.items()},
        kinds={i: sf.kind for i, sf in specials.items()},
        exits=exits,
        outings_per_day=settings.outings,
        rush_hours=settings.rush_hours(),
        lunch=settings.lunch_hour(),
        office=settings.office,
        seed=settings.seed,
    )
    metrics = MetricsCollector(warmup=settings.warmup, long_wait=settings.long_wait,
                               bucket_seconds=settings.bucket_minutes * 60)
    sources: list = [traffic]
    if settings.visitors_enabled and settings.visitors_per_day > 0:
        sources.append(VisitorTraffic(
            settings.visitors_per_day, settings.visitor_entrance - 1, settings.visitor_floor - 1,
            settings.visitor_exit_floor - 1, settings.visitor_hours(), parse_time(settings.visitors_close),
            settings.visit_minutes * 60, seed=settings.seed))
    sim = Simulation(Building(floors), cars, traffic=sources, metrics=metrics, seed=settings.seed,
                     day_start=plan.start, dispatcher_factory=lambda: make_dispatcher(settings.dispatcher))
    return sim, traffic, lobby
