import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from elevator_sim import RunPlan, Settings, SettingsStore, Simulation, make_simulation, parse_time
from elevator_sim.imports import ImportFileError, report_from_file
from elevator_sim.report import write_trips
from elevator_sim.server import Session, make_handler

NOT_IN_TRIPS = {"fleet", "cars", "hall_calls", "run", "source", "by_floor"}


def finished_run(plan=None, **settings):
    plan = plan or RunPlan.between(parse_time("08:00"), parse_time("09:00"))
    sim, _, _ = make_simulation(Settings.from_dict(settings), plan)
    sim.run(plan.duration)
    return sim


def trips_bytes(sim):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "trips.csv"
        write_trips(sim, path)
        return path.read_bytes()


def without_floor_names(by_floor):
    return [{k: v for k, v in f.items() if k not in ("name", "kind")} for f in by_floor]


class TestTripsReproduceTheReport(unittest.TestCase):
    def check(self, sim):
        live = sim.report()
        imported = report_from_file("trips.csv", trips_bytes(sim))
        for key in live:
            if key not in NOT_IN_TRIPS:
                with self.subTest(key=key):
                    self.assertEqual(imported[key], live[key])
        self.assertEqual(without_floor_names(imported["by_floor"]), without_floor_names(live["by_floor"]))
        return live, imported

    def test_timed_run(self):
        live, imported = self.check(finished_run(dispatcher="nearest", seed=4))
        run = imported["run"]
        self.assertEqual((run["dispatcher"], run["dispatcher_label"], run["seed"]), ("nearest", "Nearest car", 4))
        self.assertEqual((run["day_start"], run["description"]), (parse_time("08:00"), "8:00 to 9:00"))
        self.assertEqual(imported["source"]["kind"], "trips")
        self.assertEqual(imported["source"]["missing"], ["cars", "hall_calls"])
        self.assertIsNone(imported["fleet"]["floors_per_delivery"])          # unknown, not zero
        self.assertIsNone(imported["hall_calls"]["answered"])

    def test_steady_run_with_people_still_waiting(self):
        sim = finished_run(RunPlan("steady", None, 1800), outings=30)
        live, imported = self.check(sim)
        self.assertIsNone(imported["run"]["day_start"])

    def test_report_json_comes_back_as_is(self):
        sim = finished_run()
        live = sim.report()
        imported = report_from_file("r.json", json.dumps(live).encode())
        self.assertEqual({k: v for k, v in imported.items() if k != "source"}, live)
        self.assertEqual(imported["source"], {"name": "r.json", "kind": "report", "missing": [], "note": ""})


class TestOlderTripsFiles(unittest.TestCase):
    def test_files_without_the_run_columns_still_import(self):
        sim = finished_run(warmup=300)
        lines = trips_bytes(sim).decode().splitlines()
        keep = Simulation.TRIP_COLUMNS.index("left_behind") + 1
        old = "\n".join(",".join(line.split(",")[:keep]) for line in lines).encode()
        report = report_from_file("lookahead_hours_0800-0900_trips.csv", old)
        self.assertEqual(report["run"]["dispatcher"], "lookahead")          # read from the file name
        self.assertIsNone(report["run"]["day_start"])
        self.assertEqual(report["warmup"], 300)                             # worked out from `measured`
        self.assertEqual(report["passengers"]["measured"], sim.report()["passengers"]["measured"])


class TestBadFiles(unittest.TestCase):
    def test_messages_say_what_is_wrong(self):
        header = b"from_floor,to_floor,spawn_time,board_time,arrive_time\n"
        cases = [
            ("empty.csv", b"", "is empty"),
            ("other.csv", b"name,age\nann,3\n", "doesn't look like a trips file"),
            ("header.csv", header, "no trips"),
            ("broken.json", b"{not json", "isn't valid JSON"),
            ("other.json", b'{"hello": 1}', "doesn't look like a report"),
            ("image.png", b"\x89PNG\r\n\x1a\n\xff\xfe", "isn't a text file"),
            ("bad.csv", header + b"1,5,soon,,\n", "line 2: spawn_time must be a number"),
            ("floor.csv", header + b"0,5,1,,\n", "line 2: from_floor must be a floor number"),
            ("same.csv", header + b"3,3,1,,\n", "the same"),
        ]
        for name, data, words in cases:
            with self.subTest(name=name), self.assertRaises(ImportFileError) as ctx:
                report_from_file(name, data)
            self.assertIn(words, str(ctx.exception))


class TestImportEndpoint(unittest.TestCase):
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

    def post(self, name, data):
        req = urllib.request.Request(f"{self.base}/api/import?name={urllib.request.quote(name)}", data)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.load(err)

    def test_trips_and_reports(self):
        sim = finished_run(RunPlan.between(parse_time("08:00"), parse_time("08:30")))
        status, data = self.post("day nearest.csv", trips_bytes(sim))
        self.assertEqual((status, data["report"]["source"]["kind"]), (200, "trips"))
        with urllib.request.urlopen(f"{self.base}/api/metrics") as res:
            current = res.read()
        status, data = self.post("now.json", current)
        self.assertEqual((status, data["report"]["source"]["kind"]), (200, "report"))
        self.assertEqual(data["report"]["run"]["dispatcher_label"], "Lookahead")

    def test_bad_file(self):
        status, data = self.post("notes.txt", b"hello")
        self.assertEqual(status, 400)
        self.assertIn("notes.txt", data["error"])

    def test_page_has_the_tab(self):
        with urllib.request.urlopen(f"{self.base}/") as res:
            page = res.read().decode()
        for needle in ('id="tab-import"', 'id="view-import"', 'id="metricsLayout"', 'src="imports.js"',
                       'href="?sample=lookahead#import"', 'href="?sample=nearest#import"',
                       'href="?sample=scan#import"', 'href="?sample=office-lookahead#import"',
                       'href="?sample=office-nearest#import"', 'href="?sample=wtc-lookahead#import"',
                       'href="?sample=wtc-nearest#import"'):
            self.assertIn(needle, page)

    def test_sample_data(self):
        # Every sample the Import tab offers is served, is a valid report, and is the right algorithm.
        script = (Path(__file__).resolve().parents[1] / "web" / "imports.js").read_text()
        samples = [(a, a, 10, 38) for a in ("lookahead", "nearest", "scan")]
        samples += [(f"office-{a}", a, 10, 50) for a in ("lookahead", "nearest")]
        samples += [(f"wtc-{a}", a, None, 104) for a in ("lookahead", "nearest")]     # single runs
        for sample, algorithm, runs, floors in samples:
            with self.subTest(sample=sample):
                self.assertIn(f'id: "{sample}"', script)
                with urllib.request.urlopen(f"{self.base}/samples/{sample}.json") as res:
                    self.assertEqual(res.headers.get_content_type(), "application/json")
                    report = json.load(res)
                report = report_from_file(f"{sample}.json", json.dumps(report).encode())
                self.assertEqual(report["run"]["dispatcher"], algorithm)
                self.assertEqual(report["run"]["floors"], floors)
                self.assertEqual(report.get("averaged", {}).get("runs"), runs)
                self.assertEqual(len(report["passengers"]["trip_percentiles"]), 101)
        with urllib.request.urlopen(f"{self.base}/?sample=scan") as res:      # the page a sample tab opens
            self.assertIn('id="view-import"', res.read().decode())


if __name__ == "__main__":
    unittest.main()
