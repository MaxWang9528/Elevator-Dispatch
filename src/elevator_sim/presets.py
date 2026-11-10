"""Named presets: complete sets of settings (a building, its elevators, its residents, ...) to switch
between. A few come built in; the rest are saved by the user to a JSON file next to the settings file.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import ElevatorGroup, Settings, SettingsError, SpecialFloor

log = logging.getLogger(__name__)

PRESETS_VERSION = 1
MAX_PRESETS = 100
MAX_NAME = 60


@dataclass
class Preset:
    name: str
    settings: Settings
    builtin: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "builtin": self.builtin, "description": self.description,
                "settings": self.settings.to_dict()}


def _builtin_presets() -> list[Preset]:
    """Ready-made buildings. Only the first is modeled on a real one; the others are typical shapes."""
    mid_rise = Settings(
        floors=12, acceleration=0.3, residents=300,
        elevator_groups=[ElevatorGroup("Elevators", 2, "all", 13, 1.0)],
        special_floors=[SpecialFloor(1, "Lobby", "lobby", 70), SpecialFloor(12, "Roof deck", "amenity", 5)],
    )
    tower = Settings(
        floors=60, residents=2000,
        # Two banks: the low-rise cars stop everywhere up to the sky lounge, the high-rise cars
        # run express from the garage and shops to the sky lounge and stop everywhere above it.
        elevator_groups=[
            ElevatorGroup("Low-rise", 4, "1-30", 16, 2.0),
            ElevatorGroup("High-rise", 4, "1-3, 30-60", 16, 3.0),
        ],
        special_floors=[
            SpecialFloor(1, "Lobby", "lobby", 55),
            SpecialFloor(2, "Parking garage", "parking", 25),
            SpecialFloor(3, "Shops", "retail", 4),
            SpecialFloor(30, "Sky lounge and gym", "amenity", 6),
            SpecialFloor(60, "Roof deck", "amenity", 3),
        ],
    )
    office = Settings(
        floors=50, office=True, residents=4000, outings=1.0,
        morning_share=85, evening_share=85, lunch_enabled=True, lunch_peak="12:30", lunch_share=33,
        # Three zones, each served from the lobby and the food court, faster the higher it goes.
        elevator_groups=[
            ElevatorGroup("Low zone", 4, "1-17", 16, 2.0),
            ElevatorGroup("Mid zone", 4, "1-2, 18-33", 16, 2.5),
            ElevatorGroup("High zone", 4, "1-2, 34-50", 16, 3.0),
        ],
        special_floors=[
            SpecialFloor(1, "Lobby", "lobby", 35),
            SpecialFloor(2, "Food court", "retail", 15),
        ],
    )
    # One World Trade Center, New York. From public sources: floor numbers run to 104 (94 real
    # stories; 94-99 are skipped), the lobby and base fill 1-19, offices 20-90, the sky lobby is
    # on 64, the observatory on 100-102, and 5 Sky Pod elevators run from the lobby to 102 in
    # about 47 s at about 10 m/s. 71-73 elevators in all. Estimates (not published): how the
    # office banks split, their cars and speeds, and the number of people at work. As in the
    # real building, people working above 64 ride an express shuttle to the sky lobby on 64 and
    # change there to a local bank.
    wtc = Settings(
        floors=104, closed_floors="2-19, 91-99, 103-104", office=True, residents=13000,
        acceleration=0.3, outings=1.0, morning_share=85, evening_share=85,
        lunch_enabled=True, lunch_peak="12:30", lunch_share=33,
        # Observatory visitors: 15 million from the 2015 opening to November 2024, about 4,400 a
        # day. Up the Sky Pods to 102, out from 100. Open about 9 to 9, last entry 45 minutes
        # before closing, busiest from midday to 5 pm; visits usually take 45 to 90 minutes.
        visitors_enabled=True, visitors_per_day=4400, visitor_entrance=1, visitor_floor=102,
        visitor_exit_floor=100, visitors_open="09:00", visitors_peak="15:00",
        visitors_last_entry="20:15", visitors_close="21:00", visit_minutes=75,
        elevator_groups=[
            ElevatorGroup("Bank A, 20-30", 8, "1, 20-30", 20, 1.25),
            ElevatorGroup("Bank B, 31-41", 8, "1, 31-41", 20, 1.5),
            ElevatorGroup("Bank C, 42-52", 8, "1, 42-52", 20, 2.0),
            ElevatorGroup("Bank D, 53-63", 8, "1, 53-63", 20, 2.25),
            ElevatorGroup("Sky lobby shuttles", 8, "1, 64", 24, 3.0),
            ElevatorGroup("Bank E, 64-77", 6, "64-77", 20, 1.5),
            ElevatorGroup("Bank F, 78-90", 6, "64, 78-90", 20, 2.0),
            ElevatorGroup("Sky Pods", 5, "1, 100-102", 20, 3.0),    # calibrated: lobby to 102 in about 47 s
        ],
        special_floors=[
            SpecialFloor(1, "Lobby, PATH and Oculus concourse", "lobby", 35),
            SpecialFloor(64, "Sky lobby", "amenity", 5),
            SpecialFloor(100, "One World Observatory", "amenity", 0),
            SpecialFloor(101, "ONE Dine", "amenity", 0),
            SpecialFloor(102, "Observatory event space", "amenity", 0),
        ],
    )
    for s in (mid_rise, tower, office, wtc):
        s.validate()
    return [
        Preset("Marquee at Block 37", Settings(), builtin=True,
               description="The defaults: 38 floors in Chicago's Loop, 6 elevators, about 1,000 residents."),
        Preset("Mid-rise, 12 floors", mid_rise, builtin=True,
               description="A smaller apartment building with 2 slower elevators and 300 residents."),
        Preset("Tower, 60 floors", tower, builtin=True,
               description="A tall residential tower with low-rise and high-rise banks of 4 cars, "
                           "2,000 residents, a garage and a sky lounge."),
        Preset("Office tower, 50 floors", office, builtin=True,
               description="4,000 people at work, three zones of 4 cars from the lobby, "
                           "and a lunch peak at the food court."),
        Preset("One World Trade Center", wtc, builtin=True,
               description="New York, floors to 104: offices on 20-90, four banks from the lobby and "
                           "two from the sky lobby on 64 (with shuttles up to it), and 5 Sky Pods "
                           "taking about 4,400 visitors a day to the "
                           "observatory. About 13,000 people at work (an estimate), and the bank "
                           "layout is an estimate too."),
    ]


BUILTIN_PRESETS: list[Preset] = _builtin_presets()


def clean_name(raw: Any) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise SettingsError("Give the preset a name.", "preset_name")
    if len(name) > MAX_NAME:
        raise SettingsError(f"Preset names can be at most {MAX_NAME} characters.", "preset_name")
    return name


class PresetStore:
    """The built-in presets plus the user's, which are saved as JSON (atomically, like settings)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self._user = self._load()

    @classmethod
    def beside(cls, settings_path: Path) -> PresetStore:
        """The presets file that goes with a settings file: presets.json in the same folder."""
        return cls(Path(settings_path).parent / "presets.json")

    # --- reading ------------------------------------------------------------------------
    def all(self) -> list[Preset]:
        with self.lock:
            return [*BUILTIN_PRESETS, *self._user]

    def get(self, name: Any) -> Preset:
        key = str(name or "").strip().casefold()
        for preset in self.all():
            if preset.name.casefold() == key:
                return preset
        raise SettingsError(f"There's no preset called {name!r}.", "preset_name")

    # --- writing ------------------------------------------------------------------------
    def save(self, name: Any, settings: Settings) -> Preset:
        """Save settings under a name, replacing a saved preset of the same name."""
        name = clean_name(name)
        if any(p.name.casefold() == name.casefold() for p in BUILTIN_PRESETS):
            raise SettingsError(f"{name!r} is a built-in preset. Pick another name.", "preset_name")
        settings.validate()
        preset = Preset(name, settings)
        with self.lock:
            user = [p for p in self._user if p.name.casefold() != name.casefold()]
            if len(user) >= MAX_PRESETS:
                raise SettingsError(f"There are already {MAX_PRESETS} saved presets. Delete one first.",
                                    "preset_name")
            user.append(preset)
            user.sort(key=lambda p: p.name.casefold())
            self._write(user)
            self._user = user
        return preset

    def delete(self, name: Any) -> None:
        preset = self.get(name)
        if preset.builtin:
            raise SettingsError("Built-in presets can't be deleted.", "preset_name")
        with self.lock:
            user = [p for p in self._user if p.name.casefold() != preset.name.casefold()]
            self._write(user)
            self._user = user

    # --- file ---------------------------------------------------------------------------
    def _load(self) -> list[Preset]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            items = data["presets"]
            if not isinstance(items, list):
                raise TypeError("presets must be a list")
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("Couldn't read presets from %s (%s). Starting with none; "
                        "the file is left alone until a preset is saved.", self.path, exc)
            return []
        builtin = {p.name.casefold() for p in BUILTIN_PRESETS}
        result: dict[str, Preset] = {}
        for item in items:
            try:
                name = clean_name(item.get("name") if isinstance(item, dict) else None)
                if name.casefold() in builtin:
                    raise SettingsError("same name as a built-in preset")
                result[name.casefold()] = Preset(name, Settings.from_dict(item.get("settings")))
            except SettingsError as exc:
                log.warning("Skipping a preset in %s: %s", self.path, exc)
        return sorted(result.values(), key=lambda p: p.name.casefold())

    def _write(self, user: list[Preset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": PRESETS_VERSION,
                   "presets": [{"name": p.name, "settings": p.settings.to_dict()} for p in user]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".presets-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def load_preset(parser: Any, settings_path: Path, name: str) -> Settings:
    """For the command-line tools: a preset's settings, or exit listing the presets there are."""
    store = PresetStore.beside(settings_path)
    try:
        return store.get(name).settings
    except SettingsError:
        names = ", ".join(repr(p.name) for p in store.all())
        parser.error(f"no preset called {name!r}. Presets: {names}")
        raise  # parser.error exits; this keeps type checkers happy
