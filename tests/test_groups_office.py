"""Elevator groups (banks), office buildings and the lunch peak."""

import unittest
from collections import Counter

from elevator_sim import Building, Direction, Elevator, Settings, SettingsError, Simulation, make_simulation
import tempfile
from pathlib import Path

from elevator_sim.clock import RunPlan, parse_time
from elevator_sim.presets import BUILTIN_PRESETS
from elevator_sim.server import Session
from elevator_sim.settings import SettingsStore

H = 3600


def preset(name):
    return next(p.settings for p in BUILTIN_PRESETS if p.name == name)


def two_banks(**extra):
    return Settings.from_dict({
        "floors": 20,
        "special_floors": [{"floor": 1, "name": "Lobby", "kind": "lobby", "share": 50}],
        "elevator_groups": [
            {"name": "Low", "cars": 2, "floors": "1-10", "capacity": 10, "speed": 2},
            {"name": "High", "cars": 2, "floors": "1, 11-20", "capacity": 12, "speed": 3},
        ],
        **extra,
    })


class TestGroupSettings(unittest.TestCase):
    def test_old_settings_become_one_group(self):
        s = Settings.from_dict({"elevators": 4, "capacity": 12, "speed": 1.5})
        self.assertEqual(len(s.elevator_groups), 1)
        g = s.elevator_groups[0]
        self.assertEqual((g.cars, g.capacity, g.speed, g.floors), (4, 12, 1.5, "all"))
        self.assertEqual(s.elevators, 4)

    def test_defaults_are_one_group_of_six(self):
        s = Settings()
        self.assertEqual(s.elevators, 6)
        self.assertEqual(s.elevator_groups[0].served(s.floors), None)

    def test_floor_lists_are_tidied(self):
        s = two_banks()
        s = Settings.from_dict({**s.to_dict(), "elevator_groups": [
            {"name": "A", "cars": 2, "floors": "1,2, 3 - 10", "capacity": 9, "speed": 2},
            {"name": "B", "cars": 2, "floors": " 1 11-20 ", "capacity": 9, "speed": 2},
        ]})
        self.assertEqual([g.floors for g in s.elevator_groups], ["1-10", "1, 11-20"])
        self.assertEqual(s.elevator_groups[1].served(20), frozenset({0, *range(10, 20)}))

    def test_errors_name_the_field(self):
        base = two_banks().to_dict()
        groups = base["elevator_groups"]
        cases = [
            ([{**groups[0], "floors": "1-25"}, groups[1]], "elevator_groups.0.floors"),
            ([{**groups[0], "floors": "lobby"}, groups[1]], "elevator_groups.0.floors"),
            ([groups[0], {**groups[1], "floors": "7"}], "elevator_groups.1.floors"),
            ([groups[0], {**groups[1], "cars": 0}], "elevator_groups.1.cars"),
            ([groups[0]], "elevator_groups"),                                    # floors 11-20 unserved
            ([groups[0], {**groups[1], "floors": "11-20"}], "elevator_groups"),  # 11-20 can't reach the lobby
            ([{**groups[0], "cars": 40}, {**groups[1], "cars": 40}, {**groups[1], "cars": 40}],
             "elevator_groups.cars_total"),
            ([], "elevator_groups"),
        ]
        for value, field in cases:
            with self.subTest(field=field, value=value), self.assertRaises(SettingsError) as ctx:
                Settings.from_dict({**base, "elevator_groups": value})
            self.assertEqual(ctx.exception.field, field)

    def test_unserved_floors_are_named(self):
        with self.assertRaises(SettingsError) as ctx:
            Settings.from_dict({**two_banks().to_dict(), "elevator_groups": [
                {"name": "A", "cars": 2, "floors": "1-10, 12", "capacity": 9, "speed": 2}]})
        self.assertIn("floors 11 and 13-20", str(ctx.exception))

    def test_cars_get_their_group(self):
        sim, _, lobby = make_simulation(two_banks())
        self.assertEqual([c.group for c in sim.elevators], [0, 0, 1, 1])
        self.assertEqual([c.id for c in sim.elevators], [0, 1, 2, 3])
        self.assertEqual([c.capacity for c in sim.elevators], [10, 10, 12, 12])
        self.assertTrue(all(c.position == lobby for c in sim.elevators))
        self.assertEqual(len(sim.managers), 2)
        with self.assertRaises(RuntimeError):
            sim.manager


