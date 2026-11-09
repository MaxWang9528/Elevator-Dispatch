"""Traffic sources: things that spawn people over time."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .simulation import Simulation


class RandomTraffic:
    """People appear at random (Poisson) times with random origin and destination floors.

    `lobby_share` is the fraction of trips that start or end at the lobby, which is where most
    real apartment traffic goes. The rest are uniformly random floor-to-floor trips.
    """

    def __init__(self, per_minute: float, lobby: int | None = None, lobby_share: float = 0.0) -> None:
        if lobby_share and lobby is None:
            raise ValueError("lobby_share needs a lobby floor index")
        self.per_minute = per_minute
        self.lobby = lobby
        self.lobby_share = lobby_share

    def step(self, sim: Simulation, dt: float) -> None:
        for _ in range(_poisson(sim.rng, self.per_minute / 60.0 * dt)):
            if self.lobby is not None and sim.rng.random() < self.lobby_share:
                other = sim.rng.randrange(len(sim.building) - 1)
                other += other >= self.lobby
                if sim.rng.random() < 0.5:
                    sim.spawn_person(self.lobby, other)
                else:
                    sim.spawn_person(other, self.lobby)
            else:
                sim.spawn_person()


class FloorShareTraffic:
    """Every trip links a resident's home floor with somewhere else.

    `shares` maps a floor index to the fraction of all trips that start or end there
    (e.g. {0: 0.45} = 45% of trips touch the lobby). Whatever fraction is left over becomes
    trips between two residential floors. Half of trips leave home, half come back.
    """

    def __init__(self, per_minute: float, shares: dict[int, float], home_floors: list[int]) -> None:
        if sum(shares.values()) > 1 + 1e-9:
            raise ValueError("floor shares add up to more than 100%")
        if not home_floors:
            raise ValueError("at least one residential floor is needed")
        overlap = set(shares) & set(home_floors)
        if overlap:
            raise ValueError(f"floors {sorted(overlap)} are both special and residential")
        self.per_minute = per_minute
        self.shares = dict(shares)
        self.home_floors = list(home_floors)

    def step(self, sim: Simulation, dt: float) -> None:
        for _ in range(_poisson(sim.rng, self.per_minute / 60.0 * dt)):
            self.spawn_one(sim)

    def spawn_one(self, sim: Simulation) -> None:
        home = sim.rng.choice(self.home_floors)
        away = self._pick_away(sim, home)
        if sim.rng.random() < 0.5:
            sim.spawn_person(home, away)
        else:
            sim.spawn_person(away, home)

    def _pick_away(self, sim: Simulation, home: int) -> int:
        r = sim.rng.random()
        for floor, share in self.shares.items():
            if r < share:
                return floor
            r -= share
        neighbours = [f for f in self.home_floors if f != home]
        if neighbours:
            return sim.rng.choice(neighbours)
        return sim.rng.choice([f for f in range(len(sim.building)) if f != home])


def _poisson(rng, mean: float) -> int:
    """Number of arrivals in one tick (Knuth's method; fine for small means)."""
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
