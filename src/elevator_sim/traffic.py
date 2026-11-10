"""Traffic sources: things that spawn people over time.

`ResidentTraffic` is what the simulator uses. It models the people who live in the building:
each resident has a home floor and is either at home, on their way somewhere, or away (out of
the building, or at an amenity or a neighbor's). Every trip away from home is followed by a
trip back, so the building can never empty out or fill up impossibly, and the number of trips
follows from the number of residents.

In an office building (`office=True`) the same people work here instead: their "home" is the
floor they work on, they arrive in the morning rush and leave in the evening rush, and outside
working hours they are out of the building.

`RandomTraffic` and `FloorShareTraffic` are simpler, memoryless sources: independent trips at a
fixed rate. They are handy for tests and stress experiments.
"""

from __future__ import annotations

import bisect
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
# The same for an office: trips between floors (meetings, a coffee, the mail room) during working
# hours. Only people at work make them, so the night values hardly matter.
OFFICE_ACTIVITY = (
    0.02, 0.02, 0.02, 0.02, 0.02, 0.05, 0.15, 0.45, 0.85, 1.00, 1.10, 1.00,
    0.90, 1.00, 1.10, 1.05, 0.90, 0.60, 0.35, 0.20, 0.10, 0.05, 0.03, 0.02,
)


def daily_activity(time_of_day: float, table: Sequence[float] = DAILY_ACTIVITY) -> float:
    """An activity table at any moment, relative to the day's average (so it averages 1).
    Each hour's value sits at the middle of the hour; values in between are interpolated."""
    h = (time_of_day / HOUR - 0.5) % 24
    i = int(h)
    frac = h - i
    value = table[i] * (1 - frac) + table[(i + 1) % 24] * frac
    return value / _MEANS[id(table)]


_MEANS = {id(t): sum(t) / len(t) for t in (DAILY_ACTIVITY, OFFICE_ACTIVITY)}

# Office outings are planned per person across their own hours at work, following OFFICE_ACTIVITY.
# _OFFICE_AREA[i] is the activity summed from midnight to minute i (the trapezoid rule, per minute).
_OFFICE_AREA = [0.0]
for _m in range(1440):
    _OFFICE_AREA.append(_OFFICE_AREA[-1] + 30 * (daily_activity(_m * 60, OFFICE_ACTIVITY)
                                                 + daily_activity((_m + 1) * 60, OFFICE_ACTIVITY)))


def _office_area(time_of_day: float) -> float:
    """OFFICE_ACTIVITY summed from midnight to `time_of_day` (0 to DAY)."""
    x = min(max(time_of_day, 0.0), DAY) / 60
    i = min(int(x), 1439)
    return _OFFICE_AREA[i] + (_OFFICE_AREA[i + 1] - _OFFICE_AREA[i]) * (x - i)


def _sample_office_time(rng, lo: float, hi: float) -> float:
    """A time of day between `lo` and `hi`, more likely when office activity is high."""
    u = _office_area(lo) + rng.random() * (_office_area(hi) - _office_area(lo))
    i = min(max(bisect.bisect_right(_OFFICE_AREA, u) - 1, 0), 1439)
    seg = _OFFICE_AREA[i + 1] - _OFFICE_AREA[i]
    return min(max((i + (u - _OFFICE_AREA[i]) / seg) * 60, lo), hi)


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

