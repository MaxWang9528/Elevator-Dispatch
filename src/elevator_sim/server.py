"""Live web visualization server. Standard library only.

    python -m elevator_sim.server [--host 127.0.0.1] [--port 8000] [--settings data/settings.json]

GET  /                      the web app (files from web/)
GET  /api/stream            Server-Sent Events: a "layout" event, a "profile" event (the expected-traffic curve; again whenever outings per day change), then a "frame" event ~15x a second
POST /api/control           {"paused": bool} | {"speed": number or "max"} | {"outings": per resident per day}
                            | {"lunch": bool} (the lunch peak, for this run)
POST /api/spawn             {"origin": floor index, "destination": floor index (optional)}
POST /api/restart           start the current run over from its beginning
POST /api/run               start a new run: {"mode": "steady"} | {"mode": "morning"} | {"mode": "lunch"} |
                            {"mode": "evening"} | {"mode": "custom", "start": "06:00", "stop": "20:00"}
                            A lunch run has the lunch peak on; other runs start with it as in Settings.
GET  /api/metrics           every metric as JSON (add ?download=1 to save it as a file)
                            Downloads are named after the algorithm and the simulated time
                            covered so far, e.g. lookahead_day_0600-1432_report.json
GET  /api/trips.csv         one row per person, for analysis elsewhere
POST /api/import?name=FILE  a saved report JSON or trips CSV as the body -> {"report": ...}, the same
                            shape as /api/metrics, for the Import tab (nothing is kept on the server)
GET  /api/settings          saved settings + the schema the Settings tab is built from
POST /api/settings          validate, save to disk, restart the simulation
POST /api/settings/defaults restore defaults, save, restart
POST /api/presets/save      {"name": ..., "settings": {...}} save settings as a named preset (replaces
                            a saved preset of the same name; built-in presets can't be replaced)
POST /api/presets/load      {"name": ...} make a preset's settings the saved settings, restart
POST /api/presets/delete    {"name": ...} delete a saved preset
                            GET /api/settings lists the presets; each preset route answers with the list
"""

from __future__ import annotations

import argparse
import dataclasses
import random
import csv
import io
import json
import logging
import mimetypes
import threading
import time
import traceback
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .clock import DAY, RunPlan, parse_time
from .dispatchers import DISPATCHERS
from .imports import MAX_BYTES, ImportFileError, report_from_file
from .presets import PresetStore
from .traffic import VisitorTraffic
from .settings import (
    GROUP_HELP, GROUP_OFFICE, MAX_CARS, MAX_GROUP_CARS, MAX_GROUPS, RUSH_NAMES, SPECIAL_KINDS,
    Settings, SettingsError, SettingsStore, make_simulation,
)

log = logging.getLogger("elevator_sim.server")

FRAME_SECONDS = 1 / 15          # how often frames are sent to browsers (wall clock)
FASTEST = "max"                 # a speed: simulate as fast as this computer can
SPEEDS: tuple[float | str, ...] = (1, 2, 5, 10, 20, 100, 200, FASTEST)
FASTEST_CHUNK = 20              # steps between letting other threads in, at the fastest speed
FASTEST_SHARE = 0.85            # share of each frame's wall time spent simulating at the fastest speed
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_SETTINGS_FILE = PROJECT_ROOT / "data" / "settings.json"
PROFILE_POINTS = 144            # samples of the traffic curve sent to the page for a timed run


