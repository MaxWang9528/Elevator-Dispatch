"""Time of day, and how one run uses it.

A run either has no time of day (steady traffic at one rate, as long as you like) or starts at
a time of day and usually ends at one. Rush hours only happen in runs that have a time of day.
Times of day are seconds after midnight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DAY = 86400
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?$")


def parse_time(text: str) -> int:
    """"7:00" or "07:00" (24-hour) -> seconds after midnight. Raises ValueError."""
    match = _TIME.match(text.strip()) if isinstance(text, str) else None
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(f"{text!r} is not a time of day like 07:30")
    return int(match[1]) * 3600 + int(match[2]) * 60


def format_time(seconds: float, with_seconds: bool = False) -> str:
    """Seconds after midnight -> "7:05" (or "7:05:09"). Wraps past midnight."""
    s = int(round(seconds)) % DAY if with_seconds else int(round(seconds / 60)) * 60 % DAY
    text = f"{s // 3600}:{s % 3600 // 60:02d}"
    return f"{text}:{s % 60:02d}" if with_seconds else text


@dataclass(frozen=True)
class RunPlan:
    """When a run starts and ends.

    mode      "steady" (no time of day), "morning" / "evening" (the hours around that rush),
              or "custom" (hours you chose)
    start     the time of day at simulated second 0; None for a steady run
    duration  simulated seconds until the run ends; None = it runs until you stop it
    """

    mode: str = "steady"
    start: int | None = None
    duration: int | None = None

    def __post_init__(self) -> None:
        if self.start is not None and not 0 <= self.start < DAY:
            raise ValueError("start must be a time of day")
        if self.duration is not None and self.duration <= 0:
            raise ValueError("duration must be more than 0")

    @classmethod
    def between(cls, start: int, stop: int, mode: str = "custom") -> RunPlan:
        """From one time of day to another. A stop at or before the start is on the next day,
        so the same time twice is a full 24 hours."""
        return cls(mode, start, (stop - start) % DAY or DAY)

    @property
    def stop(self) -> int | None:
        """The time of day the run ends, if it has both a start and an end."""
        if self.start is None or self.duration is None:
            return None
        return (self.start + self.duration) % DAY

    def describe(self) -> str:
        if self.start is None:
            return "steady traffic"
        name = {"morning": "morning rush", "evening": "evening rush"}.get(self.mode, "chosen hours")
        if self.duration is None:
            return f"{name}, from {format_time(self.start)}"
        return f"{name}, {format_time(self.start)} to {format_time(self.stop)}"