# Lunch: where people go (floors of these kinds, by their share; the exits if there are none)
# and how long they stay (at least LUNCH_SHORTEST, LUNCH_MEAN on average).
LUNCH_KINDS = ("lobby", "parking", "retail")
LUNCH_SHORTEST = 20 * 60
LUNCH_MEAN = 45 * 60


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
    leave_at: float | None = None        # office: sim time they leave work today
    at_work: tuple[float, float] | None = None  # office: today's hours at work (sim times), or None


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

    Three kinds of trips, each with its way back:
      commutes  only in a run with a time of day (`Simulation.day_start`). Each day, a share of
                the residents leave through an exit floor during the morning rush and a share
                come home during the evening rush (see `RushHour`). Commuters leave and come back
                through the same exit floor. In an office (`office`) it is the other way round:
                people start the day out, come in during the morning rush (the rush with
                `leaving=False`) and leave in the evening rush (`leaving=True`).
      lunch     only with `lunch` and a time of day: a share of the people at home (at work, in
                an office) go to a lobby, parking or retail floor around midday, and come back.
      outings   from home to a floor picked by `shares` (floor index -> fraction of outings; the
                rest visit another residential floor), a stay that depends on the floor (STAYS),
                then home again. `outings_per_day` per resident on average, spread through the
                day by DAILY_ACTIVITY in a timed run, or evenly in a steady run. In an office's
                timed run, each person who is at work today makes `outings_per_day` on average,
                planned across their own hours at work by OFFICE_ACTIVITY.

    `exits` (floor index -> weight) are the ways out of the building: the lobby and the garage.

    Elevator groups: trips go wherever the elevators connect (`Simulation.reach`), changing cars
    if need be, except visits to another resident's floor (or, in an office, another office
    floor): those stay within the floors one ride away (`Simulation.direct`), as neighbors and
    colleagues mostly are.

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
                 lunch: RushHour | None = None, office: bool = False, seed: int = 0) -> None:
        _check_shares(shares, home_floors)
        if residents < 1:
            raise ValueError("at least one resident is needed")
        exits = {f: w for f, w in (exits or {}).items() if w > 0}
        if (rush_hours or lunch) and not exits:
            raise ValueError("rush hours and lunch need at least one exit floor with a weight above 0")
        if sum(1 for r in rush_hours if r.leaving) > 1 or sum(1 for r in rush_hours if not r.leaving) > 1:
            raise ValueError("at most one leaving and one coming-home rush")
        self.home_floors = list(home_floors)
        self.shares = dict(shares)
        self.kinds = dict(kinds or {})
        self.exits = exits
        self.outings_per_day = outings_per_day
        self.rush_hours = tuple(rush_hours)
        self.office = office
        self.lunch = lunch
        self.out_rush = next((r for r in rush_hours if r.leaving), None)     # out through the exits
        self.in_rush = next((r for r in rush_hours if not r.leaving), None)  # in through the exits
        # The rush earlier in the day, and the one that undoes it.
        self.first_rush, self.second_rush = ((self.in_rush, self.out_rush) if office
                                             else (self.out_rush, self.in_rush))
        self._activity = OFFICE_ACTIVITY if office else DAILY_ACTIVITY
        lunch_floors = {f: w for f, w in self.shares.items() if self.kinds.get(f) in LUNCH_KINDS}
        if not any(lunch_floors.values()):
            lunch_floors = {f: 1.0 for f in lunch_floors} or dict(exits)
        self.lunch_floors = lunch_floors
        self.reach: dict[int, frozenset[int]] | None = None   # from the simulation, at the start
        self.direct: dict[int, frozenset[int]] | None = None  # the same, one ride away
        self._lunch_gen = 0     # bumped when lunch is switched on or off; older lunch plans lapse
        self._outing_gen = 0    # the same for office outings, when outings per day change
        self._per_person = False  # outings planned per person (an office's timed run), set at the start
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
        return base if time_of_day is None else base * daily_activity(time_of_day, self._activity)

    @property
    def late_window(self) -> tuple[float, float]:
        """When commuters who aren't part of the evening rush make that trip instead: residents
        coming home, or (in an office) people leaving work."""
        if self.second_rush is None:
            return (15 * HOUR, 20 * HOUR) if self.office else (12 * HOUR, 23.5 * HOUR)
        end = self.second_rush.end
        return (end, max(end + 1800, min(23.5 * HOUR, DAY)))

    def _late_share(self) -> float:
        """Share of people in the morning rush who aren't in the evening rush."""
        m = self.first_rush.share if self.first_rush else 0.0
        e = self.second_rush.share if self.second_rush else 0.0
        return max(0.0, m - e)

    def present(self, time_of_day: float) -> float:
        """Estimated share of people at home (at work, in an office) at a time of day."""
        t = time_of_day
        lo, hi = self.late_window
        late = self._late_share() * min(max((t - lo) / (hi - lo), 0.0), 1.0)
        if self.office:
            p = 1.0 if self.in_rush is None else self.in_rush.share * self.in_rush.cdf(t)
            if self.out_rush is not None:
                p -= self.out_rush.share * self.out_rush.cdf(t)
            p -= late if self.in_rush is not None else 0.0
        else:
            p = 1.0
            if self.out_rush is not None:
                p -= self.out_rush.share * self.out_rush.cdf(t)
            if self.in_rush is not None:
                p += self.in_rush.share * self.in_rush.cdf(t)
            p += late if self.out_rush is not None else 0.0
        return min(max(p, 0.0), 1.0)

    def expected(self, time_of_day: float | None) -> dict[str, float]:
        """Expected trips per minute at a moment, and the share of them that are commutes.
        An estimate (outing returns are assumed to come about an hour after they leave),
        used for the traffic curve on the page."""
        n = len(self.residents)
        if time_of_day is None:
            total = 2 * self.outing_rate(None) * 60
            return {"total": total, "leaving": 0.0, "returning": 0.0}
        t = time_of_day
        out = n * self.out_rush.share * self.out_rush.density(t) * 60 if self.out_rush else 0.0
        back = n * self.in_rush.share * self.in_rush.density(t) * 60 if self.in_rush else 0.0
        lo, hi = self.late_window
        if lo <= t < hi and self.first_rush is not None:
            late = n * self._late_share() / (hi - lo) * 60
            if self.office:
                out += late
            else:
                back += late
        earlier = (t - _MEAN_STAY) % DAY
        if self.office:      # each person at work, over a typical workday
            outings = (self._office_outing_rate(t) + self._office_outing_rate(earlier)) * 60
        else:
            outings = (self.outing_rate(t) + self.outing_rate(earlier)) * 60
        if self.lunch is not None:
            back_from = (t - LUNCH_MEAN) % DAY
            outings += n * self.lunch.share * 60 * (self.lunch.density(t) * self.present(t)
                                                    + self.lunch.density(back_from) * self.present(back_from))
        total = out + back + outings
        return {
            "total": total,
            "leaving": out / total if total else 0.0,
            "returning": back / total if total else 0.0,
        }

    def _office_outing_rate(self, time_of_day: float) -> float:
        """Office outings starting per second: those at work, each making `outings_per_day`
        over a typical day at work (from the morning rush peak to the evening rush peak)."""
        lo, hi = self.late_window
        start = self.in_rush.peak if self.in_rush else 0.0
        end = self.out_rush.peak if self.out_rush else (hi if self.in_rush else DAY)
        area = _office_area(end) - _office_area(start)
        if area <= 0:
            return 0.0
        per_person = self.outings_per_day * daily_activity(time_of_day, OFFICE_ACTIVITY) / area
        return len(self.residents) * self.present(time_of_day) * per_person

    def set_outings(self, sim: Simulation, outings_per_day: float) -> None:
        """Change outings per day during a run. Office outings are planned per person, so the
        rest of today's are planned again at the new rate."""
        self.outings_per_day = outings_per_day
        if not (self._per_person and self._started):
            return
        self._outing_gen += 1
        midnight = sim.time - sim.time_of_day
        for r in self.residents:
            if r.at_work is not None and r.at_work[1] > sim.time:
                self._plan_outings(r, midnight, *r.at_work, sim.time)

    def _plan_outings(self, r: Resident, midnight: float, start: float, end: float, now: float) -> None:
        """Plan an office worker's outings for the part of their day at work after `now`: on
        average `outings_per_day` over the whole of it, more where office activity is high."""
        whole = _office_area(end - midnight) - _office_area(start - midnight)
        lo = max(start, now)
        if whole <= 0 or lo >= end:
            return
        left = _office_area(end - midnight) - _office_area(lo - midnight)
        for _ in range(_poisson(self.rng, self.outings_per_day * left / whole)):
            t = midnight + _sample_office_time(self.rng, lo - midnight, end - midnight)
            self._push(t, "outing", r.id, (self._outing_gen, end))

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
        if self._per_person:
            return              # an office's outings are planned per person (_plan_outings)
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
            if self.office and r.leave_at is not None and sim.time >= r.leave_at:
                self._push(sim.time + 120, "leave", r.id, (None, sim.time + 120))  # back late: off soon
            return
        r.state = State.AWAY
        if person.purpose == "commute_out":
            if self.office and r.planned_return is None:
                return                  # left work: back with tomorrow's morning rush
            back = max(r.planned_return or sim.time, sim.time + 60)
        elif person.purpose == "lunch":
            back = sim.time + LUNCH_SHORTEST + r.rng.expovariate(1 / (LUNCH_MEAN - LUNCH_SHORTEST))
            self._push(back, "return", r.id, (r.token, "lunch_return"))
            return
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
        dest = _pick_destination(r.rng, self.shares, self.home_floors, r.home, len(sim.building),
                                 self._reachable(r.home), self._near(r.home))
        return self._trip(sim, r, dest, "outing")

    def _near(self, floor: int) -> frozenset[int] | None:
        return None if self.direct is None else self.direct[floor]

    def _reachable(self, floor: int) -> frozenset[int] | None:
        return None if self.reach is None else self.reach[floor]

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
        return _weighted(r.rng, self.exits, self._reachable(r.home))

    def _pick_lunch(self, r: Resident) -> int:
        return _weighted(r.rng, self.lunch_floors, self._reachable(r.home))

    def _stay(self, rng, floor: int) -> float:
        quick, (lo, hi), mean, shortest = STAYS[self.kinds.get(floor, "residential")]
        if quick and rng.random() < quick:
            return rng.uniform(lo, hi)
        return shortest + rng.expovariate(1 / (mean - shortest))

    # --- days and commutes ---------------------------------------------------------------------
    def _start(self, sim: Simulation) -> None:
        self._started = True
        self.n_floors = len(sim.building)
        self.reach = getattr(sim, "reach", None)
        self.direct = getattr(sim, "direct", None)
        tod = sim.time_of_day
        if tod is None:
            return
        self._per_person = self.office
        # Days are planned in every timed run, even without rushes or lunch, so lunch can be
        # switched on part way through (set_lunch).
        midnight = sim.time - tod
        self._plan_day(sim, midnight, first=True)
        self._push(midnight + DAY, "day")

    def _plan_day(self, sim: Simulation, midnight: float, first: bool) -> None:
        """Decide who commutes and who goes out for lunch on the day starting at sim time
        `midnight`, and when."""
        if self.office:
            self._plan_workday(sim, midnight, first)
        else:
            self._plan_commutes(sim, midnight, first)
        if self.lunch is not None:
            self._plan_lunch(sim, midnight)

    def _plan_lunch(self, sim: Simulation, midnight: float) -> None:
        for r in self.residents:
            if self.rng.random() < self.lunch.share:
                t = midnight + self.lunch.sample(self.rng)
                if t > sim.time:
                    self._push(t, "lunch", r.id, (self._lunch_gen,))

    def set_lunch(self, sim: Simulation, lunch: RushHour | None) -> None:
        """Switch the lunch peak on (`lunch`) or off (None) during a run. Switched on, the rest of
        today's lunches are planned now; switched off, planned lunches don't happen, but anyone
        already out for lunch still comes back."""
        self._lunch_gen += 1
        self.lunch = lunch
        tod = sim.time_of_day
        if lunch is not None and self._started and tod is not None:
            self._plan_lunch(sim, sim.time - tod)

    def _plan_commutes(self, sim: Simulation, midnight: float, first: bool) -> None:
        """Residents: out in the morning rush, home in the evening rush."""
        rng, now = self.rng, sim.time
        m = self.out_rush.share if self.out_rush else 0.0
        e = self.in_rush.share if self.in_rush else 0.0
        lo, hi = self.late_window
        for r in self.residents:
            leaves = m > 0 and rng.random() < m
            if self.in_rush is None:
                rush_home = False
            elif leaves:
                rush_home = e >= m or rng.random() < e / m
            else:   # more come home than left: the rest were already out (e.g. a night shift)
                rush_home = e > m and rng.random() < (e - m) / (1 - m)
            if not leaves and not rush_home:
                continue
            back = midnight + (self.in_rush.sample(rng) if rush_home else rng.uniform(lo, hi))
            leave = midnight + self.out_rush.sample(rng) if leaves else None
            if leave is not None and back <= leave:
                back = leave + rng.uniform(6 * HOUR, 10 * HOUR)
            if leave is not None and leave > now:
                deadline = midnight + self.out_rush.end + HOUR
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

    def _plan_workday(self, sim: Simulation, midnight: float, first: bool) -> None:
        """An office: in during the morning rush, out during the evening rush. With a morning
        rush, people start the day out of the building; without one, everyone is at work."""
        rng, now = self.rng, sim.time
        a = self.in_rush.share if self.in_rush else 0.0
        d = self.out_rush.share if self.out_rush else 0.0
        lo, hi = self.late_window
        for r in self.residents:
            arrives = a > 0 and rng.random() < a
            if self.out_rush is None:
                rush_out = False
            elif arrives:
                rush_out = d >= a or rng.random() < d / a
            else:   # more leave than came in: the rest were already here (e.g. a night shift)
                rush_out = d > a and rng.random() < (d - a) / (1 - a)
            arrive = midnight + self.in_rush.sample(rng) if arrives else None
            leave = None
            if arrives or rush_out:
                leave = midnight + (self.out_rush.sample(rng) if rush_out else rng.uniform(lo, hi))
                if arrive is not None and leave <= arrive:
                    leave = arrive + rng.uniform(6 * HOUR, 10 * HOUR)
            if first:
                if self.in_rush is None:
                    inside = leave is None or now < leave
                elif arrive is not None:
                    inside = arrive <= now < leave
                else:
                    inside = rush_out and now < leave
                if not inside and r.state is State.HOME:
                    exit_floor = self._pick_exit(r)
                    if exit_floor != r.home:
                        self._remove_home(r.id)
                        r.state, r.floor, r.commuting = State.AWAY, exit_floor, True
                        r.token += 1
            if arrive is not None and arrive > now:
                self._push(arrive, "return", r.id, (r.token,))
            if leave is not None and leave > now:
                r.leave_at = leave
                self._push(leave, "leave", r.id, (None, leave))
            # Today's hours at work, and the outings in them.
            if arrive is not None:
                r.at_work = (arrive, leave)
            elif rush_out or self.in_rush is None:      # here already (or everyone always is)
                r.at_work = (midnight, leave if leave is not None else midnight + DAY)
            else:
                r.at_work = None                        # not in today
            if r.at_work is not None:
                self._plan_outings(r, midnight, *r.at_work, now)

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
                r.leave_at = None
                if self._trip(sim, r, self._pick_exit(r), "commute_out") is None:
                    r.commuting, r.planned_return = False, None    # lives on the exit floor
            elif self.office:
                return      # already out, or busy elsewhere: they leave once back at work (on_arrive)
            elif ev.time + 300 <= deadline:
                self._push(ev.time + 300, "leave", r.id, ev.data)    # busy: try again shortly
        elif ev.kind == "return":
            token = ev.data[0]
            if r.state is not State.AWAY or r.token != token:
                return                      # stale: they have moved since this was planned
            purpose = ev.data[1] if len(ev.data) > 1 else "commute_in" if r.commuting else "outing_return"
            self._trip(sim, r, r.home, purpose)
        elif ev.kind == "outing":
            gen, until = ev.data
            if gen != self._outing_gen:
                return                      # planned before outings per day changed
            if r.state is State.HOME:
                dest = _pick_destination(r.rng, self.shares, self.home_floors, r.home,
                                         len(sim.building), self._reachable(r.home), self._near(r.home))
                self._trip(sim, r, dest, "outing")
            elif not r.commuting and ev.time + 300 < until:
                self._push(ev.time + 300, "outing", r.id, ev.data)   # busy: try again shortly
        elif ev.kind == "lunch":
            if ev.data != (self._lunch_gen,):
                return                      # planned before lunch was switched off or on
            if r.state is State.HOME:       # at home (at work), not busy elsewhere
                self._trip(sim, r, self._pick_lunch(r), "lunch")

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
                      n_floors: int, reach: frozenset[int] | None = None,
                      near: frozenset[int] | None = None) -> int:
    """A special floor by its share; otherwise another residential floor. With `reach`, only
    floors in it, and with `near`, other residential floors only from those; `home` if there
    are none (no trip)."""
    ok = (lambda f: f != home) if reach is None else (lambda f: f != home and f in reach)  # noqa: E731
    x = rng.random()
    for floor, share in shares.items():
        if x < share:
            if ok(floor):
                return floor
            break
        x -= share
    neighbours = [f for f in home_floors if ok(f) and (near is None or f in near)]
    if neighbours:
        return rng.choice(neighbours)
    others = [f for f in range(n_floors) if ok(f)]
    return rng.choice(others) if others else home