class TestGroupSimulation(unittest.TestCase):
    def make(self):
        cars = [Elevator(id=0, served_floors=frozenset(range(0, 10)), group=0),
                Elevator(id=1, served_floors=frozenset({0, *range(10, 20)}), group=1)]
        return Simulation(Building.simple(20), cars, dispatcher_factory=lambda: __import__(
            "elevator_sim").NearestCarDispatcher())

    def test_each_group_has_its_own_hall_button(self):
        sim = self.make()
        low = sim.spawn_person(0, 5)
        high = sim.spawn_person(0, 15)
        self.assertEqual((low.group, high.group), (0, 1))
        sim.step()
        self.assertTrue(sim.managers[0].is_lit(0, Direction.UP))
        self.assertTrue(sim.managers[1].is_lit(0, Direction.UP))

    def test_people_ride_only_their_groups_cars(self):
        sim = self.make()
        people = [sim.spawn_person(0, f) for f in (3, 15, 8, 19)]
        sim.run(300)
        sim.check_invariants()
        for p in people:
            self.assertIsNotNone(p.arrive_time, p)
            self.assertEqual(sim.elevators[p.car_id].group, p.group)

    def test_changing_cars_between_groups(self):
        sim = self.make()
        arrivals = []
        sim.traffic.append(type("Watch", (), {"step": lambda self, sim, dt: None,
                                              "on_arrive": lambda self, sim, p: arrivals.append(p)})())
        first = sim.spawn_person(5, 15)                 # low bank down to 1, high bank up to 15
        self.assertEqual((first.requested_floor, first.onward, first.group), (0, (15,), 0))
        self.assertEqual(first.final_floor, 15)
        sim.run(300)
        sim.check_invariants()
        legs = sorted((p for p in sim.people.values() if p.journey == first.id), key=lambda p: p.leg)
        self.assertEqual([(p.origin_floor, p.requested_floor, p.group, p.leg) for p in legs],
                         [(5, 0, 0, 1), (0, 15, 1, 2)])
        self.assertGreaterEqual(legs[1].spawn_time - legs[0].arrive_time, 15 - 1e-6)   # the walk across
        self.assertEqual(legs[1].journey_start, legs[0].spawn_time)
        self.assertEqual(arrivals, [legs[1]])           # traffic only hears of the journey's end

    def test_no_route_between_unconnected_groups(self):
        cars = [Elevator(id=0, served_floors=frozenset(range(0, 10)), group=0),
                Elevator(id=1, served_floors=frozenset(range(10, 20)), group=1)]
        sim = Simulation(Building.simple(20), cars, dispatcher_factory=lambda: __import__(
            "elevator_sim").NearestCarDispatcher())
        self.assertIsNone(sim.route(5, 15))
        with self.assertRaises(ValueError):
            sim.spawn_person(5, 15)
        for _ in range(30):     # a random destination is always one they can get to
            self.assertLess(sim.spawn_person(5).final_floor, 10)

    def test_fewest_changes_then_shortest(self):
        # Lobby shuttle to a sky lobby on 10, locals above it; and a low bank from the lobby.
        cars = [Elevator(id=0, served_floors=frozenset(range(0, 8)), group=0),
                Elevator(id=1, served_floors=frozenset({0, 10}), group=1),
                Elevator(id=2, served_floors=frozenset(range(10, 20)), group=2)]
        sim = Simulation(Building.simple(20), cars, dispatcher_factory=lambda: __import__(
            "elevator_sim").NearestCarDispatcher())
        self.assertEqual(sim.route(0, 15), (10,))
        self.assertEqual(sim.route(15, 0), (10,))
        self.assertEqual(sim.route(5, 15), (0, 10))      # two changes
        self.assertEqual(sim.route(3, 6), ())

    def test_one_dispatcher_for_several_groups_is_refused(self):
        cars = [Elevator(id=0, group=0), Elevator(id=1, group=1)]
        with self.assertRaises(ValueError):
            Simulation(Building.simple(5), cars, __import__("elevator_sim").NearestCarDispatcher())

    def test_most_specific_group_is_used(self):
        # An express bank to the top, and a local bank everywhere: lobby-to-top riders take the express.
        cars = [Elevator(id=0, group=0), Elevator(id=1, served_floors=frozenset({0, 19}), group=1)]
        sim = Simulation(Building.simple(20), cars, dispatcher_factory=lambda: __import__(
            "elevator_sim").NearestCarDispatcher())
        self.assertEqual(sim.spawn_person(0, 19).group, 1)
        self.assertEqual(sim.spawn_person(0, 5).group, 0)


