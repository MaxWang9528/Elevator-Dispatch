"""Traffic sources: things that spawn people over time.

`ResidentTraffic` is what the simulator uses. It models the people who live in the building:
each resident has a home floor and is either at home, on their way somewhere, or away (out of
the building, or at an amenity or a neighbor's). Every trip away from home is followed by a
trip back, so the building can never empty out or fill up impossibly, and the number of trips
follows from the number of residents.

`RandomTraffic` and `FloorShareTraffic` are simpler, memoryless sources: independent trips at a
fixed rate. They are handy for tests and stress experiments.
"""

from __future__ import annotations

import heapq
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .clock import DAY

if TYPE_CHECKING:
    from .person import Person
    from .simulation import Simulation

HOUR = 3600


# --- memoryless sources -------------------------------------------------------------------
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
    """Independent trips, each linking a residential floor with somewhere else.

    `shares` maps a floor index to the fraction of all trips that start or end there
    (e.g. {0: 0.45} = 45% of trips touch the lobby). Whatever fraction is left over becomes
    trips between two residential floors. Half of trips leave home, half come back.
    """

    def __init__(self, per_minute: float, shares: dict[int, float], home_floors: list[int]) -> None:
        _check_shares(shares, home_floors)
        self.per_minute = per_minute
        self.shares = dict(shares)
        self.home_floors = list(home_floors)

    def step(self, sim: Simulation, dt: float) -> None:
        for _ in range(_poisson(sim.rng, self.per_minute / 60.0 * dt)):
            self.spawn_one(sim)

    def spawn_one(self, sim: Simulation) -> None:
        home = sim.rng.choice(self.home_floors)
        away = _pick_destination(sim.rng, self.shares, self.home_floors, home, len(sim.building))
        if sim.rng.random() < 0.5:
            sim.spawn_person(home, away)
        else:
            sim.spawn_person(away, home)