def _weighted(rng, weights: dict[int, float], reach: frozenset[int] | None) -> int:
    """A floor picked by weight, from those in `reach` if any of them are."""
    if reach is not None:
        weights = {f: w for f, w in weights.items() if f in reach} or weights
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _poisson(rng, mean: float) -> int:
    """Number of arrivals in one tick (Knuth's method; fine for small means)."""
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


# --- visitors ---------------------------------------------------------------------------------
# Visitors come in groups (families, friends, a few tours): sizes and how often each happens.
VISITOR_GROUPS = {1: 0.20, 2: 0.40, 3: 0.15, 4: 0.20, 5: 0.05}     # 2.5 people on average
# Share of visitors who come at an even rate through opening hours; the rest follow the peak.
VISITOR_STEADY = 0.3


class VisitorTraffic:
    """People from outside the building who come to see one floor (an observatory, say).

    Each day about `per_day` visitors arrive at `entrance`, in groups (VISITOR_GROUPS), at times
    that follow `arrivals` (a RushHour: opening, busiest time, last entry), with VISITOR_STEADY
    of them spread evenly from opening to last entry instead. A group rides up to
    `up_floor`, waits for everyone to get there, stays for its visit (on average `visit_mean`
    seconds, at least half of that or 30 minutes), then rides down from `down_floor` together.
    Nobody stays past `close`. Only in runs with a time of day.

    Like ResidentTraffic, every plan (who comes when, how long they stay) is drawn from the
    source's own generator when the day is planned, so dispatchers compared on one seed see
    the same visitors.
    """

    def __init__(self, per_day: float, entrance: int, up_floor: int, down_floor: int,
                 arrivals: RushHour, close: float, visit_mean: float, seed: int = 0) -> None:
        if entrance in (up_floor, down_floor):
            raise ValueError("visitors need an entrance apart from the floor they visit")
        if arrivals.end > close:
            raise ValueError("last entry has to be before closing time")
        self.per_day = per_day
        self.entrance, self.up_floor, self.down_floor = entrance, up_floor, down_floor
        self.arrivals = arrivals
        self.close = close
        self.visit_mean = visit_mean
        self.shortest = min(30 * 60, visit_mean / 2)
        self.rng = random.Random(f"visitors-{seed}")
        self.inside = 0                                  # visitors in the building right now
        self._events: list[_Event] = []
        self._seq = 0
        self._started = False
        self._group_of: dict[int, int] = {}              # person id -> group id
        self._groups: dict[int, list] = {}               # group id -> [size, arrived up, leave at]
        self._next_group = 0

    def expected(self, time_of_day: float | None) -> dict[str, float]:
        """Expected visitor trips per minute: up, and down about one visit later."""
        if time_of_day is None:
            return {"total": 0.0, "leaving": 0.0, "returning": 0.0}
        up = self.per_day * self._density(time_of_day)
        down = self.per_day * self._density((time_of_day - self.visit_mean) % DAY)
        return {"total": (up + down) * 60, "leaving": 0.0, "returning": 0.0}

    def _density(self, time_of_day: float) -> float:
        """Share of the day's visitors arriving per second at a time of day."""
        a = self.arrivals
        steady = VISITOR_STEADY / (a.end - a.start) if a.start <= time_of_day < a.end else 0.0
        return steady + (1 - VISITOR_STEADY) * a.density(time_of_day)

    # --- the simulation calls these ---------------------------------------------------------
    def step(self, sim: Simulation, dt: float) -> None:
        if not self._started:
            self._started = True
            tod = sim.time_of_day
            if tod is None:
                return
            midnight = sim.time - tod
            self._plan_day(sim, midnight, first=True)
            self._push(midnight + DAY, "day")
        while self._events and self._events[0].time <= sim.time:
            ev = heapq.heappop(self._events)
            if ev.kind == "day":
                self._plan_day(sim, ev.time, first=False)
                self._push(ev.time + DAY, "day")
            elif ev.kind == "arrive":
                self._arrive(sim, *ev.data)
            elif ev.kind == "leave":
                self._leave(sim, ev.data[0])

    def on_arrive(self, sim: Simulation, person: Person) -> None:
        gid = self._group_of.pop(person.journey, None)      # journeys may change cars on the way
        if gid is None:
            return
        if person.purpose == "visit_return":
            self.inside -= 1
            return
        group = self._groups[gid]
        group[1] += 1
        if group[1] == group[0]:                         # everyone's up: the visit starts
            stay, until = group[2]
            leave = min(sim.time + stay, until)
            self._push(max(leave, sim.time + 300), "leave", data=(gid,))

    # --- planning ------------------------------------------------------------------------------
    def _plan_day(self, sim: Simulation, midnight: float, first: bool) -> None:
        rng, now = self.rng, sim.time
        count = max(0, round(rng.gauss(self.per_day, math.sqrt(self.per_day))))
        sizes, weights = list(VISITOR_GROUPS), list(VISITOR_GROUPS.values())
        while count > 0:
            size = min(rng.choices(sizes, weights)[0], count)
            count -= size
            a = self.arrivals
            at = midnight + (rng.uniform(a.start, a.end) if rng.random() < VISITOR_STEADY else a.sample(rng))
            stay = self.shortest + rng.expovariate(1 / (self.visit_mean - self.shortest))
            until = midnight + self.close - rng.uniform(0, 15 * 60)   # out by closing time
            if at > now:
                self._push(at, "arrive", data=(size, stay, until))
            elif first and min(at + stay, until) > now:
                # Already up there when the run begins: they only have their way down left.
                gid = self._new_group(size, stay, until)
                self.inside += size
                self._push(max(min(at + stay, until), now + 60), "leave", data=(gid,))

    def _new_group(self, size: int, stay: float, until: float) -> int:
        gid = self._next_group
        self._next_group += 1
        self._groups[gid] = [size, 0, (stay, until)]
        return gid

    def _arrive(self, sim: Simulation, size: int, stay: float, until: float) -> None:
        gid = self._new_group(size, stay, until)
        self.inside += size
        for _ in range(size):
            p = sim.spawn_person(self.entrance, self.up_floor, purpose="visit")
            self._group_of[p.id] = gid

    def _leave(self, sim: Simulation, gid: int) -> None:
        size = self._groups.pop(gid)[0]
        for _ in range(size):
            p = sim.spawn_person(self.down_floor, self.entrance, purpose="visit_return")
            self._group_of[p.id] = gid

    def _push(self, time: float, kind: str, data: tuple = ()) -> None:
        self._seq += 1
        heapq.heappush(self._events, _Event(time, self._seq, kind, -1, data))
