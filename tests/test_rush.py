import contextlib
import io
import json
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path

from elevator_sim import (
    DAY, ResidentTraffic, RunPlan, RushHour, Settings, SettingsError, SettingsStore, daily_activity,
    format_time, make_simulation, parse_time,
)
from elevator_sim import report as report_cli
from elevator_sim.server import SPEEDS, Session, make_handler
from elevator_sim.simulation import Simulation

H = 3600


def morning(**kw):
    args = dict(name="morning", start=7 * H, peak=8.5 * H, end=10 * H, share=0.55, leaving=True)
    return RushHour(**{**args, **kw})


def run_plan(settings, plan):
    sim, traffic, _ = make_simulation(settings, plan)
    sim.run(plan.duration)
    sim.check_invariants()
    return sim, traffic


def purposes(sim):
    return Counter(p.purpose for p in sim.people.values())


class TestClock(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(parse_time("07:30"), 7 * H + 1800)
        self.assertEqual(parse_time("7:05"), 7 * H + 300)
        self.assertEqual(parse_time("23:59:59"), 23 * H + 59 * 60)   # seconds are dropped
        for bad in ("24:00", "7:60", "7", "seven", "", None, 730):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_time(bad)
        self.assertEqual(format_time(7 * H + 300), "7:05")
        self.assertEqual(format_time(DAY + 90, with_seconds=True), "0:01:30")   # wraps at midnight

    def test_run_plans(self):
        plan = RunPlan.between(parse_time("06:00"), parse_time("20:00"))
        self.assertEqual((plan.start, plan.duration, plan.stop), (6 * H, 14 * H, 20 * H))
        overnight = RunPlan.between(22 * H, 2 * H)
        self.assertEqual((overnight.duration, overnight.stop), (4 * H, 2 * H))
        self.assertEqual(RunPlan.between(9 * H, 9 * H).duration, DAY)   # same time twice = a day
        self.assertEqual(RunPlan().describe(), "steady traffic")
        self.assertEqual(plan.describe(), "chosen hours, 6:00 to 20:00")
        self.assertIsNone(RunPlan().stop)
        with self.assertRaises(ValueError):
            RunPlan("custom", DAY, 60)
        with self.assertRaises(ValueError):
            RunPlan("custom", 0, 0)


class TestRushHour(unittest.TestCase):
    def test_ramps_up_to_the_peak_and_back(self):
        rush = morning()
        self.assertEqual(rush.intensity(6 * H), 0)
        self.assertAlmostEqual(rush.intensity(7.75 * H), 0.5)   # halfway up the ramp
        self.assertAlmostEqual(rush.intensity(8.5 * H), 1.0)
        self.assertEqual(rush.intensity(10 * H), 0)
        ramp = [rush.intensity(7 * H + i * 60) for i in range(91)]
        self.assertEqual(ramp, sorted(ramp))

    def test_departure_times(self):
        rush = morning(peak=8 * H)      # lopsided: 1 hour up, 2 hours down
        self.assertEqual((rush.cdf(6 * H), rush.cdf(11 * H)), (0, 1))
        self.assertAlmostEqual(rush.cdf(8 * H), 1 / 3)
        self.assertAlmostEqual(sum(rush.density(7 * H + i) for i in range(0, 3 * H, 10)) * 10, 1, places=3)
        rng = random.Random(3)
        times = [rush.sample(rng) for _ in range(4000)]
        self.assertTrue(all(7 * H <= t <= 10 * H for t in times))
        self.assertAlmostEqual(sum(t <= 8 * H for t in times) / len(times), 1 / 3, delta=0.025)

    def test_rejects_bad_values(self):
        for kw in ({"peak": 6 * H}, {"end": 8 * H}, {"share": 1.5}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                morning(**kw)

    def test_daily_activity_averages_one(self):
        values = [daily_activity(m * 60) for m in range(1440)]
        self.assertAlmostEqual(sum(values) / len(values), 1, delta=0.01)
        self.assertLess(daily_activity(3.5 * H), 0.1)      # the middle of the night is quiet
        self.assertGreater(daily_activity(18.5 * H), 1.5)  # early evening is busiest


class TestResidentModel(unittest.TestCase):
    def test_expected_trips_per_day(self):
        # Outings make 2 trips each; commuters leave once and come home once.
        s = Settings()
        traffic = make_simulation(s)[1]
        per_day = sum(traffic.expected(m * 60)["total"] for m in range(1440))
        commuting = s.morning_share / 100     # leave once, come home once (some of them later)
        self.assertAlmostEqual(per_day / 1000, 2 * 1.5 + 2 * commuting, delta=0.1)
        steady = traffic.expected(None)
        self.assertAlmostEqual(steady["total"], 2 * 1.5 * 1000 / 1440, places=6)
        self.assertEqual(steady["leaving"], 0)

    def test_residents_spread_over_home_floors(self):
        traffic = make_simulation(Settings())[1]
        per_floor = Counter(r.home for r in traffic.residents)
        self.assertEqual(set(per_floor), set(traffic.home_floors))
        self.assertLessEqual(max(per_floor.values()) - min(per_floor.values()), 1)

    def test_every_trip_is_followed_by_the_way_back(self):
        settings = Settings.from_dict({"outings": 6, "warmup": 0})
        sim, traffic = run_plan(settings, RunPlan.between(parse_time("06:30"), parse_time("12:30")))
        trips = defaultdict(list)
        for p in sorted(sim.people.values(), key=lambda p: p.spawn_time):
            trips[p.resident_id].append(p)
        self.assertNotIn(None, trips)
        for rid, legs in trips.items():
            home = traffic.residents[rid].home
            for a, b in zip(legs, legs[1:]):
                self.assertIsNotNone(a.arrive_time, f"resident {rid} set off before arriving")
                self.assertEqual(b.origin_floor, a.requested_floor, f"resident {rid} teleported")
                self.assertTrue((a.origin_floor == home) != (b.origin_floor == home) or
                                a.purpose == "commute_in")
            if legs[0].purpose != "commute_in":        # everyone else starts the run at home
                self.assertEqual(legs[0].origin_floor, home)
        census = traffic.census()
        self.assertEqual(census.home + census.traveling + census.out + census.inside, 1000)

    def test_commuters_return_through_the_exit_they_used(self):
        sim, traffic = run_plan(Settings.from_dict({"warmup": 0}), RunPlan.between(6 * H, 21 * H))
        left = {p.resident_id: p.requested_floor for p in sim.people.values() if p.purpose == "commute_out"}
        back = {p.resident_id: p.origin_floor for p in sim.people.values() if p.purpose == "commute_in"}
        self.assertGreater(len(left), 450)
        self.assertTrue(set(left.values()) <= set(traffic.exits))
        both = set(left) & set(back)
        self.assertGreater(len(both), 450)
        self.assertTrue(all(left[r] == back[r] for r in both))

    def test_deterministic(self):
        plan = RunPlan.between(parse_time("08:00"), parse_time("08:30"))
        a = run_plan(Settings(), plan)[0].report()["passengers"]
        b = run_plan(Settings(), plan)[0].report()["passengers"]
        self.assertEqual(a, b)

    def test_rush_hours_need_an_exit(self):
        with self.assertRaises(ValueError):
            ResidentTraffic(10, [0, 1, 2], {}, rush_hours=[morning()])


class TestRushRuns(unittest.TestCase):
    """The default building (1,000 residents) through each kind of run."""

    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_dict({"warmup": 0})
        cls.morning, cls.morning_traffic = run_plan(cls.settings, cls.settings.rush_plan("morning"))
        cls.evening, cls.evening_traffic = run_plan(cls.settings, cls.settings.rush_plan("evening"))

    def test_morning_rush_heads_out(self):
        count = purposes(self.morning)
        self.assertAlmostEqual(count["commute_out"], 10 * self.settings.morning_share, delta=60)
        self.assertEqual(count["commute_in"], 0)
        self.assertLess(len(self.morning.people), 1400)        # about 1,000 trips, not thousands
        times = sorted(p.spawn_time for p in self.morning.people.values() if p.purpose == "commute_out")
        median = parse_time("06:30") + times[len(times) // 2]
        self.assertAlmostEqual(median, parse_time("08:30"), delta=15 * 60)
        self.assertGreater(self.morning_traffic.census().out, 450)

    def test_evening_rush_comes_home(self):
        # When the run begins at 15:30, the commuters are already out.
        count = purposes(self.evening)
        self.assertAlmostEqual(count["commute_in"], 10 * self.settings.evening_share, delta=60)
        self.assertEqual(count["commute_out"], 0)
        # They ride from the exit to their home floor: up, except from the garage (floor 4)
        # to floors 2 and 3.
        for p in self.evening.people.values():
            if p.purpose == "commute_in":
                self.assertIn(p.origin_floor, self.evening_traffic.exits)
                self.assertEqual(p.requested_floor, self.evening_traffic.residents[p.resident_id].home)

    def test_waits_are_realistic(self):
        for sim in (self.morning, self.evening):
            wait = sim.report()["passengers"]["wait"]
            self.assertLess(wait["mean"], 40)
            self.assertLess(wait["p95"], 120)

    def test_steady_run_has_no_commutes(self):
        sim, traffic = run_plan(self.settings, RunPlan("steady", None, H))
        count = purposes(sim)
        self.assertEqual(count["commute_out"] + count["commute_in"], 0)
        self.assertAlmostEqual(count["outing"] / 60, 1.04, delta=0.3)     # 1,000 x 1.5 / 1,440 a minute

    def test_switched_off_morning_rush(self):
        settings = Settings.from_dict({"warmup": 0, "morning_enabled": False})
        sim, _ = run_plan(settings, RunPlan.between(parse_time("07:00"), parse_time("10:00")))
        self.assertEqual(purposes(sim)["commute_out"], 0)
        # The evening rush still brings people home: they count as already out when the run starts.
        traffic = make_simulation(settings, settings.rush_plan("evening"))
        traffic[0].step()
        self.assertGreater(traffic[1].census().out, 400)

    def test_switched_off_evening_rush_spreads_returns(self):
        settings = Settings.from_dict({"evening_enabled": False})
        traffic = make_simulation(settings)[1]
        self.assertEqual(traffic.late_window, (12 * H, 23.5 * H))
        coming_home = lambda h: (lambda e: e["total"] * e["returning"])(traffic.expected(h * H))  # noqa: E731
        self.assertAlmostEqual(coming_home(17.5), coming_home(14))        # no peak: an even spread
        self.assertAlmostEqual(coming_home(14), 10 * settings.morning_share / (11.5 * 60), places=6)
        self.assertEqual(coming_home(11), 0)


class TestRushSettings(unittest.TestCase):
    def test_defaults(self):
        s = Settings()
        self.assertEqual((s.residents, s.outings), (1000, 1.5))
        self.assertEqual((s.morning_start, s.morning_end, s.morning_share), ("07:00", "10:00", 65))
        self.assertEqual((s.evening_start, s.evening_end, s.evening_share), ("16:00", "19:00", 55))
        self.assertEqual([r.name for r in s.rush_hours()], ["morning", "evening"])
        self.assertTrue(s.rush_hour("morning").leaving)
        self.assertAlmostEqual(s.rush_hour("evening").share, 0.55)
        self.assertTrue(s.random_seed)

    def test_values_from_the_form(self):
        s = Settings.from_dict({"morning_start": "6:45", "morning_enabled": "false", "evening_enabled": True})
        self.assertEqual(s.morning_start, "06:45")
        self.assertFalse(s.morning_enabled)
        self.assertEqual([r.name for r in s.rush_hours()], ["evening"])

    def test_errors_name_the_field_and_the_rush(self):
        cases = [
            ({"morning_peak": "11:00"}, "morning_peak", "morning rush has to peak"),
            ({"evening_start": "19:30"}, "evening_end", "evening rush has to end after"),
            ({"morning_end": "25:00"}, "morning_end", "Morning rush end time"),
            ({"evening_share": 101}, "evening_share", "Evening rush share of residents"),
            ({"residents": 0}, "residents", "Residents"),
            ({"outings": -1}, "outings", "Outings per resident per day"),
            ({"evening_enabled": "maybe"}, "evening_enabled", "on or off"),
        ]
        for data, field, words in cases:
            with self.subTest(data=data), self.assertRaises(SettingsError) as ctx:
                Settings.from_dict(data)
            self.assertEqual(ctx.exception.field, field)
            self.assertIn(words, str(ctx.exception))

    def test_rush_plans(self):
        s = Settings()
        self.assertEqual(s.rush_plan("morning"), RunPlan("morning", parse_time("06:30"), 4 * H))
        self.assertEqual(s.rush_plan("evening").describe(), "evening rush, 15:30 to 19:30")
        with self.assertRaises(SettingsError) as ctx:
            Settings.from_dict({"evening_enabled": False}).rush_plan("evening")
        self.assertEqual(ctx.exception.field, "evening_enabled")

    def test_schema(self):
        by_key = {f["key"]: f for f in Settings.schema()}
        self.assertEqual(by_key["morning_enabled"]["type"], "bool")
        self.assertEqual(by_key["evening_peak"]["type"], "time")
        self.assertEqual(by_key["morning_share"]["enabled_by"], "morning_enabled")
        self.assertEqual(by_key["residents"]["group"], "Residents")
        self.assertNotIn("rate", by_key)

    def test_old_settings_file_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"version": 1, "floors": 40, "rate": 12, "morning_multiplier": 2}))
            s = SettingsStore(path).load()
        self.assertEqual((s.floors, s.residents, s.morning_share), (40, 1000, 65))

    def test_trip_rows_name_the_resident(self):
        sim, _ = run_plan(Settings(), RunPlan.between(parse_time("08:00"), parse_time("08:05")))
        cols = Simulation.TRIP_COLUMNS
        row = dict(zip(cols, next(sim.trip_rows())))
        self.assertIn(row["purpose"], ("outing", "commute_out"))
        self.assertIsInstance(row["resident"], int)


class TestSessionRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(SettingsStore(Path(self.tmp.name) / "settings.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def frame(self):
        return json.loads(self.session.frame_json)

    def layout(self):
        return json.loads(self.session.layout_json)

    def test_speeds(self):
        self.assertEqual(SPEEDS, (1, 2, 5, 10, 20, 100, 200, "max"))
        self.assertEqual(self.layout()["speeds"], list(SPEEDS))
        self.session.control({"speed": 20})
        self.assertEqual(self.frame()["speed"], 20)
        self.session.control({"speed": "max"})
        self.assertEqual(self.frame()["speed"], "max")
        for bad in (500, 50, "fast"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.session.control({"speed": bad})

    def test_fastest_runs_flat_out_and_stops_at_the_stop_time(self):
        import time as _time
        self.session.control({"speed": "max"})
        self.session.run_flat_out(_time.monotonic() + 0.5)
        self.assertGreater(self.session.sim.time, 50)       # far beyond real time
        self.session.control({"paused": True})
        before = self.session.sim.time
        self.session.run_flat_out(_time.monotonic() + 0.2)
        self.assertEqual(self.session.sim.time, before)     # paused: does nothing
        self.session.start_run({"mode": "custom", "start": "08:00", "stop": "08:02"})
        self.session.run_flat_out(_time.monotonic() + 2)
        self.assertAlmostEqual(self.session.sim.time, 120, delta=0.01)
        self.assertTrue(self.session.finished)

    def test_starts_steady(self):
        layout, frame = self.layout(), self.frame()
        self.assertEqual(layout["run"]["mode"], "steady")
        self.assertIsNone(layout["profile"])
        self.assertIsNone(frame["clock"])
        self.assertEqual(frame["residents"], {"total": 1000, "home": 1000, "traveling": 0, "out": 0, "inside": 0})
        self.assertAlmostEqual(frame["rate_now"], 2.08, delta=0.01)

    def test_outings_control(self):
        self.session.control({"outings": 3})
        self.assertEqual(self.frame()["outings"], 3)
        self.assertAlmostEqual(self.frame()["rate_now"], 4.17, delta=0.01)
        with self.assertRaises(ValueError):
            self.session.control({"outings": 51})

    def test_outings_redraw_the_traffic_curve(self):
        s = self.session
        s.start_run({"mode": "morning"})
        before, before_id = json.loads(s.profile_json), s.profile_id
        self.assertEqual(before, self.layout()["profile"])
        s.control({"outings": 6})
        after = json.loads(s.profile_json)
        self.assertGreater(s.profile_id, before_id)
        self.assertGreater(max(p[1] for p in after), max(p[1] for p in before))
        # The "now" dot sits on the new curve.
        self.assertAlmostEqual(after[0][1], self.frame()["rate_now"], delta=0.01)

    def test_rush_run(self):
        info = self.session.start_run({"mode": "morning"})["run"]
        self.assertEqual((info["start"], info["stop"], info["residents"]), (parse_time("06:30"), parse_time("10:30"), 1000))
        profile = self.layout()["profile"]
        busiest = max(profile, key=lambda p: p[1])
        self.assertAlmostEqual(parse_time("06:30") + busiest[0], parse_time("08:30"), delta=10 * 60)
        self.assertGreater(busiest[1], 6)
        self.assertLess(busiest[1], 12)
        self.assertEqual(self.frame()["clock"], parse_time("06:30"))

    def test_random_seed_on_every_restart(self):
        s = self.session
        s.save_settings({"random_seed": False})
        self.assertEqual((s.sim.seed, self.layout()["run"]["seed"]), (1, 1))
        s.restart()
        self.assertEqual(s.sim.seed, 1)                       # off: the same seed every time
        s.save_settings({"random_seed": True})
        seeds = {s.sim.seed}
        for _ in range(5):
            s.restart()
            seeds.add(s.sim.seed)
            self.assertEqual(self.layout()["run"]["seed"], s.sim.seed)
            self.assertEqual(s.metrics_payload()["run"]["seed"], s.sim.seed)
        self.assertGreater(len(seeds), 1)
        self.assertEqual(s.settings.seed, 1)                  # the saved seed is left alone
        s.start_run({"mode": "morning"})
        self.assertTrue(self.layout()["run"]["random_seed"])
        field = next(f for f in Settings.schema() if f["key"] == "seed")
        self.assertEqual(field["disabled_by"], "random_seed")

    def test_download_names(self):
        s = self.session
        with s.lock:
            s._advance(2 * H + 15 * 60)
        self.assertEqual(s.download_name("trips", "csv"), "lookahead_steady_2h15m_trips.csv")
        s.save_settings({"dispatcher": "nearest"})
        s.start_run({"mode": "morning"})
        with s.lock:
            s._advance(H + 42 * 60)
        self.assertEqual(s.download_name("report", "json"), "nearest_morning-rush_0630-0812_report.json")
        s.start_run({"mode": "custom", "start": "23:30", "stop": "01:00"})
        with s.lock:
            s._advance(45 * 60)
        self.assertEqual(s.download_name("trips", "csv"), "nearest_hours_2330-0015_trips.csv")

    def test_run_stops_at_its_stop_time(self):
        self.session.start_run({"mode": "custom", "start": "08:20", "stop": "08:25"})
        with self.session.lock:
            self.session._advance(600)
        self.assertAlmostEqual(self.session.sim.time, 300, delta=0.01)
        self.assertTrue(self.session.finished and self.session.paused)
        self.session.control({"paused": False})
        self.assertTrue(self.session.paused)              # finished runs stay stopped
        self.session.restart()
        self.assertFalse(self.session.finished or self.session.paused)
        self.assertEqual(self.session.sim.time, 0)

    def test_bad_requests(self):
        for body in ({"mode": "lunch"}, {"mode": "custom", "start": "8", "stop": "9:00"}, {}, []):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.session.start_run(body)
        self.session.save_settings({"evening_enabled": False})
        with self.assertRaises(SettingsError):
            self.session.start_run({"mode": "evening"})

    def test_saving_settings_follows_the_rush(self):
        self.session.start_run({"mode": "morning"})
        self.session.save_settings({"morning_start": "05:00", "morning_peak": "06:00", "morning_end": "07:00"})
        self.assertEqual(self.session.plan.start, parse_time("04:30"))
        self.session.save_settings({"morning_enabled": False})
        self.assertEqual(self.session.plan.mode, "steady")

    def test_frames_during_the_evening_peak(self):
        self.session.start_run({"mode": "evening"})
        with self.session.lock:
            self.session._advance(2 * H)    # 15:30 -> 17:30
            self.session._publish()
        frame = self.frame()
        self.assertGreater(frame["returning"], 0.5)
        self.assertEqual(frame["leaving"], 0)
        self.assertEqual(sum(frame["residents"][k] for k in ("home", "traveling", "out", "inside")), 1000)
        metrics = self.session.metrics_payload()
        self.assertEqual((metrics["run"]["mode"], metrics["run"]["day_start"]), ("evening", parse_time("15:30")))
        self.assertEqual(metrics["residents"]["total"], 1000)


class TestRunEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        session = Session(SettingsStore(Path(self.tmp.name) / "settings.json"))
        web = Path(__file__).resolve().parents[1] / "web"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, web))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(f"{self.base}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.load(err)

    def test_run_modes(self):
        status, data = self.post("/api/run", {"mode": "custom", "start": "06:00", "stop": "20:00"})
        self.assertEqual((status, data["run"]["duration"]), (200, 14 * H))
        self.assertEqual(self.post("/api/run", {"mode": "steady"})[1]["run"]["start"], None)
        status, data = self.post("/api/run", {"mode": "custom", "start": "6am", "stop": "20:00"})
        self.assertEqual(status, 400)
        self.assertIn("times of day", data["error"])

    def test_switched_off_rush(self):
        self.post("/api/settings", {"morning_enabled": False})
        status, data = self.post("/api/run", {"mode": "morning"})
        self.assertEqual((status, data["field"]), (400, "morning_enabled"))

    def test_settings_payload_has_group_help(self):
        with urllib.request.urlopen(f"{self.base}/api/settings") as res:
            data = json.load(res)
        self.assertIn("Residents", data["group_help"])


class TestReportCli(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                report = report_cli.main([*args, "--settings", str(Path(tmp) / "none.json")])
        return report, out.getvalue()

    def test_hours_run_with_timeline(self):
        report, text = self.run_cli("--start", "08:20", "--stop", "08:40", "--warmup", "0", "--timeline")
        self.assertEqual(report["run"]["mode"], "custom")
        self.assertAlmostEqual(report["time"], 1200, delta=0.1)
        self.assertIn("Run: chosen hours, 8:20 to 8:40, 1,000 residents", text)
        self.assertIn("Measured 8:20 to 8:40", text)
        self.assertIn("\n  8:25 ", text)

    def test_overrides(self):
        report, _ = self.run_cli("--start", "08:00", "--minutes", "5", "--warmup", "60", "--quiet",
                                 "--residents", "400", "--outings", "3")
        self.assertAlmostEqual(report["time"], 360, delta=0.1)
        self.assertEqual((report["settings"]["residents"], report["settings"]["outings"]), (400, 3))

    def test_argument_errors(self):
        for args in (["--rush", "morning", "--start", "07:00"], ["--stop", "09:00"],
                     ["--rush", "evening", "--minutes", "10"], ["--start", "7am"], ["--rate", "20"]):
            with self.subTest(args=args), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                report_cli.main(args)


if __name__ == "__main__":
    unittest.main()
