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
from .elevator import Elevator
from .enums import FloorKind
from .metrics import MetricsCollector
from .simulation import Simulation
from .traffic import FloorShareTraffic

log = logging.getLogger(__name__)

SETTINGS_VERSION = 1

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
            step: float = 1, unit: str = "", help: str = "") -> Any:
    return field(default=default, metadata={
        "group": group, "label": label, "min": minimum, "max": maximum,
        "step": step, "unit": unit, "help": help,
    })


@dataclass
class SpecialFloor:
    floor: int      # floor number as people see it (1 = ground)
    name: str
    kind: str       # a key of SPECIAL_KINDS
    share: float    # percent of all trips that start or end here


def default_special_floors() -> list[SpecialFloor]:
    """Marquee at Block 37. Shares are estimates, not measurements."""
    return [
        SpecialFloor(1, "Lobby", "lobby", 45),
        SpecialFloor(4, "Parking, mall, CTA Red and Blue lines", "parking", 32),
        SpecialFloor(5, "Common areas", "amenity", 8),
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
    capacity: int = setting(16, group="Elevators", label="People per car", minimum=1, maximum=40)
    speed: float = setting(1.0, group="Elevators", label="Travel speed", minimum=0.2, maximum=5,
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
    # --- Traffic ---
    rate: float = setting(20, group="Traffic", label="New people per minute", minimum=0, maximum=600,
                          unit="per min",
                          help="The starting rate. The slider on the Simulation tab changes it "
                               "for the current run only.")
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
    seed: int = setting(1, group="Simulation", label="Random seed", minimum=0, maximum=2**31 - 1,
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
        s = cls()
        for f in fields(cls):
            if not f.metadata or f.name not in data:
                continue
            setattr(s, f.name, _parse_number(data[f.name], f.name, type(f.default), f.metadata))
        if "special_floors" in data:
            s.special_floors = _parse_special_floors(data["special_floors"])
        s.validate()
        return s

    @classmethod
    def schema(cls) -> list[dict[str, Any]]:
        """Field descriptions the web UI renders into a form."""
        return [
            {"key": f.name, "type": "int" if type(f.default) is int else "float", "default": f.default,
             **f.metadata}
            for f in fields(cls) if f.metadata
        ]

    def validate(self) -> None:
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


def make_simulation(settings: Settings) -> tuple[Simulation, FloorShareTraffic, int]:
    """Build a ready-to-run simulation. Returns (simulation, its traffic source, lobby index)."""
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
    traffic = FloorShareTraffic(
        settings.rate,
        shares={i: sf.share / 100 for i, sf in specials.items()},
        home_floors=[i for i in range(settings.floors) if i not in specials],
    )
    metrics = MetricsCollector(warmup=settings.warmup, long_wait=settings.long_wait,
                               bucket_seconds=settings.bucket_minutes * 60)
    sim = Simulation(Building(floors), cars, traffic=[traffic], metrics=metrics, seed=settings.seed)
    return sim, traffic, lobby
