import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from elevator_sim import (
    Arrived, FloorKind, Settings, SettingsError, SettingsStore, make_simulation,
)


class TestSettingsValidation(unittest.TestCase):
    def test_defaults_describe_marquee(self):
        s = Settings()
        s.validate()
        self.assertEqual(s.floors, 38)
        self.assertEqual({sf.floor: sf.kind for sf in s.special_floors},
                         {1: "lobby", 4: "parking", 5: "amenity", 38: "amenity"})
        self.assertLessEqual(sum(sf.share for sf in s.special_floors), 100)

    def test_missing_and_unknown_keys(self):
        s = Settings.from_dict({"elevators": 4, "some_future_setting": True})
        self.assertEqual(s.elevators, 4)
        self.assertEqual(s.floors, Settings().floors)

    def test_numbers_as_strings_from_form(self):
        self.assertEqual(Settings.from_dict({"floors": "40", "speed": "1.5"}).floors, 40)

    def test_errors_name_the_field(self):
        cases = [
            ({"floors": 1}, "floors"),
            ({"capacity": 2.5}, "capacity"),
            ({"speed": ""}, "speed"),
            ({"special_floors": [{"floor": 50, "kind": "lobby", "share": 10}]}, "special_floors.0.floor"),
            ({"special_floors": [{"floor": 2, "kind": "lobby", "share": 10},
                                 {"floor": 2, "kind": "amenity", "share": 5}]}, "special_floors.1.floor"),
            ({"special_floors": [{"floor": 2, "kind": "spa", "share": 10}]}, "special_floors.0.kind"),
            ({"special_floors": [{"floor": 2, "kind": "lobby", "share": 70},
                                 {"floor": 3, "kind": "amenity", "share": 40}]}, "special_floors.share_total"),
        ]
        for data, field in cases:
            with self.subTest(data=data), self.assertRaises(SettingsError) as ctx:
                Settings.from_dict(data)
            self.assertEqual(ctx.exception.field, field)

    def test_schema_lists_every_simple_field(self):
        keys = [f["key"] for f in Settings.schema()]
        self.assertIn("floors", keys)
        self.assertNotIn("special_floors", keys)
        for f in Settings.schema():
            self.assertTrue(f["label"] and f["group"])


class TestSettingsStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "nested" / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        store = SettingsStore(self.path)
        self.assertEqual(store.load(), Settings())  # no file yet
        s = Settings.from_dict({"elevators": 3, "special_floors": [{"floor": 1, "kind": "lobby", "share": 60, "name": "Main"}]})
        store.save(s)
        self.assertEqual(SettingsStore(self.path).load(), s)
        self.assertEqual(json.loads(self.path.read_text())["version"], 1)
        self.assertEqual(list(self.path.parent.glob(".settings-*")), [])  # no temp files left

    def test_corrupt_file_falls_back_to_defaults_without_overwriting(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertLogs("elevator_sim.settings", "WARNING"):
            self.assertEqual(SettingsStore(self.path).load(), Settings())
        self.assertEqual(self.path.read_text(), "{not json")


class TestMakeSimulation(unittest.TestCase):
    def test_building_matches_settings(self):
        sim, traffic, lobby = make_simulation(Settings())
        self.assertEqual(len(sim.building), 38)
        self.assertEqual(lobby, 0)
        self.assertEqual(sim.building[0].label, "1")
        self.assertEqual(sim.building[3].kind, FloorKind.PARKING)
        self.assertEqual(sim.building[37].name, "Roof deck and gym")
        self.assertEqual(len(sim.elevators), 6)
        self.assertTrue(all(c.position == 0 for c in sim.elevators))

    def test_trip_shares_match_settings(self):
        sim, traffic, _ = make_simulation(Settings())
        n = 20000
        for _ in range(n):
            traffic.spawn_one(sim)
        touches = Counter()
        for p in sim.people.values():
            for f in {p.origin_floor, p.requested_floor}:
                touches[f] += 1
        expected = {0: 0.45, 3: 0.32, 4: 0.08, 37: 0.07}
        for floor, share in expected.items():
            self.assertAlmostEqual(touches[floor] / n, share, delta=0.015, msg=f"floor index {floor}")
        # remaining ~8% of trips are between two residential floors
        between = sum(1 for p in sim.people.values()
                      if p.origin_floor not in expected and p.requested_floor not in expected)
        self.assertAlmostEqual(between / n, 0.08, delta=0.015)

    def test_runs_and_delivers(self):
        sim, traffic, _ = make_simulation(Settings.from_dict({"rate": 30}))
        sim.run(600)
        traffic.per_minute = 0
        sim.run(900)
        sim.check_invariants()
        self.assertTrue(all(isinstance(p.location, Arrived) for p in sim.people.values()))


if __name__ == "__main__":
    unittest.main()