class TestResidentsWithGroups(unittest.TestCase):
    def test_tower_day_strands_nobody(self):
        sim, _, _ = make_simulation(preset("Tower, 60 floors"), RunPlan.between(7 * H, 10 * H))
        sim.run(3 * H)
        sim.check_invariants()
        for p in sim.people.values():
            self.assertIn(p.group, sim.groups_serving(p.origin_floor, p.requested_floor))
        waiting = [sim.time - p.spawn_time for p in sim.people.values() if p.board_time is None]
        self.assertLess(max(waiting, default=0), 300)
        self.assertEqual({p.group for p in sim.people.values()}, {0, 1})


class TestOffice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = Settings.from_dict({**two_banks().to_dict(), "office": True, "residents": 400,
                                       "morning_share": 90, "evening_share": 90,
                                       "lunch_enabled": True, "lunch_share": 50})
        cls.sim, cls.traffic, _ = make_simulation(settings, RunPlan.between(5 * H, 21 * H))
        cls.start_census = None
        cls.sim.step()
        cls.start_census = cls.traffic.census()
        cls.sim.run(16 * H - cls.sim.dt)
        cls.trips = Counter((p.purpose, p.direction) for p in cls.sim.people.values())

    def test_rushes_are_swapped(self):
        s = Settings.from_dict({"office": True})
        self.assertFalse(s.rush_hour("morning").leaving)
        self.assertTrue(s.rush_hour("evening").leaving)
        self.assertTrue(Settings().rush_hour("morning").leaving)

    def test_people_start_the_day_out(self):
        self.assertGreater(self.start_census.out, 0.85 * 400)
        self.assertLess(self.start_census.home, 0.15 * 400)

    def test_up_in_the_morning_down_in_the_evening(self):
        ins = [p for p in self.sim.people.values() if p.purpose == "commute_in"]
        outs = [p for p in self.sim.people.values() if p.purpose == "commute_out"]
        self.assertAlmostEqual(len(ins), 360, delta=40)
        self.assertAlmostEqual(len(outs), 360, delta=40)
        self.assertTrue(all(p.direction is Direction.UP for p in ins))
        self.assertTrue(all(p.direction is Direction.DOWN for p in outs))
        clock = lambda p: 5 * H + p.spawn_time  # noqa: E731
        self.assertTrue(all(7 * H <= clock(p) <= 10 * H for p in ins))
        self.assertGreater(sum(16 * H <= clock(p) <= 19 * H for p in outs), 0.9 * len(outs))

    def test_lunch_peak(self):
        lunches = [p for p in self.sim.people.values() if p.purpose == "lunch"]
        self.assertAlmostEqual(len(lunches), 0.5 * 360, delta=50)
        self.assertTrue(all(11.5 * H <= 5 * H + p.spawn_time <= 13.5 * H for p in lunches))
        self.assertTrue(all(p.requested_floor == 0 for p in lunches))     # the only lunch floor
        self.assertGreater(self.trips[("lunch_return", Direction.UP)], 0.9 * len(lunches))

    def test_outings_per_person_at_work_match_the_setting(self):
        came = {p.resident_id for p in self.sim.people.values() if p.purpose == "commute_in"}
        outings = [p for p in self.sim.people.values() if p.purpose == "outing" and p.leg == 1]
        self.assertTrue(all(p.resident_id in came for p in outings))     # nobody who isn't in
        self.assertAlmostEqual(len(outings) / len(came), 1.5, delta=0.2)

    def test_everyone_has_gone_home_by_night(self):
        census = self.traffic.census()
        self.assertEqual(census.home, 0)
        self.assertGreater(census.out, 0.95 * 400)
        self.sim.check_invariants()

    def test_expected_curve_follows_the_day(self):
        rate = lambda h: self.traffic.expected(h * H)["total"]  # noqa: E731
        self.assertLess(rate(3), 0.5)
        self.assertGreater(rate(8.5), 3 * rate(3) + 1)
        self.assertGreater(self.traffic.expected(8.5 * H)["returning"], 0.5)   # arriving for work
        self.assertGreater(self.traffic.expected(17.5 * H)["leaving"], 0.5)    # leaving work


