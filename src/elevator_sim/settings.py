"""User-editable settings, saved to a JSON file so they survive restarts.

To add a setting, add a field to `Settings` using `setting(...)`. The web Settings tab is built
from that metadata, and settings files saved before the field existed still load (the missing
field just takes its default).
"""

from __future__ import annotations

import json
import logging
import os
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
from .traffic import ResidentTraffic, RushHour

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
            disabled_by: str | None = None, error_name: str | None = None) -> Any:
    """A number. `enabled_by` names an on/off setting; while that is off the UI greys this out.
    `disabled_by` is the reverse: greyed out while that setting is on.
    `error_name` replaces the label in error messages when the label alone is ambiguous."""
    return field(default=default, metadata={
        "type": "int" if type(default) is int else "float",
        "group": group, "label": label, "min": minimum, "max": maximum,
        "step": step, "unit": unit, "help": help, "enabled_by": enabled_by,
        "disabled_by": disabled_by, "error_name": error_name or label,
    })


def choice(default: str, *, group: str, label: str, options: dict[str, str], help: str = "") -> Any:
    """One of a fixed set of values. `options` maps each value to the name shown for it."""
    return field(default=default, metadata={
        "type": "choice", "group": group, "label": label, "help": help, "error_name": label,
        "options": [{"value": v, "label": name} for v, name in options.items()],
    })


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
    "Residents": "Every trip is made by a resident, and everyone who goes somewhere comes back, "
                 "so traffic follows from how many people live here. A typical resident makes "
                 "3 to 5 elevator trips a day.",
    "Morning rush": "Residents leaving for the day. " + _RUSH_HELP,
    "Evening rush": "Residents coming home. " + _RUSH_HELP,
}


@dataclass
class SpecialFloor:
    floor: int      # floor number as people see it (1 = ground)
    name: str
    kind: str       # a key of SPECIAL_KINDS
    share: float    # percent of residents' outings that go here


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
    # --- Elevators ---
    elevators: int = setting(6, group="Elevators", label="Number of elevators", minimum=1, maximum=16)
    capacity: int = setting(9, group="Elevators", label="People per car", minimum=1, maximum=40)
    speed: float = setting(2.5, group="Elevators", label="Travel speed", minimum=0.2, maximum=5,
                           step=0.1, unit="floors/s",
                           help="Top speed. A typical high-rise car at 3.5 m/s covers about "
                                "1.1 floors per second.")
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
                                  "estimate for Marquee at Block 37.")
    outings: float = setting(1.5, group="Residents", label="Outings per resident per day",
                             minimum=0, maximum=50, step=0.1, unit="per day",
                             help="Trips away from home other than commuting: errands, the gym, "
                                  "the mail room, visiting a neighbor. Each one is two elevator "
                                  "trips, there and back. Where they go follows the special "
                                  "floors' shares. The slider on the Simulation tab changes it for "
                                  "the current run only.")
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
             "transit floor (split by those floors' shares). Most leave around the peak.")
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
             "evening. With this rush off, they come home spread over the afternoon and evening.")
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
        return RushHour(
            name=name, start=parse_time(get("start")), peak=parse_time(get("peak")),
            end=parse_time(get("end")), share=get("share") / 100, leaving=name == "morning",
        )

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

    def validate(self) -> None:
        for name in RUSH_NAMES:
            start, peak, end = (parse_time(getattr(self, f"{name}_{key}")) for key in ("start", "peak", "end"))
            if not start < end:
                raise SettingsError(f"The {name} rush has to end after it starts.", f"{name}_end")
            if not start < peak < end:
                raise SettingsError(f"The {name} rush has to peak between its start and its end.",
                                    f"{name}_peak")
        seen: dict[int, int] = {}
        for i, sf in enumerate(self.special_floors):
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
        if len(self.special_floors) >= self.floors:
            raise SettingsError("At least one floor has to be residential.", "special_floors")


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
    floors = [
        Floor(i, str(i + 1),
              FloorKind[specials[i].kind.upper()] if i in specials else FloorKind.RESIDENTIAL,
              name=specials[i].name if i in specials else "")
        for i in range(settings.floors)
    ]
    lobby = min((i for i, sf in specials.items() if sf.kind == "lobby"), default=0)
    cars = [
        Elevator(id=i, capacity=settings.capacity, speed=settings.speed,
                 acceleration=settings.acceleration, door_time=settings.door_time,
                 dwell_time=settings.dwell_time, transfer_time=settings.transfer_time,
                 position=float(lobby))
        for i in range(settings.elevators)
    ]
    # Commutes use the lobby and parking floors in proportion to their share of outings
    # (equally if none has a share). A building without either uses the ground floor.
    exits = {i: sf.share for i, sf in specials.items() if sf.kind in EXIT_KINDS}
    if not any(exits.values()):
        exits = {i: 1.0 for i in exits} or {lobby: 1.0}
    traffic = ResidentTraffic(
        settings.residents,
        home_floors=[i for i in range(settings.floors) if i not in specials],
        shares={i: sf.share / 100 for i, sf in specials.items()},
        kinds={i: sf.kind for i, sf in specials.items()},
        exits=exits,
        outings_per_day=settings.outings,
        rush_hours=settings.rush_hours(),
        seed=settings.seed,
    )
    metrics = MetricsCollector(warmup=settings.warmup, long_wait=settings.long_wait,
                               bucket_seconds=settings.bucket_minutes * 60)
    sim = Simulation(Building(floors), cars, make_dispatcher(settings.dispatcher), traffic=[traffic],
                     metrics=metrics, seed=settings.seed, day_start=plan.start)
    return sim, traffic, lobby
