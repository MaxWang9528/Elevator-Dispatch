"""Live web visualization server. Standard library only.

    python -m elevator_sim.server [--host 127.0.0.1] [--port 8000] [--settings data/settings.json]

GET  /                      the web app (files from web/)
GET  /api/stream            Server-Sent Events: a "layout" event, then a "frame" event ~15x a second
POST /api/control           {"paused": bool} | {"speed": number} | {"rate": people_per_minute}
POST /api/spawn             {"origin": floor index, "destination": floor index (optional)}
POST /api/restart           start the current settings over from time 0
GET  /api/metrics           every metric as JSON (add ?download=1 to save it as a file)
GET  /api/trips.csv         one row per person, for analysis elsewhere
GET  /api/settings          saved settings + the schema the Settings tab is built from
POST /api/settings          validate, save to disk, restart the simulation
POST /api/settings/defaults restore defaults, save, restart
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import mimetypes
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .settings import SPECIAL_KINDS, Settings, SettingsError, SettingsStore, make_simulation

log = logging.getLogger("elevator_sim.server")

FRAME_SECONDS = 1 / 15          # how often frames are sent to browsers (wall clock)
SPEEDS = (1, 2, 5, 10, 30, 60, 120)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_SETTINGS_FILE = PROJECT_ROOT / "data" / "settings.json"


class Session:
    """One shared simulation, stepped in a background thread and broadcast to every browser."""

    def __init__(self, store: SettingsStore) -> None:
        self.store = store
        self.settings = store.load()
        self.lock = threading.Lock()
        self.new_frame = threading.Condition()
        self.paused = False
        self.speed: float = 5
        self.error: str | None = None
        self.frame_id = 0
        self.frame_json = "{}"
        self.layout_json = "{}"
        self.layout_id = 0
        self._step_debt = 0.0
        with self.lock:
            self._rebuild()

    # --- called from HTTP threads ------------------------------------------------------
    def restart(self) -> None:
        with self.lock:
            self._rebuild()

    def save_settings(self, data: Any) -> Settings:
        settings = Settings.from_dict(data)   # raises SettingsError
        self.store.save(settings)
        with self.lock:
            self.settings = settings
            self._rebuild()
        return settings

    def restore_defaults(self) -> Settings:
        return self.save_settings(Settings().to_dict())

    def settings_payload(self) -> dict[str, Any]:
        with self.lock:
            current = self.settings.to_dict()
        return {
            "settings": current,
            "defaults": Settings().to_dict(),
            "schema": Settings.schema(),
            "kinds": [{"value": k, "label": v} for k, v in SPECIAL_KINDS.items()],
            "file": _display_path(self.store.path),
        }

    def metrics_payload(self) -> dict[str, Any]:
        with self.lock:
            report = self.sim.report()
            report["settings"] = self.settings.to_dict()
            report["rate_now"] = self.traffic.per_minute
            report["layout_id"] = self.layout_id
        return report

    def trips_csv(self) -> bytes:
        out = io.StringIO()
        writer = csv.writer(out)
        with self.lock:
            writer.writerow(self.sim.TRIP_COLUMNS)
            writer.writerows(self.sim.trip_rows())
        return out.getvalue().encode()

    def control(self, data: dict[str, Any]) -> None:
        with self.lock:
            if "paused" in data:
                self.paused = bool(data["paused"])
            if "speed" in data:
                speed = float(data["speed"])
                if speed not in SPEEDS:
                    raise ValueError(f"speed must be one of {SPEEDS}")
                self.speed = speed
            if "rate" in data:
                rate = float(data["rate"])
                if not 0 <= rate <= 600:
                    raise ValueError("rate must be between 0 and 600")
                self.traffic.per_minute = rate
            self._publish()

    def spawn(self, data: dict[str, Any]) -> None:
        with self.lock:
            origin = int(data["origin"])
            dest = data.get("destination")
            self.sim.spawn_person(origin, None if dest is None else int(dest))
            self._publish()

    # --- simulation thread ------------------------------------------------------------
    def run_forever(self) -> None:
        next_frame = time.monotonic()
        while True:
            with self.lock:
                if not self.paused and self.error is None:
                    self._advance(self.speed * FRAME_SECONDS)
                self._publish()
            next_frame += FRAME_SECONDS
            delay = next_frame - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.monotonic()  # fell behind; don't try to catch up

    # --- internals (caller holds self.lock) -------------------------------------------
    def _rebuild(self) -> None:
        self.sim, self.traffic, lobby = make_simulation(self.settings)
        self.error = None
        self._step_debt = 0.0
        layout = self.sim.layout()
        layout["lobby"] = lobby
        layout["speeds"] = SPEEDS
        self.layout_json = json.dumps(layout)
        self.layout_id += 1
        self._publish()

    def _advance(self, sim_seconds: float) -> None:
        self._step_debt += sim_seconds / self.sim.dt
        steps = int(self._step_debt + 1e-9)
        self._step_debt -= steps
        try:
            for _ in range(steps):
                self.sim.step()
        except Exception:  # keep serving so the error is visible in the browser
            self.error = traceback.format_exc()
            self.paused = True
            log.exception("simulation crashed")

    def _publish(self) -> None:
        frame = self.sim.snapshot()
        frame["stats"] = self.sim.summary()
        frame["paused"] = self.paused
        frame["speed"] = self.speed
        frame["rate"] = self.traffic.per_minute
        frame["layout_id"] = self.layout_id
        frame["error"] = self.error
        payload = json.dumps(frame)
        with self.new_frame:
            self.frame_json = payload
            self.frame_id += 1
            self.new_frame.notify_all()


def _display_path(path: Path) -> str:
    """Shown in the UI: relative to the project when possible, so it stays short."""
    resolved = path.resolve()
    return str(resolved.relative_to(PROJECT_ROOT)) if resolved.is_relative_to(PROJECT_ROOT) else str(resolved)


def make_handler(session: Session, web_dir: Path) -> type[BaseHTTPRequestHandler]:
    web_root = web_dir.resolve()

    post_routes: dict[str, Callable[[Any], Any]] = {
        "/api/control": session.control,
        "/api/spawn": session.spawn,
        "/api/restart": lambda _: session.restart(),
        "/api/settings": lambda d: {"settings": session.save_settings(d).to_dict()},
        "/api/settings/defaults": lambda _: {"settings": session.restore_defaults().to_dict()},
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - " + fmt, self.address_string(), *args)

        # --- routing ----------------------------------------------------------------
        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path == "/api/stream":
                self._stream()
            elif path == "/api/settings":
                self._send_json(HTTPStatus.OK, session.settings_payload())
            elif path == "/api/metrics":
                name = "elevator-metrics.json" if "download" in query else None
                self._send_json(HTTPStatus.OK, session.metrics_payload(), download=name)
            elif path == "/api/trips.csv":
                self._send_bytes(session.trips_csv(), "text/csv; charset=utf-8", download="elevator-trips.csv")
            else:
                self._static(path)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path not in post_routes:
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": f"no route {path}"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                result = post_routes[path](data)
            except SettingsError as exc:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc), "field": exc.field})
            except (ValueError, KeyError, TypeError) as exc:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except OSError as exc:
                log.exception("couldn't save settings")
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                       {"error": f"Couldn't write the settings file: {exc}"})
            self._send_json(HTTPStatus.OK, result if isinstance(result, dict) else {"ok": True})

        # --- helpers ----------------------------------------------------------------
        def _send_json(self, status: HTTPStatus, body: dict[str, Any], download: str | None = None) -> None:
            self._send_bytes(json.dumps(body).encode(), "application/json", status, download)

        def _send_bytes(self, data: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK,
                        download: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            file = (web_root / rel).resolve()
            if not file.is_file() or not file.is_relative_to(web_root):
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            data = file.read_bytes()
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _stream(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.close_connection = True
            sent_layout, sent_frame = -1, -1
            try:
                while True:
                    with session.new_frame:
                        session.new_frame.wait_for(lambda: session.frame_id != sent_frame, timeout=15)
                        frame_id, frame = session.frame_id, session.frame_json
                        layout_id, layout = session.layout_id, session.layout_json
                    chunks = []
                    if layout_id != sent_layout:
                        chunks.append(f"event: layout\ndata: {layout}\n\n")
                        sent_layout = layout_id
                    if frame_id != sent_frame:
                        chunks.append(f"event: frame\ndata: {frame}\n\n")
                        sent_frame = frame_id
                    else:
                        chunks.append(": keep-alive\n\n")
                    self.wfile.write("".join(chunks).encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # browser went away

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the elevator simulator web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside Docker")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--web-dir", type=Path, default=DEFAULT_WEB_DIR)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_FILE,
                        help="where settings are saved (created on first save)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    if not (args.web_dir / "index.html").is_file():
        raise SystemExit(f"web files not found in {args.web_dir} (use --web-dir)")

    session = Session(SettingsStore(args.settings))
    threading.Thread(target=session.run_forever, name="sim", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(session, args.web_dir))
    server.daemon_threads = True
    shown = "localhost" if args.host in ("0.0.0.0", "127.0.0.1") else args.host
    log.info("Settings file: %s", args.settings)
    log.info("Elevator simulator running at http://%s:%d  (Ctrl+C to stop)", shown, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
