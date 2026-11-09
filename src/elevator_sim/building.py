"""Floors and the building.

Floors are addressed by a 0-based *index* everywhere in the simulation (index 0 is the lowest
level, e.g. a basement). `label` is what people see ("B2", "L", "12"), so buildings with
basements or a missing 13th floor still have contiguous indices.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .enums import Direction, FloorKind
from .person import Person


@dataclass(eq=False)
class Floor:
    index: int
    label: str
    kind: FloorKind = FloorKind.RESIDENTIAL
    name: str = ""                     # e.g. "Lobby"; empty for ordinary floors
    up_queue: deque[Person] = field(default_factory=deque)
    down_queue: deque[Person] = field(default_factory=deque)

    def queue(self, direction: Direction) -> deque[Person]:
        if direction is Direction.UP:
            return self.up_queue
        if direction is Direction.DOWN:
            return self.down_queue
        raise ValueError("no queue for Direction.IDLE")

    @property
    def waiting_count(self) -> int:
        return len(self.up_queue) + len(self.down_queue)


@dataclass(eq=False)
class Building:
    floors: list[Floor]

    def __post_init__(self) -> None:
        for i, f in enumerate(self.floors):
            if f.index != i:
                raise ValueError(f"floor at position {i} has index {f.index}")

    @classmethod
    def simple(cls, num_floors: int, basements: int = 0) -> Building:
        """Basements B<n>..B1, then a lobby labelled 'L', then floors 2, 3, ..."""
        floors: list[Floor] = []
        for b in range(basements, 0, -1):
            floors.append(Floor(len(floors), f"B{b}", FloorKind.PARKING))
        floors.append(Floor(len(floors), "L", FloorKind.LOBBY))
        for n in range(2, num_floors + 1):
            floors.append(Floor(len(floors), str(n)))
        return cls(floors)

    def __len__(self) -> int:
        return len(self.floors)

    def __getitem__(self, index: int) -> Floor:
        return self.floors[index]

    @property
    def top(self) -> int:
        return len(self.floors) - 1
