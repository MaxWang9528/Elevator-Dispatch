import json
import tempfile
import unittest
from pathlib import Path

from elevator_sim.presets import BUILTIN_PRESETS, PresetStore
from elevator_sim.server import Session
from elevator_sim.settings import Settings, SettingsError, SettingsStore, make_simulation


class TestBuiltinPresets(unittest.TestCase):
    def test_first_is_the_defaults(self):
        self.assertEqual(BUILTIN_PRESETS[0].settings.to_dict(), Settings().to_dict())

    def test_every_builtin_is_valid_and_runs(self):
        for preset in BUILTIN_PRESETS:
            with self.subTest(preset.name):
                Settings.from_dict(preset.settings.to_dict())
                sim, _, _ = make_simulation(preset.settings)
                sim.run(120)


class TestPresetStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "presets.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_save_survives_reload(self):
        PresetStore(self.path).save("  Small   building ", Settings.from_dict({"floors": 10, "elevators": 2, "special_floors": []}))
        store = PresetStore(self.path)
        preset = store.get("small building")
        self.assertEqual(preset.name, "Small building")
        self.assertEqual((preset.settings.floors, preset.settings.elevators), (10, 2))
        self.assertFalse(preset.builtin)

    def test_same_name_replaces(self):
        store = PresetStore(self.path)
        store.save("A", Settings.from_dict({"elevators": 2}))
        store.save("a", Settings.from_dict({"elevators": 3}))
        user = [p for p in store.all() if not p.builtin]
        self.assertEqual(len(user), 1)
        self.assertEqual(user[0].settings.elevators, 3)

    def test_builtins_are_protected(self):
        store = PresetStore(self.path)
        name = BUILTIN_PRESETS[0].name
        with self.assertRaises(SettingsError):
            store.save(name.upper(), Settings())
        with self.assertRaises(SettingsError):
            store.delete(name)

    def test_delete_and_missing(self):
        store = PresetStore(self.path)
        store.save("Gone soon", Settings())
        store.delete("Gone soon")
        self.assertNotIn("Gone soon", [p.name for p in PresetStore(self.path).all()])
        with self.assertRaises(SettingsError):
            store.get("Gone soon")

    def test_bad_names(self):
        store = PresetStore(self.path)
        for name in ("", "   ", None, "x" * 61):
            with self.subTest(name=name), self.assertRaises(SettingsError) as ctx:
                store.save(name, Settings())
            self.assertEqual(ctx.exception.field, "preset_name")

    def test_bad_entries_are_skipped(self):
        self.path.write_text(json.dumps({"version": 1, "presets": [
            {"name": "Good", "settings": {"floors": 40}},
            {"name": "Bad", "settings": {"floors": 1}},
            {"settings": {}},
            "nonsense",
        ]}))
        names = [p.name for p in PresetStore(self.path).all() if not p.builtin]
        self.assertEqual(names, ["Good"])

    def test_unreadable_file_means_no_saved_presets(self):
        self.path.write_text("{not json")
        self.assertEqual([p for p in PresetStore(self.path).all() if not p.builtin], [])


class TestSessionPresets(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.session = Session(SettingsStore(Path(self.dir.name) / "settings.json"))

    def tearDown(self):
        self.dir.cleanup()

    def test_load_saves_settings_and_rebuilds(self):
        name = BUILTIN_PRESETS[1].name
        result = self.session.load_preset({"name": name})
        self.assertEqual(result["current_preset"], name)
        self.assertEqual(self.session.settings.floors, BUILTIN_PRESETS[1].settings.floors)
        self.assertEqual(len(self.session.sim.building.floors), BUILTIN_PRESETS[1].settings.floors)
        self.assertEqual(self.session.store.load().floors, BUILTIN_PRESETS[1].settings.floors)

    def test_save_validates_and_marks_current(self):
        with self.assertRaises(SettingsError):
            self.session.save_preset({"name": "Broken", "settings": {"floors": 1}})
        result = self.session.save_preset({"name": "Mine", "settings": Settings().to_dict()})
        self.assertEqual(result["saved"], "Mine")
        # The defaults match both the first built-in and the new preset; the built-in comes first.
        self.assertEqual(result["current_preset"], BUILTIN_PRESETS[0].name)
        self.assertEqual(Path(self.dir.name, "presets.json").exists(), True)

    def test_settings_payload_lists_presets(self):
        payload = self.session.settings_payload()
        self.assertEqual([p["name"] for p in payload["presets"]], [p.name for p in BUILTIN_PRESETS])
        self.assertEqual(payload["current_preset"], BUILTIN_PRESETS[0].name)


if __name__ == "__main__":
    unittest.main()
