"""Run the simulation without the web page and print its metrics.

    python -m elevator_sim.report --minutes 120
    python -m elevator_sim.report --minutes 120 --seed 7 --outings 4 --json run.json --trips trips.csv
    python -m elevator_sim.report --rush morning --timeline
    python -m elevator_sim.report --start 06:00 --stop 20:00

Building and elevator settings come from the saved settings file (the one the Settings tab
edits), or the defaults if there isn't one. --seed, --residents, --outings and --warmup override
it for this run.

Three kinds of run:
  steady (default)      outings at an even rate, no time of day, no commutes. `--minutes` is
                        the measured time; the warm-up runs first and is not counted.
  --rush morning|evening  the hours around that rush hour, as set in Settings
  --start [--stop]      from one time of day to another, with any rush hours in between. Without
                        --stop it runs for the warm-up plus `--minutes`.
In the two timed runs the warm-up is the first part of the run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from .clock import RunPlan, format_time, parse_time
from .settings import RUSH_NAMES, Settings, SettingsError, SettingsStore, make_simulation
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


def format_report(report: dict[str, Any], timeline: bool = False) -> str:
    """The report as plain text, for a terminal. `timeline` adds a row per chart interval."""
    p, s, f, hc, run = (report[k] for k in ("passengers", "system", "fleet", "hall_calls", "run"))
    start = report["warmup"]
    day_start = run.get("day_start")
    # Timed runs show the time of day; steady runs show time since the run began.
    stamp = clock if day_start is None else (lambda t: format_time(day_start + t))
    lines = [
        "Elevator simulation report",
        f"  {run['floors']} floors, {run['elevators']} elevators, {run['dispatcher']}, seed {run['seed']}",
        f"  Run: {run.get('description', 'steady traffic')}"
        + (f", {run['residents']:,} residents" if run.get("residents") else ""),
        f"  Measured {stamp(start)} to {stamp(report['time'])} "
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
        f"  Straight shots (no other stops on the way): {_pct(p['straight_shot_share'])} "
        f"({p['straight_shots']:,} of {p['delivered']:,} delivered)",
        "",
        f"{'By direction':<24}{'people':>8}{'avg wait':>10}{'95th wait':>11}{'avg trip':>10}{'straight':>10}",
    ]
    for name, d in report["by_direction"].items():
        lines.append(f"  {name.capitalize():<22}{d['people']:>8,}{_num(d['wait_mean']):>10}{_num(d['wait_p95']):>11}"
                     f"{_num(d['trip_mean']):>10}{_pct(d['straight_shot_share']):>10}")

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
    if timeline:
        lines += ["", f"{'Over time':<12}{'arriving':>10}{'delivered':>11}{'avg wait':>10}{'95th wait':>11}{'waiting avg':>13}{'peak':>6}"]
        for b in report["timeline"]:
            if b["complete"]:
                lines.append(f"  {stamp(b['t0']):<10}{b['arrived']:>10,}{b['delivered']:>11,}{_num(b['wait_mean']):>10}"
                             f"{_num(b['wait_p95']):>11}{_num(b['avg_waiting']):>13}{b['peak_waiting']:>6,}")
        lines.append("  (people are grouped by when they pressed the button; rows start at the time shown)")
    return "\n".join(lines)


def write_trips(sim: Simulation, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(sim.TRIP_COLUMNS)
        writer.writerows(sim.trip_rows())


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Run the elevator simulation headless and print its metrics.")
    parser.add_argument("--minutes", type=float, help="simulated minutes to measure, after the warm-up (default 60)")
    parser.add_argument("--rush", choices=RUSH_NAMES, help="run the hours around this rush hour instead of steady traffic")
    parser.add_argument("--start", help="time of day the run starts, like 06:00 (turns on rush hours)")
    parser.add_argument("--stop", help="time of day the run stops, like 20:00 (needs --start)")
    parser.add_argument("--timeline", action="store_true", help="also print a row per chart interval")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_FILE, help="settings file to use")
    parser.add_argument("--seed", type=int, help="override the random seed")
    parser.add_argument("--dispatcher", help="override the dispatch algorithm (see Settings)")
    parser.add_argument("--residents", type=int, help="override the number of residents")
    parser.add_argument("--outings", type=float, help="override outings per resident per day")
    parser.add_argument("--warmup", type=int, help="override the warm-up period, in seconds")
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    parser.add_argument("--trips", type=Path, help="also write one CSV row per person")
    parser.add_argument("--quiet", action="store_true", help="don't print the text report")
    args = parser.parse_args(argv)
    if args.minutes is not None and args.minutes <= 0:
        parser.error("--minutes must be more than 0")
    if args.rush and (args.start or args.stop):
        parser.error("use --rush or --start/--stop, not both")
    if args.stop and not args.start:
        parser.error("--stop needs --start")
    if args.minutes is not None and (args.rush or args.stop):
        parser.error("--minutes can't be combined with --rush or --stop, which set the length of the run")

    settings = SettingsStore(args.settings).load()
    overrides = {k: v for k, v in (("seed", args.seed), ("dispatcher", args.dispatcher), ("residents", args.residents),
                                   ("outings", args.outings), ("warmup", args.warmup)) if v is not None}
    if overrides:
        settings = Settings.from_dict({**settings.to_dict(), **overrides})

    steady_seconds = round(settings.warmup + (args.minutes or 60) * 60)
    try:
        if args.rush:
            plan = settings.rush_plan(args.rush)
        elif args.stop:
            plan = RunPlan.between(parse_time(args.start), parse_time(args.stop))
        elif args.start:
            plan = RunPlan("custom", parse_time(args.start), steady_seconds)
        else:
            plan = RunPlan("steady", None, steady_seconds)
    except SettingsError as exc:
        parser.error(str(exc))
    except ValueError:
        parser.error("--start and --stop must be times of day, like 06:00")

    sim, _, _ = make_simulation(settings, plan)
    sim.run(plan.duration)
    report = sim.report()
    report["run"].update(mode=plan.mode, start=plan.start, stop=plan.stop, duration=plan.duration,
                         description=plan.describe(), residents=settings.residents)
    report["settings"] = settings.to_dict()

    if not args.quiet:
        print(format_report(report, timeline=args.timeline))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nReport written to {args.json}", file=sys.stderr)
    if args.trips:
        write_trips(sim, args.trips)
        print(f"Trips written to {args.trips}", file=sys.stderr)
    return report


if __name__ == "__main__":
    main()
