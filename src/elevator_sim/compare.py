"""Compare dispatch algorithms on the same residents, across many seeds.

    python -m elevator_sim.compare
    python -m elevator_sim.compare --dispatchers nearest,lookahead --runs morning,evening --seeds 1-20
    python -m elevator_sim.compare --runs 07:30-09:00 --seeds 1-5 --json compare.json

Every dispatcher runs every scenario on every seed. A seed fixes the residents and their
plans (see ResidentTraffic), so each comparison is paired: the same day with a different
algorithm. The first dispatcher listed is the baseline; for each other one the report shows
the average change across seeds and its 95% confidence interval. A change counts as "better"
or "worse" only when that interval excludes zero.

Scenarios: morning, evening (the hours around each rush, as set in Settings), midday
(11:00-15:00), day (06:00-20:00), heavy (the morning rush with 4x the outings), or any
"HH:MM-HH:MM". Other settings come from the saved settings file, or the defaults.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from statistics import fmean, stdev
from typing import Any

from .clock import RunPlan, format_time, parse_time
from .dispatchers import DISPATCHERS
from .enums import Direction, FloorKind
from .metrics import percentile
from .presets import load_preset
from .settings import Settings, SettingsError, SettingsStore, make_simulation

DEFAULT_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "data" / "settings.json"
SCENARIOS = ("morning", "evening", "midday", "day", "heavy")
UPPER_FLOOR = 16          # "upper floors" for the morning down-wait metric: this floor and up

# (key, label, which way is better: -1 lower, +1 higher, 0 neither, how to show it)
METRICS: list[tuple[str, str, int, str]] = [
    ("wait_mean", "Average wait (s)", -1, "{:.1f}"),
    ("wait_p95", "95th percentile wait (s)", -1, "{:.1f}"),
    ("busy_hour_p95", "Busiest hour, 95th percentile wait (s)", -1, "{:.1f}"),
    ("long_wait_share", "Waits over the long-wait threshold (%)", -1, "{:.2f}"),
    ("upper_down_wait", f"Down trips from floor {UPPER_FLOOR} and up, average wait (s)", -1, "{:.1f}"),
    ("parking_up_wait", "Up trips from parking floors, average wait (s)", -1, "{:.1f}"),
    ("trip_mean", "Average whole trip (s)", -1, "{:.1f}"),
    ("ride_mean", "Average ride (s)", -1, "{:.1f}"),
    ("straight_share", "Straight shots (%)", 1, "{:.1f}"),
    ("left_behind", "People left behind by a full car", -1, "{:.1f}"),
    ("floors_per_person", "Floors traveled per person delivered", -1, "{:.2f}"),
    ("starts_per_person", "Starts per person delivered", -1, "{:.3f}"),
    ("empty_share", "Empty travel (%)", 0, "{:.1f}"),
    ("car_spread", "Busiest car minus quietest, share of average", -1, "{:.2f}"),
    ("people", "People measured", 0, "{:.0f}"),
]

# Two-sided 95% critical values of Student's t, for 1 to 30 degrees of freedom.
_T95 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179,
        2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064,
        2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def scenario_plan(name: str, settings: Settings) -> tuple[RunPlan, Settings]:
    """The run plan for a scenario name, and the settings to use for it."""
    if name in ("morning", "evening"):
        return settings.rush_plan(name), settings
    if name == "midday":
        return RunPlan.between(parse_time("11:00"), parse_time("15:00"), "midday"), settings
    if name == "day":
        return RunPlan.between(parse_time("06:00"), parse_time("20:00"), "day"), settings
    if name == "heavy":
        heavy = Settings.from_dict({**settings.to_dict(), "outings": settings.outings * 4})
        return heavy.rush_plan("morning"), heavy
    start, sep, stop = name.partition("-")
    if not sep:
        raise ValueError(f"unknown scenario {name!r}")
    return RunPlan.between(parse_time(start), parse_time(stop), "custom"), settings


def run_one(job: tuple[str, str, int, dict[str, Any]]) -> tuple[str, str, int, dict[str, float | None]]:
    """Run one dispatcher on one scenario and seed. (Top level, so worker processes can use it.)"""
    dispatcher, scenario, seed, settings_dict = job
    settings = Settings.from_dict({**settings_dict, "dispatcher": dispatcher, "seed": seed})
    plan, settings = scenario_plan(scenario, settings)
    sim, _, _ = make_simulation(settings, plan)
    sim.run(plan.duration)
    return dispatcher, scenario, seed, summarize(sim)


def summarize(sim) -> dict[str, float | None]:
    """The comparison metrics for a finished run."""
    report = sim.report()
    p, fleet = report["passengers"], report["fleet"]
    now, metrics = sim.time, sim.metrics
    measured = [x for x in sim.people.values() if metrics.measures(x)]

    def wait(x) -> float:
        return (x.board_time if x.board_time is not None else now) - x.spawn_time

    # The busiest hour of the run (most people pressing a button), and its 95th percentile.
    by_hour: dict[int, list[float]] = defaultdict(list)
    for x in measured:
        by_hour[int(x.spawn_time // 3600)].append(wait(x))
    busiest = max(by_hour.values(), key=len, default=[])
    parking = {f.index for f in sim.building.floors if f.kind is FloorKind.PARKING}
    upper = [wait(x) for x in measured
             if x.direction is Direction.DOWN and x.origin_floor >= UPPER_FLOOR - 1]
    garage_up = [wait(x) for x in measured if x.direction is Direction.UP and x.origin_floor in parking]
    delivered = [c["delivered"] for c in report["cars"]]
    total = sum(delivered)

    def pct(v: float | None) -> float | None:
        return None if v is None else v * 100

    return {
        "wait_mean": p["wait"]["mean"],
        "wait_p95": p["wait"]["p95"],
        "busy_hour_p95": percentile(sorted(busiest), 95) if busiest else None,
        "long_wait_share": pct(p["long_wait_share"]),
        "upper_down_wait": fmean(upper) if upper else None,
        "parking_up_wait": fmean(garage_up) if garage_up else None,
        "trip_mean": p["trip"]["mean"],
        "ride_mean": p["ride"]["mean"],
        "straight_share": pct(p["straight_shot_share"]),
        "left_behind": p["left_behind_people"],
        "floors_per_person": fleet["floors_per_delivery"],
        "starts_per_person": fleet["starts"] / fleet["delivered"] if fleet["delivered"] else None,
        "empty_share": pct(fleet["empty_share"]),
        "car_spread": (max(delivered) - min(delivered)) / (total / len(delivered)) if total else None,
        "people": p["measured"],
    }


def paired_change(base: list[float | None], other: list[float | None]) -> tuple[float, float] | None:
    """Mean of (other - base) over seeds where both have a value, and the 95% half-width."""
    diffs = [o - b for b, o in zip(base, other) if b is not None and o is not None]
    if not diffs:
        return None
    if len(diffs) == 1:
        return diffs[0], math.inf
    t = _T95[len(diffs) - 2] if len(diffs) - 1 <= len(_T95) else 1.96
    return fmean(diffs), t * stdev(diffs) / math.sqrt(len(diffs))


def verdict(change: tuple[float, float] | None, better: int) -> str:
    if change is None:
        return "-"
    mean, half = change
    if half == 0 and mean == 0:
        return "same"
    if abs(mean) <= half or better == 0:
        return "no clear difference" if better else ""
    return "better" if mean * better > 0 else "worse"


def compare(dispatchers: list[str], scenarios: list[str], seeds: list[int], settings: Settings,
            jobs: int = 1, progress=None) -> dict[str, Any]:
    """Run everything and return {scenario: {dispatcher: {metric: [value per seed]}}} plus the changes."""
    work = [(d, sc, seed, settings.to_dict()) for sc in scenarios for d in dispatchers for seed in seeds]
    results: dict[str, dict[str, dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))
    done = 0
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for d, sc, seed, summary in pool.map(run_one, work):
                results[sc][d][seed] = summary
                done += 1
                if progress:
                    progress(done, len(work))
    else:
        for job in work:
            d, sc, seed, summary = run_one(job)
            results[sc][d][seed] = summary
            done += 1
            if progress:
                progress(done, len(work))

    out: dict[str, Any] = {"dispatchers": dispatchers, "scenarios": scenarios, "seeds": seeds,
                           "baseline": dispatchers[0], "results": {}}
    for sc in scenarios:
        per = {d: {key: [results[sc][d][s][key] for s in seeds] for key, *_ in METRICS} for d in dispatchers}
        changes = {}
        for d in dispatchers[1:]:
            changes[d] = {}
            for key, _, better, _ in METRICS:
                ch = paired_change(per[dispatchers[0]][key], per[d][key])
                changes[d][key] = {"change": None if ch is None else ch[0],
                                   "ci95": None if ch is None else ch[1],
                                   "verdict": verdict(ch, better)}
        out["results"][sc] = {"values": per, "changes": changes}
    return out


def _mean(values: list[float | None]) -> float | None:
    v = [x for x in values if x is not None]
    return fmean(v) if v else None


def format_comparison(result: dict[str, Any], settings: Settings) -> str:
    dispatchers, base = result["dispatchers"], result["baseline"]
    seeds = result["seeds"]
    lines = [f"Comparing {', '.join(dispatchers)} on {len(seeds)} seeds "
             f"({seeds[0]}-{seeds[-1]}). Baseline: {base}.",
             "Values are averages over seeds. Change is against the baseline, paired by seed, with "
             "its 95% confidence interval."]
    for sc in result["scenarios"]:
        plan, _ = scenario_plan(sc, settings)
        when = f"{format_time(plan.start)} to {format_time(plan.stop)}" if plan.start is not None else ""
        lines += ["", f"== {sc} ({when})"]
        values, changes = result["results"][sc]["values"], result["results"][sc]["changes"]
        width = max(len(label) for _, label, *_ in METRICS) + 2
        col = {d: max(10, len(d)) + 2 for d in dispatchers}
        header = f"{'':{width}}" + "".join(f"{d:>{col[d]}}" for d in dispatchers)
        for d in dispatchers[1:]:
            header += f"   {'change (' + d + ')':<34}"
        lines.append(header)
        for key, label, better, form in METRICS:
            row = f"{label:<{width}}"
            for d in dispatchers:
                m = _mean(values[d][key])
                row += f"{'-' if m is None else form.format(m):>{col[d]}}"
            for d in dispatchers[1:]:
                c = changes[d][key]
                if c["change"] is None:
                    row += f"   {'-':<34}"
                    continue
                text = f"{c['change']:+.2f} ± {c['ci95']:.2f}"
                tag = c["verdict"]
                row += f"   {text + ('  ' + tag if tag else ''):<34}"
            lines.append(row.rstrip())
    return "\n".join(lines)


def _parse_seeds(text: str) -> list[int]:
    seeds: list[int] = []
    for part in text.split(","):
        lo, sep, hi = part.partition("-")
        seeds += list(range(int(lo), int(hi) + 1)) if sep else [int(lo)]
    if not seeds:
        raise ValueError("no seeds")
    return seeds


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Compare dispatch algorithms across seeds and scenarios.")
    parser.add_argument("--dispatchers", default=",".join(DISPATCHERS),
                        help=f"comma-separated, baseline first (default: all: {','.join(DISPATCHERS)})")
    parser.add_argument("--runs", default=",".join(SCENARIOS),
                        help=f"comma-separated scenarios (default: {','.join(SCENARIOS)}), or HH:MM-HH:MM")
    parser.add_argument("--seeds", default="1-10", help="like 1-20 or 101-120 or 1,5,9 (default 1-10)")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1, help="parallel processes")
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_FILE, help="settings file to use")
    parser.add_argument("--preset", help="use this preset's settings instead (built-in, or saved next to the settings file)")
    parser.add_argument("--json", type=Path, help="also write every value as JSON")
    args = parser.parse_args(argv)

    dispatchers = [d.strip() for d in args.dispatchers.split(",") if d.strip()]
    unknown = [d for d in dispatchers if d not in DISPATCHERS]
    if unknown or len(dispatchers) < 2:
        parser.error(f"give at least two dispatchers from: {', '.join(DISPATCHERS)}")
    settings = SettingsStore(args.settings).load()
    if args.preset:
        settings = load_preset(parser, args.settings, args.preset)
    scenarios = [s.strip() for s in args.runs.split(",") if s.strip()]
    try:
        seeds = _parse_seeds(args.seeds)
        for sc in scenarios:
            scenario_plan(sc, settings)
    except SettingsError as exc:
        parser.error(str(exc))
    except ValueError as exc:
        parser.error(f"{exc}. Scenarios: {', '.join(SCENARIOS)}, or HH:MM-HH:MM")

    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        print(f"\r  {done}/{total} runs, {time.monotonic() - started:.0f} s", end="", file=sys.stderr)

    result = compare(dispatchers, scenarios, seeds, settings, max(1, args.jobs), progress)
    print(file=sys.stderr)
    print(format_comparison(result, settings))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"\nAll values written to {args.json}", file=sys.stderr)
    return result


if __name__ == "__main__":
    main()
