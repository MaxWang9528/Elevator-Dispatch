import csv
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from elevator_sim import (
    Arrived, Building, Elevator, InCar, MetricsCollector, Settings, SettingsStore, Simulation,
    distribution, make_simulation, percentile, percentiles,
)
from elevator_sim import report as report_cli
from elevator_sim.server import Session, make_handler


def make_sim(floors=10, cars=1, capacity=16, **metrics_kw):
    elevators = [Elevator(id=i, capacity=capacity) for i in range(cars)]
    return Simulation(Building.simple(floors), elevators, metrics=MetricsCollector(**metrics_kw))


def run_until(sim, cond, max_seconds=900):
    for _ in range(round(max_seconds / sim.dt)):
        if cond():
            return
        sim.step()
    raise AssertionError(f"condition not met within {max_seconds}s; summary={sim.summary()}")


def all_arrived(sim):
    return lambda: all(isinstance(p.location, Arrived) for p in sim.people.values())


class TestStatistics(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(percentile([7], 99), 7)
        self.assertEqual(percentile([0, 10], 0), 0)
        self.assertEqual(percentile([0, 10], 100), 10)
        with self.assertRaises(ValueError):
            percentile([], 50)

    def test_percentiles(self):
        values = percentiles([5, 1, 3])
        self.assertEqual(len(values), 101)
        self.assertEqual((values[0], values[50], values[100]), (1, 3, 5))
        self.assertEqual(values, sorted(values))
        self.assertEqual(percentiles([]), [])

    def test_distribution(self):
        d = distribution(range(1, 101))
        self.assertEqual((d["n"], d["mean"], d["p50"], d["max"]), (100, 50.5, 50.5, 100))
        self.assertAlmostEqual(d["p95"], 95.0, delta=0.1)
        self.assertEqual(distribution([]), {"n": 0, "mean": None, "p50": None, "p90": None,
                                            "p95": None, "p99": None, "max": None})


class TestOneTrip(unittest.TestCase):
    """One person, 5 -> 0, with one car starting at 0: every number can be checked by hand."""

    def setUp(self):
        self.sim = make_sim()
        self.p = self.sim.spawn_person(origin=5, destination=0)
        run_until(self.sim, all_arrived(self.sim))
        self.sim.run(5)   # let the doors close
        self.report = self.sim.report()

    def test_passenger_times_match_the_person(self):
        p, r = self.p, self.report["passengers"]
        self.assertEqual((r["measured"], r["delivered"], r["waiting"], r["riding"]), (1, 1, 0, 0))
        self.assertAlmostEqual(r["wait"]["mean"], p.wait_time, delta=0.05)
        self.assertAlmostEqual(r["ride"]["mean"], p.ride_time, delta=0.05)
        self.assertAlmostEqual(r["trip"]["mean"], p.wait_time + p.ride_time, delta=0.1)
        self.assertEqual(p.trip_time, p.wait_time + p.ride_time)
        self.assertEqual(p.car_id, 0)
        self.assertEqual(r["stops_per_ride"], 0)
        self.assertEqual((r["straight_shots"], r["straight_shot_share"]), (1, 1.0))

    def test_hall_call_response(self):
        calls = self.report["hall_calls"]
        self.assertEqual((calls["answered"], calls["open"], calls["oldest_open"]), (1, 0, None))
        # The person boards the moment the doors finish opening.
        self.assertAlmostEqual(calls["response"]["mean"], self.p.wait_time, delta=0.2)

    def test_car_figures(self):
        car = self.report["cars"][0]
        self.assertAlmostEqual(car["distance"], 10.0, delta=0.05)        # 5 up empty, 5 down loaded
        self.assertAlmostEqual(car["empty_distance"], 5.0, delta=0.05)
        self.assertAlmostEqual(car["empty_share"], 0.5, delta=0.01)
        self.assertAlmostEqual(car["avg_load_moving"], 0.5, delta=0.01)
        self.assertEqual((car["starts"], car["door_cycles"]), (2, 2))
        self.assertEqual((car["boarded"], car["delivered"], car["peak_load"]), (1, 1, 1))
        self.assertAlmostEqual(car["share_moving"] + car["share_doors"] + car["share_idle"], 1.0, delta=0.002)
        self.assertEqual(self.report["fleet"]["distance"], car["distance"])
        self.assertAlmostEqual(self.report["fleet"]["floors_per_delivery"], 10.0, delta=0.1)


class TestPeopleStillWaitingCount(unittest.TestCase):
    def test_wait_so_far_is_included(self):
        sim = make_sim(floors=40, long_wait=5)
        sim.spawn_person(origin=39, destination=0)
        sim.run(10)   # the car can't get to floor 39 in 10 seconds
        r = sim.report()["passengers"]
        self.assertEqual((r["waiting"], r["delivered"]), (1, 0))
        self.assertAlmostEqual(r["wait"]["mean"], 10.0, delta=0.15)
        self.assertEqual(r["long_wait_share"], 1.0)
        self.assertEqual(r["ride"]["n"], 0)
        live = sim.summary()
        self.assertAlmostEqual(live["mean_wait"], 10.0, delta=0.15)
        self.assertAlmostEqual(live["max_wait"], 10.0, delta=0.15)
        self.assertIsNone(live["mean_ride"])


class TestWarmup(unittest.TestCase):
    def test_people_and_travel_before_warmup_are_excluded(self):
        sim = make_sim(warmup=60)
        early = sim.spawn_person(origin=3, destination=0)
        self.assertTrue(sim.summary()["warming_up"])
        sim.run(60)
        self.assertIsInstance(early.location, Arrived)
        self.assertFalse(sim.summary()["warming_up"])
        late = sim.spawn_person(origin=0, destination=4)
        run_until(sim, all_arrived(sim))
        sim.run(5)

        r = sim.report()
        self.assertEqual(r["passengers"]["measured"], 1)
        self.assertAlmostEqual(r["passengers"]["wait"]["mean"], late.wait_time, delta=0.05)
        self.assertAlmostEqual(r["cars"][0]["distance"], 4.0, delta=0.05)   # only the late trip
        self.assertEqual(r["cars"][0]["delivered"], 1)
        self.assertAlmostEqual(r["measured_seconds"], r["time"] - 60, delta=0.2)
        self.assertEqual(sum(f["trips_from"] for f in r["by_floor"]), 1)
        live = sim.summary()
        self.assertEqual((live["spawned"], live["arrived"]), (2, 2))        # counts are everyone
        self.assertAlmostEqual(live["mean_wait"], late.wait_time, delta=0.05)
        rows = {row[0]: row for row in sim.trip_rows()}
        self.assertEqual((rows[early.id][5], rows[late.id][5]), (0, 1))     # "measured" column

    def test_nothing_measured_yet(self):
        sim = make_sim(warmup=600)
        sim.spawn_person(origin=0, destination=5)
        sim.run(30)
        r = sim.report()
        self.assertTrue(r["warming_up"])
        self.assertEqual(r["passengers"]["measured"], 0)
        self.assertIsNone(r["passengers"]["wait"]["mean"])
        self.assertEqual((r["timeline"], r["measured_seconds"]), ([], 0))
        self.assertIsNone(sim.summary()["mean_wait"])
        report_cli.format_report(r)   # must not choke on empty values


class TestCrowding(unittest.TestCase):
    def test_left_behind_by_a_full_car(self):
        sim = make_sim(capacity=3)
        crowd = [sim.spawn_person(origin=0, destination=9) for _ in range(5)]
        run_until(sim, all_arrived(sim))
        sim.run(5)
        r = sim.report()
        self.assertEqual(sorted(p.left_behind for p in crowd), [0, 0, 0, 1, 1])
        self.assertEqual((r["passengers"]["left_behind_people"], r["passengers"]["left_behind_events"]), (2, 2))
        car = r["cars"][0]
        self.assertEqual((car["peak_load"], car["full_departures"]), (3, 1))

    def test_stops_en_route(self):
        sim = make_sim()
        far = sim.spawn_person(origin=0, destination=9)
        near = sim.spawn_person(origin=0, destination=5)
        run_until(sim, all_arrived(sim))
        self.assertEqual((far.stops_en_route, near.stops_en_route), (1, 0))
        p = sim.report()["passengers"]
        self.assertEqual(p["stops_per_ride"], 0.5)
        # Only the rider to 5 went straight there; the one to 9 stopped at 5 on the way.
        self.assertEqual((p["straight_shots"], p["straight_shot_share"]), (1, 0.5))
        self.assertEqual(sim.report()["by_direction"]["up"]["straight_shot_share"], 0.5)


class TestUnderTraffic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings.from_dict({"outings": 40, "warmup": 0, "seed": 5})
        cls.sim, _, _ = make_simulation(cls.settings)
        cls.sim.run(20 * 60)
        cls.report = cls.sim.report()

    def test_everyone_is_counted_exactly_once(self):
        r, p = self.report, self.report["passengers"]
        live = self.sim.summary()
        self.assertGreater(p["measured"], 400)
        self.assertEqual(p["measured"], p["waiting"] + p["riding"] + p["delivered"])
        self.assertEqual(p["measured"], live["spawned"])
        self.assertEqual((p["waiting"], p["riding"], p["delivered"]),
                         (live["waiting"], live["riding"], live["arrived"]))
        self.assertEqual(p["riding"], sum(isinstance(q.location, InCar) for q in self.sim.people.values()))
        self.assertEqual(p["wait"]["n"], p["measured"])
        self.assertEqual(p["ride"]["n"], p["delivered"])
        self.assertEqual(sum(b["arrived"] for b in r["timeline"]), p["measured"])
        self.assertEqual(sum(b["delivered"] for b in r["timeline"]), p["delivered"])
        self.assertEqual(sum(f["trips_from"] for f in r["by_floor"]), p["measured"])
        self.assertEqual(sum(f["trips_to"] for f in r["by_floor"]), p["measured"])
        self.assertEqual(sum(d["people"] for d in r["by_direction"].values()), p["measured"])
        self.assertEqual(sum(c["boarded"] for c in r["cars"]), p["riding"] + p["delivered"])
        self.assertEqual(sum(c["delivered"] for c in r["cars"]), p["delivered"])

    def test_live_numbers_agree_with_the_report(self):
        live, p = self.sim.summary(), self.report["passengers"]
        self.assertAlmostEqual(live["mean_wait"], p["wait"]["mean"], delta=0.1)
        self.assertAlmostEqual(live["max_wait"], p["wait"]["max"], delta=0.1)
        self.assertAlmostEqual(live["mean_ride"], p["ride"]["mean"], delta=0.1)

    def test_distributions_are_ordered(self):
        for key in ("wait", "ride", "trip"):
            d = self.report["passengers"][key]
            self.assertLessEqual(d["p50"], d["p90"])
            self.assertLessEqual(d["p90"], d["p95"])
            self.assertLessEqual(d["p95"], d["p99"])
            self.assertLessEqual(d["p99"], d["max"])
        p = self.report["passengers"]
        steps = p["trip_percentiles"]
        self.assertEqual(len(steps), 101)
        self.assertEqual(steps, sorted(steps))
        self.assertEqual((steps[50], steps[95], steps[100]), (p["trip"]["p50"], p["trip"]["p95"], p["trip"]["max"]))
        share = self.report["passengers"]["long_wait_share"]
        self.assertTrue(0 <= share <= 1)

    def test_cars_and_fleet(self):
        r = self.report
        for car in r["cars"]:
            self.assertAlmostEqual(car["share_moving"] + car["share_doors"] + car["share_idle"], 1.0, delta=0.002)
            self.assertLessEqual(car["empty_distance"], car["distance"])
            self.assertLessEqual(car["peak_load"], car["capacity"])
            self.assertLessEqual(abs(car["starts"] - car["door_cycles"]), car["door_cycles"])
        self.assertAlmostEqual(r["fleet"]["distance"], sum(c["distance"] for c in r["cars"]), delta=0.5)
        self.assertEqual(r["fleet"]["starts"], sum(c["starts"] for c in r["cars"]))
        self.assertEqual(r["fleet"]["peak_load"], max(c["peak_load"] for c in r["cars"]))

    def test_timeline_and_system(self):
        r = self.report
        self.assertEqual(len(r["timeline"]), 4)                # 20 minutes in 5-minute intervals
        self.assertEqual([b["t0"] for b in r["timeline"]], [0, 300, 600, 900])
        self.assertTrue(all(b["complete"] for b in r["timeline"][:-1]))
        self.assertAlmostEqual(r["measured_seconds"], 1200, delta=0.2)
        self.assertEqual(r["system"]["peak_waiting"], max(b["peak_waiting"] for b in r["timeline"]))
        self.assertAlmostEqual(r["system"]["arrivals_per_hour"], r["passengers"]["measured"] * 3, delta=0.5)
        self.assertGreater(r["hall_calls"]["answered"], 100)

    def test_report_is_json_and_text_ready(self):
        json.dumps(self.report)
        text = report_cli.format_report(self.report)
        self.assertIn("Wait", text)
        self.assertIn("Car 6", text)

    def test_same_seed_same_report(self):
        again, _, _ = make_simulation(self.settings)
        again.run(20 * 60)
        self.assertEqual(again.report(), self.report)
        other, _, _ = make_simulation(Settings.from_dict({"outings": 40, "warmup": 0, "seed": 6}))
        other.run(20 * 60)
        self.assertNotEqual(other.report()["passengers"], self.report["passengers"])

    def test_trip_rows(self):
        rows = list(self.sim.trip_rows())
        self.assertEqual(len(rows), len(self.sim.people))
        self.assertTrue(all(len(row) == len(self.sim.TRIP_COLUMNS) for row in rows))
        self.assertEqual({row[4] for row in rows} - {"waiting", "riding", "delivered"}, set())
        delivered = next(row for row in rows if row[4] == "delivered")
        cols = dict(zip(self.sim.TRIP_COLUMNS, delivered))
        self.assertAlmostEqual(cols["trip"], cols["wait"] + cols["ride"], delta=0.02)


class TestTimelineSettling(unittest.TestCase):
    def test_interval_waits_for_its_people_to_board(self):
        sim = Simulation(Building.simple(10), [], metrics=MetricsCollector())   # no cars: nobody boards
        sim.spawn_person(origin=0, destination=5)
        sim.run(200)
        first = sim.report()["timeline"][0]
        self.assertEqual((first["complete"], first["settled"], first["still_waiting"]), (False, False, 1))
        sim.run(150)   # 0:05:50 - the interval has ended, but its one arrival is still waiting
        first = sim.report()["timeline"][0]
        self.assertEqual((first["complete"], first["settled"]), (True, False))
        sim.run(300)   # a whole further interval later it is shown anyway
        first = sim.report()["timeline"][0]
        self.assertEqual((first["complete"], first["settled"]), (True, True))
        self.assertAlmostEqual(first["wait_mean"], 650, delta=0.5)

    def test_settled_once_everyone_has_boarded(self):
        sim = make_sim()
        sim.spawn_person(origin=0, destination=5)
        sim.run(301)
        first = sim.report()["timeline"][0]
        self.assertEqual((first["complete"], first["settled"], first["still_waiting"]), (True, True, 0))


class TestOverload(unittest.TestCase):
    def test_flag_when_the_cars_cannot_keep_up(self):
        busy, _, _ = make_simulation(Settings.from_dict({"outings": 40, "elevators": 2, "warmup": 0}))
        busy.run(20 * 60)
        system = busy.report()["system"]
        self.assertTrue(system["overloaded"])
        self.assertGreater(system["backlog_change"], 100)
        self.assertLess(system["delivered_per_hour"], system["arrivals_per_hour"])

        calm, _, _ = make_simulation(Settings.from_dict({"outings": 3.5, "warmup": 0}))
        calm.run(20 * 60)
        self.assertFalse(calm.report()["system"]["overloaded"])


class TestSettingsAndCli(unittest.TestCase):
    def test_metrics_settings_reach_the_collector(self):
        self.assertIn("Metrics", {f["group"] for f in Settings.schema()})
        sim, _, _ = make_simulation(Settings.from_dict({"warmup": 120, "long_wait": 45, "bucket_minutes": 2}))
        m = sim.metrics
        self.assertEqual((m.warmup, m.long_wait, m.bucket_seconds), (120, 45, 120))
        with self.assertRaises(ValueError):
            MetricsCollector(bucket_seconds=0)

    def test_cli_writes_report_and_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, trips = Path(tmp) / "run.json", Path(tmp) / "trips.csv"
            report = report_cli.main(["--minutes", "3", "--warmup", "60", "--outings", "20", "--seed", "9",
                                      "--quiet", "--settings", str(Path(tmp) / "none.json"),
                                      "--json", str(out), "--trips", str(trips)])
            saved = json.loads(out.read_text())
            self.assertEqual(saved["passengers"], report["passengers"])
            self.assertEqual((saved["settings"]["seed"], saved["run"]["seed"], saved["warmup"]), (9, 9, 60))
            self.assertAlmostEqual(saved["measured_seconds"], 180, delta=0.2)
            with trips.open(newline="") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(tuple(rows[0]), Simulation.TRIP_COLUMNS)
            self.assertGreater(len(rows), saved["passengers"]["measured"])   # also pre-warm-up people


class TestServerEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        session = Session(SettingsStore(Path(self.tmp.name) / "settings.json"))
        session.spawn({"origin": 0, "destination": 7})
        web = Path(__file__).resolve().parents[1] / "web"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(session, web))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_metrics_json(self):
        with urllib.request.urlopen(f"{self.base}/api/metrics") as res:
            self.assertIsNone(res.headers["Content-Disposition"])
            data = json.load(res)
        self.assertEqual(data["settings"]["floors"], 38)
        self.assertIn("timeline", data)
        self.assertTrue(data["warming_up"])
        with urllib.request.urlopen(f"{self.base}/api/metrics?download=1") as res:
            self.assertIn('filename="lookahead_steady_0h00m_report.json"', res.headers["Content-Disposition"])

    def test_trips_csv(self):
        with urllib.request.urlopen(f"{self.base}/api/trips.csv") as res:
            self.assertIn('attachment; filename="lookahead_steady_0h00m_trips.csv"', res.headers["Content-Disposition"])
            lines = res.read().decode().splitlines()
        self.assertEqual(lines[0].split(","), list(Simulation.TRIP_COLUMNS))
        self.assertEqual(lines[1].split(",")[:5], ["0", "1", "8", "up", "waiting"])


if __name__ == "__main__":
    unittest.main()