class Session:
    """One shared simulation, stepped in a background thread and broadcast to every browser."""

    def __init__(self, store: SettingsStore, presets: PresetStore | None = None) -> None:
        self.store = store
        self.presets = presets or PresetStore.beside(store.path)
        self.settings = store.load()
        self.lock = threading.Lock()
        self.new_frame = threading.Condition()
        self.plan = RunPlan()           # steady traffic until a timed run is started
        self.lunch_on = self.settings.lunch_enabled   # this run's lunch peak (the checkbox)
        self.paused = False
        self.finished = False           # a timed run reached its stop time
        self.speed: float | str = 5
        self.actual_speed = 0.0         # simulated seconds per real second, measured
        self.error: str | None = None
        self.frame_id = 0
        self.frame_json = "{}"
        self.layout_json = "{}"
        self.layout_id = 0
        self.profile_json = "null"      # the expected-traffic curve, sent again when it changes
        self.profile_id = 0
        self._step_debt = 0.0
        with self.lock:
            self._rebuild()

    # --- called from HTTP threads ------------------------------------------------------
    def restart(self) -> None:
        with self.lock:
            if self.finished:
                self.paused = False     # it paused itself at the stop time
            self._rebuild()

    def start_run(self, data: dict[str, Any]) -> dict[str, Any]:
        """Start over with a new run plan, and set it running."""
        mode = data.get("mode") if isinstance(data, dict) else None
        with self.lock:
            if mode == "steady":
                plan = RunPlan()
            elif mode in RUSH_NAMES:
                plan = self.settings.rush_plan(mode)   # raises SettingsError if switched off
            elif mode == "lunch":
                plan = self.settings.lunch_plan()
            elif mode == "custom":
                try:
                    plan = RunPlan.between(parse_time(data.get("start")), parse_time(data.get("stop")))
                except ValueError:
                    raise ValueError("Start and stop must be times of day, like 06:00.") from None
            else:
                raise ValueError("mode must be steady, morning, evening or custom")
            self.plan = plan
            self.lunch_on = mode == "lunch" or self.settings.lunch_enabled
            self.paused = False
            self._rebuild()
            return {"run": self._run_info()}

    def save_settings(self, data: Any) -> Settings:
        settings = Settings.from_dict(data)   # raises SettingsError
        self.store.save(settings)
        with self.lock:
            self.settings = settings
            if self.plan.mode in RUSH_NAMES:
                # The rush this run is built around may have moved or been switched off.
                self.plan = (settings.rush_plan(self.plan.mode)
                             if settings.rush_enabled(self.plan.mode) else RunPlan())
            elif self.plan.mode == "lunch":
                self.plan = settings.lunch_plan()
            self.lunch_on = self.plan.mode == "lunch" or settings.lunch_enabled
            if self.finished:
                self.paused = False
            self._rebuild()
        return settings

    def restore_defaults(self) -> Settings:
        return self.save_settings(Settings().to_dict())

    # --- presets ----------------------------------------------------------------------
    def presets_payload(self) -> dict[str, Any]:
        """Every preset, and the name of the one the saved settings match (None if none does)."""
        with self.lock:
            current = self.settings.to_dict()
        presets = [p.to_dict() for p in self.presets.all()]
        match = next((p["name"] for p in presets if p["settings"] == current), None)
        return {"presets": presets, "current_preset": match}

    def save_preset(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("expected {\"name\": ..., \"settings\": {...}}")
        preset = self.presets.save(data.get("name"), Settings.from_dict(data.get("settings")))
        return {"saved": preset.name, **self.presets_payload()}

    def load_preset(self, data: Any) -> dict[str, Any]:
        preset = self.presets.get(data.get("name") if isinstance(data, dict) else None)
        settings = self.save_settings(preset.settings.to_dict())
        return {"settings": settings.to_dict(), "loaded": preset.name, **self.presets_payload()}

    def delete_preset(self, data: Any) -> dict[str, Any]:
        self.presets.delete(data.get("name") if isinstance(data, dict) else None)
        return self.presets_payload()

    def settings_payload(self) -> dict[str, Any]:
        with self.lock:
            current = self.settings.to_dict()
        return {
            **self.presets_payload(),
            "settings": current,
            "defaults": Settings().to_dict(),
            "schema": Settings.schema(),
            "group_help": GROUP_HELP,
            "group_office": GROUP_OFFICE,
            "group_limits": {"groups": MAX_GROUPS, "cars": MAX_GROUP_CARS, "total": MAX_CARS},
            "kinds": [{"value": k, "label": v} for k, v in SPECIAL_KINDS.items()],
            "file": _display_path(self.store.path),
            "presets_file": _display_path(self.presets.path),
        }

    def metrics_payload(self) -> dict[str, Any]:
        with self.lock:
            report = self.sim.report()
            report["run"].update(self._run_info())
            report["run"]["dispatcher_label"] = DISPATCHERS[self.settings.dispatcher].label
            report["settings"] = self.settings.to_dict()
            report["rate_now"] = round(self._expected(self.sim.time_of_day)["total"], 2)
            report["residents"] = {"total": len(self.traffic.residents), **self.traffic.census().to_dict()}
            report["layout_id"] = self.layout_id
        return report

    def download_name(self, kind: str, ext: str) -> str:
        """A file name for a download: algorithm, run, and the simulated time covered so far.

        lookahead_morning-rush_0630-0812_trips.csv   a timed run, from its start to now
        nearest_steady_2h15m_report.json              a steady run, time since it began
        """
        with self.lock:
            plan, sim = self.plan, self.sim
            run = {"morning": "morning-rush", "lunch": "lunch-rush", "evening": "evening-rush",
                   "custom": "hours"}.get(plan.mode, plan.mode)
            if plan.start is None:
                minutes = round(sim.time) // 60
                span = f"{minutes // 60}h{minutes % 60:02d}m"
            else:
                span = f"{_hhmm(plan.start)}-{_hhmm(plan.start + sim.time)}"
            return f"{self.settings.dispatcher}_{run}_{span}_{kind}.{ext}"

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
                # A finished run stays stopped; Restart runs it again.
                self.paused = bool(data["paused"]) or self.finished
            if "speed" in data:
                speed = data["speed"]
                if speed != FASTEST:
                    speed = float(speed)
                if speed not in SPEEDS:
                    raise ValueError(f"speed must be one of {SPEEDS}")
                self.speed = speed
            if "outings" in data:
                outings = float(data["outings"])
                if not 0 <= outings <= 50:
                    raise ValueError("outings must be between 0 and 50 per resident per day")
                self.traffic.set_outings(self.sim, outings)
                self._set_profile(self._profile())     # the expected traffic changed with it
            if "lunch" in data and bool(data["lunch"]) != self.lunch_on:
                self.lunch_on = bool(data["lunch"])
                self.traffic.set_lunch(self.sim, self.settings.lunch_hour(even_if_off=True)
                                       if self.lunch_on else None)
                self._set_profile(self._profile())
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
        last_wall, last_sim, last_sim_obj = next_frame, 0.0, None
        while True:
            frame_start = time.monotonic()
            with self.lock:
                running = not self.paused and self.error is None
                fastest = running and self.speed == FASTEST
                if running and not fastest:
                    self._advance(self.speed * FRAME_SECONDS)
            if fastest:
                self.run_flat_out(frame_start + FRAME_SECONDS * FASTEST_SHARE)
            with self.lock:
                # How fast the simulation is really going (shown at the fastest speed).
                now = time.monotonic()
                if self.sim is last_sim_obj and now > last_wall:
                    rate = (self.sim.time - last_sim) / (now - last_wall)
                    self.actual_speed = 0.8 * self.actual_speed + 0.2 * rate
                else:
                    self.actual_speed = 0.0
                last_wall, last_sim, last_sim_obj = now, self.sim.time, self.sim
                self._publish()
            next_frame += FRAME_SECONDS
            delay = next_frame - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.monotonic()  # fell behind; don't try to catch up

    def run_flat_out(self, until: float) -> None:
        """Step the simulation as fast as possible until the wall clock reaches `until`.
        Works in small chunks, releasing the lock between them so controls and the metrics
        page still get a turn."""
        while time.monotonic() < until:
            with self.lock:
                if self.speed != FASTEST or self.paused or self.error is not None:
                    return
                self._run_steps(FASTEST_CHUNK)

    # --- internals (caller holds self.lock) -------------------------------------------
    def _rebuild(self) -> None:
        settings = self.settings
        if settings.random_seed:
            settings = dataclasses.replace(settings, seed=random.SystemRandom().randrange(1, 2**31 - 1))
        settings = dataclasses.replace(settings, lunch_enabled=self.lunch_on)
        self.sim, self.traffic, lobby = make_simulation(settings, self.plan)
        self.visitors = next((t for t in self.sim.traffic if isinstance(t, VisitorTraffic)), None)
        self.error = None
        self.finished = False
        self._step_debt = 0.0
        layout = self.sim.layout()
        layout["lobby"] = lobby
        layout["speeds"] = SPEEDS
        layout["run"] = self._run_info()
        layout["rush"] = self._rush_info()
        layout["profile"] = self._profile()
        layout["office"] = settings.office
        layout["groups"] = [{"name": g.name, "cars": g.cars, "floors": g.floors}
                            for g in settings.elevator_groups]
        self._set_profile(layout["profile"])
        self.layout_json = json.dumps(layout)
        self.layout_id += 1
        self._publish()

    def _run_info(self) -> dict[str, Any]:
        plan = self.plan
        return {"mode": plan.mode, "start": plan.start, "stop": plan.stop,
                "duration": plan.duration, "description": plan.describe(),
                "residents": self.settings.residents, "seed": self.sim.seed,
                "random_seed": self.settings.random_seed}

    def _rush_info(self) -> list[dict[str, Any]]:
        """What the rush buttons on the page need: is each rush on, and which hours it would run."""
        info = []
        for name in RUSH_NAMES:
            rush = self.settings.rush_hour(name)
            entry: dict[str, Any] = {"name": name, "enabled": self.settings.rush_enabled(name),
                                     "start": rush.start, "peak": rush.peak, "end": rush.end}
            if entry["enabled"]:
                plan = self.settings.rush_plan(name)
                entry["run_start"], entry["run_stop"] = plan.start, plan.stop
            info.append(entry)
        # Lunch can always be run: a lunch run switches the lunch peak on.
        lunch, plan = self.settings.lunch_hour(even_if_off=True), self.settings.lunch_plan()
        info.append({"name": "lunch", "enabled": True, "start": lunch.start, "peak": lunch.peak,
                     "end": lunch.end, "run_start": plan.start, "run_stop": plan.stop})
        return info

    def _profile(self) -> list[list[float]] | None:
        """Expected traffic over a timed run, as [seconds into the run, trips per minute]."""
        plan = self.plan
        if plan.start is None or plan.duration is None:
            return None
        step = plan.duration / PROFILE_POINTS
        return [[round(i * step, 1), round(self._expected((plan.start + i * step) % DAY)["total"], 3)]
                for i in range(PROFILE_POINTS + 1)]

    def _expected(self, time_of_day: float | None) -> dict[str, float]:
        """Expected trips a minute from residents (or workers) and visitors together, with the
        shares that are commutes out, commutes in, and visitors."""
        res = self.traffic.expected(time_of_day)
        vis = self.visitors.expected(time_of_day)["total"] if self.visitors else 0.0
        total = res["total"] + vis
        scale = res["total"] / total if total else 0.0
        return {"total": total, "leaving": res["leaving"] * scale, "returning": res["returning"] * scale,
                "visiting": vis / total if total else 0.0}

    def _set_profile(self, profile: list[list[float]] | None) -> None:
        self.profile_json = json.dumps(profile)
        self.profile_id += 1

    def _advance(self, sim_seconds: float) -> None:
        self._step_debt += sim_seconds / self.sim.dt
        steps = int(self._step_debt + 1e-9)
        self._step_debt -= steps
        self._run_steps(steps)

    def _run_steps(self, steps: int) -> None:
        """Step the simulation, stopping a timed run at its stop time."""
        duration = self.plan.duration
        if duration is not None:
            steps = min(steps, max(0, round((duration - self.sim.time) / self.sim.dt)))
        try:
            for _ in range(steps):
                self.sim.step()
        except Exception:  # keep serving so the error is visible in the browser
            self.error = traceback.format_exc()
            self.paused = True
            log.exception("simulation crashed")
        if duration is not None and self.sim.time >= duration - self.sim.dt / 2:
            self.finished = True
            self.paused = True
            self._step_debt = 0.0

    def _publish(self) -> None:
        frame = self.sim.snapshot()
        frame["stats"] = self.sim.summary()
        expected = self._expected(self.sim.time_of_day)
        frame["paused"] = self.paused
        frame["finished"] = self.finished
        frame["speed"] = self.speed
        frame["actual_speed"] = round(self.actual_speed, 1)
        frame["outings"] = self.traffic.outings_per_day
        frame["lunch"] = self.lunch_on
        frame["rate_now"] = round(expected["total"], 2)          # expected trips per minute now
        frame["leaving"] = round(expected["leaving"], 3)         # share of them: commutes out
        frame["returning"] = round(expected["returning"], 3)     # share of them: commutes home
        frame["residents"] = {"total": len(self.traffic.residents), **self.traffic.census().to_dict()}
        frame["visitors"] = None if self.visitors is None else self.visitors.inside
        frame["visiting"] = round(expected["visiting"], 3)       # share of trips now: visitors
        frame["layout_id"] = self.layout_id
        frame["error"] = self.error
        payload = json.dumps(frame)
        with self.new_frame:
            self.frame_json = payload
            self.frame_id += 1
            self.new_frame.notify_all()


def _hhmm(seconds: float) -> str:
    """Time of day for a file name: 0630 (no colon, which file systems dislike)."""
    m = round(seconds) // 60 % (24 * 60)      # to the second first: 0.1 s steps drift
    return f"{m // 60:02d}{m % 60:02d}"


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
        "/api/run": session.start_run,
        "/api/settings": lambda d: {"settings": session.save_settings(d).to_dict(),
                                    **session.presets_payload()},
        "/api/settings/defaults": lambda _: {"settings": session.restore_defaults().to_dict(),
                                             **session.presets_payload()},
        "/api/presets/save": session.save_preset,
        "/api/presets/load": session.load_preset,
        "/api/presets/delete": session.delete_preset,
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
                name = session.download_name("report", "json") if "download" in query else None
                self._send_json(HTTPStatus.OK, session.metrics_payload(), download=name)
            elif path == "/api/trips.csv":
                self._send_bytes(session.trips_csv(), "text/csv; charset=utf-8",
                                 download=session.download_name("trips", "csv"))
            else:
                self._static(path)

        def do_POST(self) -> None:
            path, _, query = self.path.partition("?")
            if path == "/api/import":
                return self._import(query)
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
                log.exception("couldn't write to disk")
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                                       {"error": f"Couldn't write the file: {exc}"})
            self._send_json(HTTPStatus.OK, result if isinstance(result, dict) else {"ok": True})

        def _import(self, query: str) -> None:
            name = urllib.parse.parse_qs(query).get("name", ["imported run"])[0][:120]
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BYTES:
                return self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                       {"error": f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB."})
            data = self.rfile.read(length)
            try:
                report = report_from_file(name, data)
            except ImportFileError as exc:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            self._send_json(HTTPStatus.OK, {"report": report})

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
            sent_layout, sent_profile, sent_frame = -1, -1, -1
            try:
                while True:
                    with session.new_frame:
                        session.new_frame.wait_for(lambda: session.frame_id != sent_frame, timeout=15)
                        frame_id, frame = session.frame_id, session.frame_json
                        layout_id, layout = session.layout_id, session.layout_json
                        profile_id, profile = session.profile_id, session.profile_json
                    chunks = []
                    if layout_id != sent_layout:
                        chunks.append(f"event: layout\ndata: {layout}\n\n")
                        sent_layout = layout_id
                    if profile_id != sent_profile:
                        chunks.append(f"event: profile\ndata: {profile}\n\n")
                        sent_profile = profile_id
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
                        help="where settings are saved (created on first save); presets are "
                             "saved to presets.json in the same folder")
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
    log.info("Presets file: %s", session.presets.path)
    log.info("Elevator simulator running at http://%s:%d  (Ctrl+C to stop)", shown, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
