"""The desktop app launcher: where its files are, where it saves, and which port it uses."""

import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from elevator_sim import app

WEB = Path(__file__).resolve().parents[1] / "web"
HOME = Path("/home/someone")


class DataDirTest(unittest.TestCase):
    def test_windows_uses_appdata(self):
        self.assertEqual(app.data_dir("win32", {"APPDATA": r"C:\Users\x\AppData\Roaming"}, HOME),
                         Path(r"C:\Users\x\AppData\Roaming") / "Elevator Simulator")
        self.assertEqual(app.data_dir("win32", {}, HOME), HOME / "AppData" / "Roaming" / "Elevator Simulator")

    def test_macos_uses_application_support(self):
        self.assertEqual(app.data_dir("darwin", {}, HOME),
                         HOME / "Library" / "Application Support" / "Elevator Simulator")

    def test_linux_uses_xdg(self):
        self.assertEqual(app.data_dir("linux", {}, HOME), HOME / ".local" / "share" / "elevator-simulator")
        self.assertEqual(app.data_dir("linux", {"XDG_DATA_HOME": "/data"}, HOME),
                         Path("/data") / "elevator-simulator")


class ResourceDirTest(unittest.TestCase):
    def test_project_root_when_not_packaged(self):
        self.assertTrue((app.resource_dir() / "web" / "index.html").is_file())

    def test_bundle_folder_when_packaged(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", "/tmp/_MEI123", create=True):
            self.assertEqual(app.resource_dir(), Path("/tmp/_MEI123"))


class BindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "settings.json"

    def start(self, port):
        server, _ = app.bind(port, WEB, self.settings)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_serves_the_page(self):
        server = self.start(0)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as res:
            self.assertIn('id="view-import"', res.read().decode())

    def test_falls_back_to_a_free_port_when_taken(self):
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind((app.HOST, 0))
        taken.listen()
        port = taken.getsockname()[1]
        server = self.start(port)
        self.assertNotEqual(server.server_address[1], port)
        self.assertNotEqual(server.server_address[1], 0)


if __name__ == "__main__":
    unittest.main()