class TestOutingsPerPerson(unittest.TestCase):
    def test_residents_average_the_setting(self):
        sim, traffic, _ = make_simulation(Settings.from_dict({"residents": 300, "outings": 4}))
        sim.run(6 * H)                                 # steady: a quarter of a day
        outings = sum(p.purpose == "outing" for p in sim.people.values())
        self.assertAlmostEqual(outings / 300, 4 / 4, delta=0.2)

    def test_changing_outings_mid_day_in_an_office(self):
        settings = Settings.from_dict({**two_banks().to_dict(), "office": True, "residents": 300,
                                       "outings": 1.5})
        plan = RunPlan.between(6 * H, 20 * H)
        runs = [make_simulation(settings, plan) for _ in range(2)]
        for sim, _, _ in runs:
            sim.run(6 * H)                             # to noon
        before = [sum(p.purpose == "outing" for p in sim.people.values()) for sim, _, _ in runs]
        self.assertEqual(before[0], before[1])
        runs[1][1].set_outings(runs[1][0], 4.5)
        for sim, _, _ in runs:
            sim.run(8 * H)
        after = [sum(p.purpose == "outing" for p in sim.people.values()) - b
                 for (sim, _, _), b in zip(runs, before)]
        self.assertGreater(after[1], 2.2 * after[0])   # about 3 times as many from noon on


class TestLunchAtHome(unittest.TestCase):
    def test_residents_go_out_for_lunch_too(self):
        settings = Settings.from_dict({"lunch_enabled": True, "lunch_share": 30})
        sim, _, _ = make_simulation(settings, RunPlan.between(11 * H, 15 * H))
        sim.run(4 * H)
        lunches = [p for p in sim.people.values() if p.purpose == "lunch"]
        self.assertGreater(len(lunches), 40)      # 30% of those home at noon; most left at 7-10
        kinds = {sim.building[p.requested_floor].kind.name for p in lunches}
        self.assertLessEqual(kinds, {"LOBBY", "PARKING", "RETAIL"})

    def test_lunch_off_leaves_the_day_unchanged(self):
        plan = RunPlan.between(7 * H, 9 * H)
        a, _, _ = make_simulation(Settings(), plan)
        b, _, _ = make_simulation(Settings.from_dict({"lunch_start": "11:00"}), plan)
        a.run(H)
        b.run(H)
        self.assertEqual(list(a.trip_rows()), list(b.trip_rows()))


class TestClosedFloors(unittest.TestCase):
    """Floors with no stops: mechanical floors and skipped floor numbers."""

    def settings(self, **extra):
        return Settings.from_dict({
            "floors": 30, "closed_floors": "2-5, 28 - 29",
            "special_floors": [{"floor": 1, "name": "Lobby", "kind": "lobby", "share": 50},
                               {"floor": 30, "name": "Roof", "kind": "amenity", "share": 5}],
            "elevator_groups": [{"name": "Low", "cars": 2, "floors": "1, 6-17", "capacity": 10, "speed": 2},
                                {"name": "High", "cars": 2, "floors": "1, 18-27, 30", "capacity": 10, "speed": 3}],
            **extra,
        })

    def test_no_group_has_to_serve_them(self):
        s = self.settings()
        self.assertEqual(s.closed_floors, "2-5, 28-29")
        self.assertEqual(s.closed_numbers(), {2, 3, 4, 5, 28, 29})

    def test_nobody_lives_or_goes_there(self):
        sim, traffic, _ = make_simulation(self.settings(residents=200, outings=6))
        closed = {1, 2, 3, 4, 27, 28}
        self.assertFalse(closed & set(traffic.home_floors))
        self.assertEqual({sim.building[f].kind.name for f in closed}, {"CLOSED"})
        sim.run(2 * H)
        used = {f for p in sim.people.values() for f in (p.origin_floor, p.requested_floor)}
        self.assertFalse(closed & used)
        self.assertIn("closed", {f["kind"] for f in sim.layout()["floors"]})

    def test_errors(self):
        cases = [
            ({"closed_floors": "2-5, 31"}, "closed_floors"),
            ({"closed_floors": "all"}, "closed_floors"),
            ({"closed_floors": "1-5"}, "special_floors.0.floor"),     # the lobby
            ({"closed_floors": "2-4"}, "elevator_groups"),            # floor 5 is now unserved
        ]
        for extra, field in cases:
            with self.subTest(extra=extra), self.assertRaises(SettingsError) as ctx:
                self.settings(**extra)
            self.assertEqual(ctx.exception.field, field)

    def test_one_world_trade_center(self):
        s = preset("One World Trade Center")
        self.assertEqual((s.floors, s.elevators, s.office), (104, 57, True))
        sim, traffic, _ = make_simulation(s)
        self.assertEqual((traffic.home_floors[0] + 1, traffic.home_floors[-1] + 1), (20, 90))
        self.assertNotIn(63, traffic.home_floors)                 # the sky lobby
        pod = sim.spawn_person(0, 101)
        self.assertEqual(s.elevator_groups[pod.group].name, "Sky Pods")
        while pod.arrive_time is None:
            sim.step()
        self.assertAlmostEqual(pod.ride_time, 47, delta=5)        # the published ride
        # Above 64 means a shuttle to the sky lobby and a change there; 20-63 rides straight up.
        self.assertEqual(sim.route(0, 69), (63,))
        self.assertEqual(sim.route(84, 0), (63,))
        self.assertEqual(sim.route(0, 40), ())
        self.assertEqual(sim.route(25, 63), (0,))                 # the sky lobby, from a low floor
        up = sim.spawn_person(0, 69)
        self.assertEqual(s.elevator_groups[up.group].name, "Sky lobby shuttles")


