"""Run the simulation without the web page and print its metrics.

    python -m elevator_sim.report --minutes 120
    python -m elevator_sim.report --minutes 120 --seed 7 --rate 30 --json run.json --trips trips.csv

Building and elevator settings come from the saved settings file (the one the Settings tab
edits), or the defaults if there isn't one. --seed, --rate and --warmup override it for this run.
`--minutes` is the measured time; the warm-up runs first and is not counted.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from .settings import Settings, SettingsStore, make_simulation
from .simulation import Simulation

DEFAULT_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data" / "settings.json"


def clock(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _num(value: Any, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}{suffix}"
    return f"{value:,}{suffix}"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def format_report(report: dict[str, Any]) -> str:
    """The report as plain text, for a terminal."""
    p, s, f, hc, run = (report[k] for k in ("passengers", "system", "fleet", "hall_calls", "run"))
    start = report["warmup"]
    lines = [
        "Elevator simulation report",
        f"  {run['floors']} floors, {run['elevators']} elevators, {run['dispatcher']}, seed {run['seed']}",
        f"  Measured {clock(start)} to {clock(report['time'])} "
        f"({report['measured_seconds'] / 60:.1f} min), {p['measured']:,} people",
        "",
    ]
    if s["overloaded"]:
        lines += [f"  NOT KEEPING UP: {s['backlog_change']:+,} people in queues and cars since "
                  "measuring began. Waits are still growing, so these averages understate them.", ""]

    lines.append(f"{'Passengers (seconds)':<24}{'mean':>8}{'median':>8}{'90th':>8}{'95th':>8}{'99th':>8}{'max':>8}{'people':>9}")
    for label, key in (("Wait", "wait"), ("Ride", "ride"), ("Whole trip", "trip")):
        d = p[key]
        lines.append(f"  {label:<22}" + "".join(f"{_num(d[k]):>8}" for k in ("mean", "p50", "p90", "p95", "p99", "max"))
                     + f"{d['n']:>9,}")
    lines += [
        f"  Waits over {report['long_wait']:g} s: {_pct(p['long_wait_share'])} ({p['long_waits']:,} people)",
        f"  Still waiting: {p['waiting']:,}    Riding: {p['riding']:,}    Delivered: {p['delivered']:,}",
        f"  Left behind by a full car: {p['left_behind_people']:,} people, {p['left_behind_events']:,} times",
        f"  Stops during a ride, other than their own: {_num(p['stops_per_ride'], digits=2)}",
        "",
        f"{'By direction':<24}{'people':>8}{'avg wait':>10}{'95th wait':>11}{'avg trip':>10}",
    ]
    for name, d in report["by_direction"].items():
        lines.append(f"  {name.capitalize():<22}{d['people']:>8,}{_num(d['wait_mean']):>10}{_num(d['wait_p95']):>11}{_num(d['trip_mean']):>10}")

    r = hc["response"]
    lines += [
        "",
        "Hall calls (button press to doors open)",
        f"  Answered: {hc['answered']:,}    avg {_num(r['mean'], ' s')}    95th {_num(r['p95'], ' s')}    longest {_num(r['max'], ' s')}",
        f"  Lit right now: {hc['open']:,}    oldest {_num(hc['oldest_open'], ' s')}",
        "",
        "Building",
        f"  Arrivals: {_num(s['arrivals_per_hour'])} per hour    Delivered: {_num(s['delivered_per_hour'])} per hour",
        f"  People waiting: avg {_num(s['avg_waiting'])}, peak {s['peak_waiting']:,}    People riding: avg {_num(s['avg_riding'])}",
        f"  Most delivered in one {report['bucket_seconds'] / 60:g}-minute interval: {_num(s['peak_delivered_per_interval'])}",
        f"  Change in people in the system since measuring began: {s['backlog_change']:+,}",
        "",
        f"{'Elevators':<10}{'floors':>9}{'empty':>8}{'starts':>8}{'stops':>7}{'moving':>8}{'doors':>7}{'idle':>7}{'avg load':>10}{'full':>7}{'peak':>6}{'delivered':>11}",
    ]
    for row in [*report["cars"], {"id": "All", **f}]:
        name = f"Car {row['id'] + 1}" if isinstance(row["id"], int) else "All"
        lines.append(
            f"  {name:<8}{row['distance']:>9,.0f}{_pct(row['empty_share']):>8}{row['starts']:>8,}{row['door_cycles']:>7,}"
            f"{_pct(row['share_moving']):>8}{_pct(row['share_doors']):>7}{_pct(row['share_idle']):>7}"
            f"{_num(row['avg_load_departing'], digits=2):>10}{_pct(row['full_departure_share']):>7}{row['peak_load']:>6}{row['delivered']:>11,}")
    lines += [
        f"  Floors traveled per person delivered: {_num(f['floors_per_delivery'])}",
        "  (empty = share of distance with nobody aboard; avg load = riders when the doors close;",
        "   full = share of departures at capacity. Energy isn't modeled: distance and starts stand in for it.)",
    ]
    return "\n".join(lines)


def write_trips(sim: Simulation, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(sim.TRIP_COLUMNS)
        writer.writerows(sim.trip_rows())


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run the elevator simulation headless and print its metrics.")
    parser.add_argument("--minutes", type=float, default=60, help="simulated minutes to measure, after the warm-up (default 60)")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_FILE, help="settings file to use")
    parser.add_argument("--seed", type=int, help="override the random seed")
    parser.add_argument("--rate", type=float, help="override new people per minute")
    parser.add_argument("--warmup", type=int, help="override the warm-up period, in seconds")
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    parser.add_argument("--trips", type=Path, help="also write one CSV row per person")
    parser.add_argument("--quiet", action="store_true", help="don't print the text report")
    args = parser.parse_args(argv)
    if args.minutes <= 0:
        parser.error("--minutes must be more than 0")

    settings = SettingsStore(args.settings).load()
    overrides = {k: v for k, v in (("seed", args.seed), ("rate", args.rate), ("warmup", args.warmup)) if v is not None}
    if overrides:
        settings = Settings.from_dict({**settings.to_dict(), **overrides})

    sim, _, _ = make_simulation(settings)
    sim.run(settings.warmup + args.minutes * 60)
    report = sim.report()
    report["settings"] = settings.to_dict()

    if not args.quiet:
        print(format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nReport written to {args.json}", file=sys.stderr)
    if args.trips:
        write_trips(sim, args.trips)
        print(f"Trips written to {args.trips}", file=sys.stderr)
    return report


if __name__ == "__main__":
    main()