# --- rush hours -----------------------------------------------------------------------------
@dataclass(frozen=True)
class RushHour:
    """A daily commute. `share` of the residents leave home (morning, `leaving=True`) or come
    home (evening) between `start` and `end`, most of them around `peak`.

    Departure times follow an S-shaped ramp up to the peak and back down (half a cosine wave
    each side), so traffic eases away from the baseline and back to it.
    start, peak, end are times of day in seconds after midnight.
    """

    name: str
    start: float
    peak: float
    end: float
    share: float
    leaving: bool

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.peak < self.end <= DAY:
            raise ValueError(f"{self.name} rush: start, peak and end must be in that order within one day")
        if not 0 <= self.share <= 1:
            raise ValueError(f"{self.name} rush: share must be between 0 and 1")

    def intensity(self, time_of_day: float) -> float:
        """How far into the rush we are: 0 outside it, 1 at the peak."""
        if not self.start < time_of_day < self.end:
            return 0.0
        if time_of_day <= self.peak:
            x = (time_of_day - self.start) / (self.peak - self.start)
        else:
            x = (self.end - time_of_day) / (self.end - self.peak)
        return 0.5 - 0.5 * math.cos(math.pi * x)

    def density(self, time_of_day: float) -> float:
        """Probability density of a commuter's departure time (per second). Integrates to 1."""
        return self.intensity(time_of_day) / ((self.end - self.start) / 2)

    def cdf(self, time_of_day: float) -> float:
        """Share of this rush's commuters who have set off by `time_of_day`."""
        t = min(max(time_of_day, self.start), self.end)
        rise, fall = self.peak - self.start, self.end - self.peak
        if t <= self.peak:
            u = t - self.start
            area = u / 2 - rise / (2 * math.pi) * math.sin(math.pi * u / rise)
        else:
            u = self.end - t
            area = rise / 2 + fall / 2 - (u / 2 - fall / (2 * math.pi) * math.sin(math.pi * u / fall))
        return area / ((self.end - self.start) / 2)

    def sample(self, rng) -> float:
        """A random departure time of day, by inverting the CDF."""
        target, lo, hi = rng.random(), self.start, self.end
        for _ in range(32):          # bisection: well under a second of error
            mid = (lo + hi) / 2
            if self.cdf(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2


# Relative outing activity through the day, one value per hour (midnight to 11 pm). Outings
# outside the rush hours (errands, the gym, visits) are rare at night and busiest in the
# evening. Only the shape matters: it is scaled so the day's total matches the setting.
DAILY_ACTIVITY = (
    0.15, 0.08, 0.05, 0.03, 0.03, 0.08, 0.35, 0.80, 0.90, 0.90, 0.90, 1.00,
    1.10, 1.00, 0.95, 1.00, 1.10, 1.30, 1.50, 1.50, 1.30, 1.00, 0.60, 0.30,
)
_ACTIVITY_MEAN = sum(DAILY_ACTIVITY) / len(DAILY_ACTIVITY)


def daily_activity(time_of_day: float) -> float:
    """DAILY_ACTIVITY at any moment, relative to the day's average (so it averages 1).
    Each hour's value sits at the middle of the hour; values in between are interpolated."""
    h = (time_of_day / HOUR - 0.5) % 24
    i = int(h)
    frac = h - i
    value = DAILY_ACTIVITY[i] * (1 - frac) + DAILY_ACTIVITY[(i + 1) % 24] * frac
    return value / _ACTIVITY_MEAN


# How long an outing lasts, by the kind of floor it goes to:
# (chance of a quick visit, quick visit range in seconds, mean of a longer stay, shortest longer stay)
STAYS: dict[str, tuple[float, tuple[float, float], float, float]] = {
    "lobby": (0.4, (120, 480), 60 * 60, 10 * 60),        # mail and packages, or out on foot
    "parking": (0.0, (0, 0), 90 * 60, 15 * 60),          # the car, the mall, the CTA
    "retail": (0.0, (0, 0), 40 * 60, 10 * 60),
    "amenity": (0.0, (0, 0), 50 * 60, 15 * 60),          # gym, roof deck, common rooms
    "residential": (0.0, (0, 0), 60 * 60, 10 * 60),      # visiting a neighbor
}
_MEAN_STAY = HOUR       # used only to estimate when outing returns happen, for the traffic curve


class State(Enum):
    HOME = "home"
    TRAVELING = "traveling"   # waiting for or riding an elevator
    AWAY = "away"             # somewhere other than home: out of the building, or at a floor


@dataclass(eq=False)
class Resident:
    id: int
    home: int
    rng: random.Random = field(repr=False, default_factory=random.Random)  # their own choices
    state: State = State.HOME
    floor: int = 0                       # where they are, or where they are headed
    commuting: bool = False              # away for the day (as opposed to an outing)
    token: int = 0                       # bumped every time they arrive somewhere
    planned_return: float | None = None  # sim time a commuter comes home


@dataclass(frozen=True)
class Census:
    home: int
    traveling: int
    out: int        # away through an exit floor (out of the building, or in the lobby or garage)
    inside: int     # away at another floor inside the building

    def to_dict(self) -> dict[str, int]:
        return {"home": self.home, "traveling": self.traveling, "out": self.out, "inside": self.inside}


@dataclass(order=True)
class _Event:
    time: float
    seq: int
    kind: str = field(compare=False)       # "leave", "return" or "day"
    rid: int = field(compare=False, default=-1)
    data: tuple = field(compare=False, default=())


class ResidentTraffic:
    """The residents of the building, and the trips they make.

    Two kinds of trips, each with its way back:
      commutes  only in a run with a time of day (`Simulation.day_start`). Each day, a share of
                the residents leave through an exit floor during the morning rush and a share
                come home during the evening rush (see `RushHour`). Commuters leave and come back
                through the same exit floor.
      outings   from home to a floor picked by `shares` (floor index -> fraction of outings; the
                rest visit another residential floor), a stay that depends on the floor (STAYS),
                then home again. `outings_per_day` per resident on average, spread through the
                day by DAILY_ACTIVITY in a timed run, or evenly in a steady run.

    `exits` (floor index -> weight) are the ways out of the building: the lobby and the garage.

    Random numbers: traffic never uses the simulation's generator. When outings start and who
    commutes when come from the traffic's own generator (`seed`), and each resident's own
    choices (where to go, how long to stay) from that resident's generator. So two dispatchers
    run on the same seed face the same residents making the same plans, and a comparison
    measures the algorithms rather than luck. Only what depends on the elevators (when someone
    is back home and free to go out again) can differ.
    """

    def __init__(self, residents: int, home_floors: Sequence[int], shares: dict[int, float], *,
                 kinds: dict[int, str] | None = None, exits: dict[int, float] | None = None,
                 outings_per_day: float = 1.5, rush_hours: Sequence[RushHour] = (),
                 seed: int = 0) -> None:
        _check_shares(shares, home_floors)
        if residents < 1:
            raise ValueError("at least one resident is needed")
        exits = {f: w for f, w in (exits or {}).items() if w > 0}
        if rush_hours and not exits:
            raise ValueError("rush hours need at least one exit floor with a weight above 0")
        if sum(1 for r in rush_hours if r.leaving) > 1 or sum(1 for r in rush_hours if not r.leaving) > 1:
            raise ValueError("at most one leaving and one coming-home rush")
        self.home_floors = list(home_floors)
        self.shares = dict(shares)
        self.kinds = dict(kinds or {})
        self.exits = exits
        self.outings_per_day = outings_per_day
        self.rush_hours = tuple(rush_hours)
        self.morning = next((r for r in rush_hours if r.leaving), None)
        self.evening = next((r for r in rush_hours if not r.leaving), None)
        # Spread residents evenly over the residential floors.
        n_floors = len(self.home_floors)
        self.rng = random.Random(f"traffic-{seed}")
        self.residents = [
            Resident(i, self.home_floors[i * n_floors // residents], random.Random(f"resident-{seed}-{i}"))
            for i in range(residents)
        ]
        for r in self.residents:
            r.floor = r.home
        self._home = [r.id for r in self.residents]     # ids of residents at home
        self._home_pos = {rid: i for i, rid in enumerate(self._home)}
        self._events: list[_Event] = []
        self._seq = 0
        self._started = False
        self.n_floors: int | None = None

    # --- rates ---------------------------------------------------------------------------
    def outing_rate(self, time_of_day: float | None) -> float:
        """Outings starting per second across the building."""
        base = len(self.residents) * self.outings_per_day / DAY
        return base if time_of_day is None else base * daily_activity(time_of_day)

    @property
    def late_window(self) -> tuple[float, float]:
        """When commuters who don't come home in the evening rush come home instead."""
        if self.evening is None:
            return (12 * HOUR, 23.5 * HOUR)
        end = self.evening.end
        return (end, max(end + 1800, min(23.5 * HOUR, DAY)))

    def _late_share(self) -> float:
        """Share of residents who leave in the morning but don't come home in the evening rush."""
        m = self.morning.share if self.morning else 0.0
        e = self.evening.share if self.evening else 0.0
        return max(0.0, m - e)

    def expected(self, time_of_day: float | None) -> dict[str, float]:
        """Expected trips per minute at a moment, and the share of them that are commutes.
        An estimate (outing returns are assumed to come about an hour after they leave),
        used for the traffic curve on the page."""
        n = len(self.residents)
        if time_of_day is None:
            total = 2 * self.outing_rate(None) * 60
            return {"total": total, "leaving": 0.0, "returning": 0.0}
        t = time_of_day
        out = n * self.morning.share * self.morning.density(t) * 60 if self.morning else 0.0
        back = n * self.evening.share * self.evening.density(t) * 60 if self.evening else 0.0
        lo, hi = self.late_window
        if lo <= t < hi:
            back += n * self._late_share() / (hi - lo) * 60
        outings = (self.outing_rate(t) + self.outing_rate((t - _MEAN_STAY) % DAY)) * 60
        total = out + back + outings
        return {
            "total": total,
            "leaving": out / total if total else 0.0,
            "returning": back / total if total else 0.0,
        }

    def census(self) -> Census:
        home = len(self._home)
        traveling = out = inside = 0
        for r in self.residents:
            if r.state is State.TRAVELING:
                traveling += 1
            elif r.state is State.AWAY:
                if r.floor in self.exits:
                    out += 1
                else:
                    inside += 1
        return Census(home, traveling, out, inside)

    # --- the simulation calls these ---------------------------------------------------------
    def step(self, sim: Simulation, dt: float) -> None:
        if not self._started:
            self._start(sim)
        while self._events and self._events[0].time <= sim.time:
            self._handle(sim, heapq.heappop(self._events))
        for _ in range(_poisson(self.rng, self.outing_rate(sim.time_of_day) * dt)):
            self.start_outing(sim)

    def on_arrive(self, sim: Simulation, person: Person) -> None:
        """A resident got out of the elevator: they are home, or somewhere to spend time."""
        if person.resident_id is None:
            return
        r = self.residents[person.resident_id]
        r.floor = person.requested_floor
        r.token += 1
        if r.floor == r.home:
            r.state = State.HOME
            r.commuting = False
            r.planned_return = None
            self._add_home(r.id)
            return
        r.state = State.AWAY
        if person.purpose == "commute_out":
            back = max(r.planned_return or sim.time, sim.time + 60)
        else:
            back = sim.time + self._stay(r.rng, r.floor)
        self._push(back, "return", r.id, (r.token,))

    # --- trips -------------------------------------------------------------------------------
    def start_outing(self, sim: Simulation) -> Person | None:
        """A random resident who is at home goes somewhere. None if nobody is home.

        Uses exactly one random number from the traffic generator, whoever is home: it picks a
        point in the list of residents and takes the first one at home from there. That keeps
        the traffic generator in step across dispatchers even when different people are home.
        """
        x = self.rng.random()
        if not self._home:
            return None
        n = len(self.residents)
        start = int(x * n)
        for k in range(n):
            r = self.residents[(start + k) % n]
            if r.state is State.HOME:
                break
        dest = _pick_destination(r.rng, self.shares, self.home_floors, r.home, len(sim.building))
        return self._trip(sim, r, dest, "outing")

    def _trip(self, sim: Simulation, r: Resident, dest: int, purpose: str) -> Person | None:
        if dest == r.floor:
            return None
        if r.state is State.HOME:
            self._remove_home(r.id)
        origin = r.floor
        r.state = State.TRAVELING
        r.floor = dest
        return sim.spawn_person(origin, dest, resident_id=r.id, purpose=purpose)

    def _pick_exit(self, r: Resident) -> int:
        return r.rng.choices(list(self.exits), weights=list(self.exits.values()))[0]

    def _stay(self, rng, floor: int) -> float:
        quick, (lo, hi), mean, shortest = STAYS[self.kinds.get(floor, "residential")]
        if quick and rng.random() < quick:
            return rng.uniform(lo, hi)
        return shortest + rng.expovariate(1 / (mean - shortest))

    # --- days and commutes ---------------------------------------------------------------------
    def _start(self, sim: Simulation) -> None:
        self._started = True
        self.n_floors = len(sim.building)
        tod = sim.time_of_day
        if tod is None or not self.rush_hours:
            return
        midnight = sim.time - tod
        self._plan_day(sim, midnight, first=True)
        self._push(midnight + DAY, "day")

    def _plan_day(self, sim: Simulation, midnight: float, first: bool) -> None:
        """Decide who commutes on the day starting at sim time `midnight`, and when."""
        rng, now = self.rng, sim.time
        m = self.morning.share if self.morning else 0.0
        e = self.evening.share if self.evening else 0.0
        lo, hi = self.late_window
        for r in self.residents:
            leaves = m > 0 and rng.random() < m
            if self.evening is None:
                rush_home = False
            elif leaves:
                rush_home = e >= m or rng.random() < e / m
            else:   # more come home than left: the rest were already out (e.g. a night shift)
                rush_home = e > m and rng.random() < (e - m) / (1 - m)
            if not leaves and not rush_home:
                continue
            back = midnight + (self.evening.sample(rng) if rush_home else rng.uniform(lo, hi))
            leave = midnight + self.morning.sample(rng) if leaves else None
            if leave is not None and back <= leave:
                back = leave + rng.uniform(6 * HOUR, 10 * HOUR)
            if leave is not None and leave > now:
                deadline = midnight + self.morning.end + HOUR
                self._push(leave, "leave", r.id, (back, deadline))
            elif first and back > now and r.state is State.HOME:
                # Already left for the day when the run begins: start them out of the building.
                self._remove_home(r.id)
                r.state, r.floor, r.commuting = State.AWAY, self._pick_exit(r), True
                if r.floor == r.home:      # lives on the exit floor: nothing to simulate
                    r.state, r.floor, r.commuting = State.HOME, r.home, False
                    self._add_home(r.id)
                    continue
                r.token += 1
                self._push(back, "return", r.id, (r.token,))

    def _handle(self, sim: Simulation, ev: _Event) -> None:
        if ev.kind == "day":
            self._plan_day(sim, ev.time, first=False)
            self._push(ev.time + DAY, "day")
            return
        r = self.residents[ev.rid]
        if ev.kind == "leave":
            back, deadline = ev.data
            if r.state is State.HOME:
                r.planned_return = back
                r.commuting = True
                if self._trip(sim, r, self._pick_exit(r), "commute_out") is None:
                    r.commuting, r.planned_return = False, None    # lives on the exit floor
            elif ev.time + 300 <= deadline:
                self._push(ev.time + 300, "leave", r.id, ev.data)    # busy: try again shortly
        elif ev.kind == "return":
            (token,) = ev.data
            if r.state is not State.AWAY or r.token != token:
                return                      # stale: they have moved since this was planned
            purpose = "commute_in" if r.commuting else "outing_return"
            self._trip(sim, r, r.home, purpose)

    # --- bookkeeping ---------------------------------------------------------------------------
    def _push(self, time: float, kind: str, rid: int = -1, data: tuple = ()) -> None:
        self._seq += 1
        heapq.heappush(self._events, _Event(time, self._seq, kind, rid, data))

    def _add_home(self, rid: int) -> None:
        if rid not in self._home_pos:
            self._home_pos[rid] = len(self._home)
            self._home.append(rid)

    def _remove_home(self, rid: int) -> None:
        i = self._home_pos.pop(rid)
        last = self._home.pop()
        if last != rid:
            self._home[i] = last
            self._home_pos[last] = i


# --- helpers --------------------------------------------------------------------------------
def _check_shares(shares: dict[int, float], home_floors: Sequence[int]) -> None:
    if sum(shares.values()) > 1 + 1e-9:
        raise ValueError("floor shares add up to more than 100%")
    if not home_floors:
        raise ValueError("at least one residential floor is needed")
    overlap = set(shares) & set(home_floors)
    if overlap:
        raise ValueError(f"floors {sorted(overlap)} are both special and residential")


def _pick_destination(rng, shares: dict[int, float], home_floors: Sequence[int], home: int,
                      n_floors: int) -> int:
    """A special floor by its share; otherwise another residential floor."""
    x = rng.random()
    for floor, share in shares.items():
        if x < share:
            if floor != home:
                return floor
            break
        x -= share
    neighbours = [f for f in home_floors if f != home]
    if neighbours:
        return rng.choice(neighbours)
    return rng.choice([f for f in range(n_floors) if f != home])


def _poisson(rng, mean: float) -> int:
    """Number of arrivals in one tick (Knuth's method; fine for small means)."""
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