class TestVisitors(unittest.TestCase):
    VISITORS = {"visitors_enabled": True, "visitors_per_day": 600, "visitor_entrance": 1,
                "visitor_floor": 20, "visitor_exit_floor": 19, "visitors_open": "09:00",
                "visitors_peak": "15:00", "visitors_last_entry": "20:15", "visitors_close": "21:00",
                "visit_minutes": 60}

    @classmethod
    def setUpClass(cls):
        settings = two_banks(residents=100, **cls.VISITORS)
        cls.sim, _, _ = make_simulation(settings, RunPlan.between(8 * H, 22 * H))
        cls.sim.run(14 * H)
        cls.ups = [p for p in cls.sim.people.values() if p.purpose == "visit"]
        cls.downs = [p for p in cls.sim.people.values() if p.purpose == "visit_return"]

    def clock(self, p, at="spawn_time"):
        return 8 * H + getattr(p, at)

    def test_about_the_day_s_number_come_and_all_leave(self):
        self.assertAlmostEqual(len(self.ups), 600, delta=90)
        self.assertEqual(len(self.downs), len(self.ups))
        self.assertTrue(all((p.origin_floor, p.requested_floor) == (0, 19) for p in self.ups))
        self.assertTrue(all((p.origin_floor, p.requested_floor) == (18, 0) for p in self.downs))
        visitors = next(t for t in self.sim.traffic if hasattr(t, "inside"))
        self.assertEqual(visitors.inside, 0)

    def test_open_hours(self):
        self.assertTrue(all(9 * H <= self.clock(p) <= 20.25 * H for p in self.ups))
        self.assertTrue(all(self.clock(p) <= 21 * H for p in self.downs))
        morning = sum(self.clock(p) < 11 * H for p in self.ups)
        afternoon = sum(13 * H <= self.clock(p) < 15 * H for p in self.ups)
        self.assertGreater(morning, 0.05 * len(self.ups))          # never empty while it's open
        self.assertGreater(afternoon, morning)                      # busiest after midday

    def test_visits_last_a_while(self):
        first_down = min(self.clock(p) for p in self.downs)
        first_up = min(self.clock(p, "arrive_time") for p in self.ups)
        self.assertGreaterEqual(first_down - first_up, 30 * 60 - 1)

    def test_steady_runs_have_no_visitors(self):
        sim, _, _ = make_simulation(two_banks(residents=50, **self.VISITORS))
        sim.run(H)
        self.assertFalse([p for p in sim.people.values() if p.purpose.startswith("visit")])

    def test_same_visitors_for_every_algorithm(self):
        plan = RunPlan.between(14 * H, 15 * H)
        arrivals = []
        for name in ("nearest", "lookahead"):
            sim, _, _ = make_simulation(two_banks(residents=50, dispatcher=name, **self.VISITORS), plan)
            sim.run(H)
            arrivals.append(sorted(p.spawn_time for p in sim.people.values() if p.purpose == "visit"))
        self.assertEqual(arrivals[0], arrivals[1])

    def test_errors(self):
        cases = [
            ({"visitor_floor": 1}, "visitor_entrance"),           # the entrance is the floor they visit
            ({"visitor_floor": 25}, "visitor_floor"),             # the building has 20 floors
            ({"visitors_close": "20:00"}, "visitors_close"),      # before last entry
            ({"visitors_peak": "08:00"}, "visitors_peak"),        # before opening
        ]
        for extra, field in cases:
            with self.subTest(extra=extra), self.assertRaises(SettingsError) as ctx:
                two_banks(**{**self.VISITORS, **extra})
            self.assertEqual(ctx.exception.field, field)
        # From 5 to 20 takes a change of cars at the lobby, which visitors can do too.
        two_banks(**{**self.VISITORS, "visitor_entrance": 5})
        # Switched off, visitor settings aren't checked.
        two_banks(**{**self.VISITORS, "visitors_enabled": False, "visitor_floor": 500})


