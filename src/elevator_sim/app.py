"""The desktop app: start the simulator on this computer and open it in the browser.

    python -m elevator_sim.app [--port 8000] [--no-browser]

This is what the downloadable Windows and macOS builds run (see packaging/). Unlike
`elevator_sim.server`, it saves settings and presets in the user's own app data folder, since
a packaged app's files are read-only, and it falls back to any free port if 8000 is taken.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import webbrowser
from pathlib import Path

from .server import create_server, serve

APP_NAME = "Elevator Simulator"
DEFAULT_PORT = 8000
HOST = "127.0.0.1"           # this computer only: the app isn't meant to be reached from others


def resource_dir() -> Path:
    """Where the bundled files (web/) are: PyInstaller's unpacked folder in a build, else the project."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def data_dir(platform: str | None = None, env: dict[str, str] | None = None, home: Path | None = None) -> Path:
    """The per-user folder for settings.json and presets.json."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or Path.home()
    if platform.startswith("win"):
        base = env.get("APPDATA")
        return (Path(base) if base else home / "AppData" / "Roaming") / APP_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "elevator-simulator"


def bind(port: int, web_dir: Path, settings_path: Path):
    """Start on `port`, or on any free port if that one is taken."""
    try:
        return create_server(HOST, port, web_dir, settings_path)
    except OSError:
        if port == 0:
            raise
        return create_server(HOST, 0, web_dir, settings_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=f"Run the {APP_NAME} on this computer.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="falls back to a free port if taken")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    web_dir = resource_dir() / "web"
    if not (web_dir / "index.html").is_file():
        raise SystemExit(f"The app's web files are missing from {web_dir}.")
    settings_path = data_dir() / "settings.json"
    server, _ = bind(args.port, web_dir, settings_path)
    url = f"http://localhost:{server.server_address[1]}/"

    print(f"{APP_NAME} is running at {url}")
    print(f"Settings and presets are saved in {settings_path.parent}")
    print("Keep this window open while you use it. Close it (or press Ctrl+C) to stop the simulator.",
          flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    serve(server)


if __name__ == "__main__":
    main()