class TestLunchControls(unittest.TestCase):
    """The Lunch rush button and the live Lunch peak checkbox on the Simulation tab."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.session = Session(SettingsStore(Path(self.dir.name) / "settings.json"))

    def tearDown(self):
        self.dir.cleanup()

    def advance(self, seconds):
        with self.session.lock:
            self.session._advance(seconds)

    def lunches(self, after=0.0):
        return [p for p in self.session.sim.people.values() if p.purpose == "lunch" and p.spawn_time > after]

    def test_lunch_run_has_lunch_on(self):
        s = self.session
        self.assertFalse(s.settings.lunch_enabled)
        s.start_run({"mode": "lunch"})
        self.assertEqual((s.plan.start, s.plan.stop), (parse_time("11:00"), parse_time("14:00")))
        self.assertEqual(s.plan.describe(), "lunch rush, 11:00 to 14:00")
        self.assertTrue(s.lunch_on and s.traffic.lunch is not None)
        self.advance(2 * H)
        self.assertGreater(len(self.lunches()), 40)
        s.restart()                                   # running it again keeps lunch on
        self.assertTrue(s.traffic.lunch is not None)
        s.start_run({"mode": "morning"})              # other runs follow Settings
        self.assertFalse(s.lunch_on or s.traffic.lunch)

    def test_rush_info_lists_lunch(self):
        rushes = {r["name"]: r for r in self.session._rush_info()}
        self.assertEqual(list(rushes), ["morning", "evening", "lunch"])
        self.assertTrue(rushes["lunch"]["enabled"])
        self.assertEqual(rushes["lunch"]["run_start"], parse_time("11:00"))

    def test_switching_lunch_off_mid_run(self):
        s = self.session
        s.start_run({"mode": "lunch"})
        self.advance(75 * 60)                          # 12:15, the peak
        out_now = len(self.lunches())
        self.assertGreater(out_now, 10)
        t = s.sim.time
        s.control({"lunch": False})
        self.assertIsNone(s.traffic.lunch)
        self.advance(105 * 60)
        self.assertEqual(self.lunches(after=t), [])
        backs = [p for p in s.sim.people.values() if p.purpose == "lunch_return"]
        self.assertGreater(len(backs), 0.8 * out_now)  # whoever was out still comes back

    def test_switching_lunch_on_mid_run(self):
        s = self.session
        s.start_run({"mode": "custom", "start": "10:30", "stop": "14:30"})
        self.advance(H)                                # 11:30
        self.assertEqual(self.lunches(), [])
        before = s.traffic.expected(parse_time("12:15"))["total"]
        s.control({"lunch": True})
        self.assertGreater(s.traffic.expected(parse_time("12:15"))["total"], before)
        self.assertIn('"lunch": true', s.frame_json)
        self.advance(2 * H)
        self.assertGreater(len(self.lunches()), 40)
        gen = s.traffic._lunch_gen
        s.control({"lunch": True})                     # already on: nothing is planned again
        self.assertEqual(s.traffic._lunch_gen, gen)

    def test_saving_settings_follows_lunch(self):
        s = self.session
        s.start_run({"mode": "lunch"})
        s.save_settings({"lunch_start": "12:00", "lunch_peak": "12:30", "lunch_end": "13:00"})
        self.assertEqual((s.plan.mode, s.plan.start), ("lunch", parse_time("11:30")))
        self.assertTrue(s.lunch_on)


if __name__ == "__main__":
    unittest.main()
