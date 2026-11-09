# Elevator simulator

A multi-elevator apartment building simulation (Python, standard library only) with a live
web visualization. Defaults approximate Marquee at Block 37 in Chicago: 38 floors served by
6 elevators, ~1,000 residents, with special floors for the lobby (1), parking / Block 37 mall /
CTA Red and Blue lines (4), common areas (5) and the roof deck and gym (38).

Settings are edited on the Settings tab and saved to `data/settings.json` (in Docker, the
`settings` volume), so they persist across restarts. To add a setting, add a field to
`Settings` in `src/elevator_sim/settings.py`; the Settings tab is generated from it.

## Run

**Docker**

```sh
docker compose up --build
```

**Without Docker** (Python 3.11+)

```sh
PYTHONPATH=src python3 -m elevator_sim.server
```

Then open http://localhost:8000.

## Metrics

The Metrics tab shows everything below, live. To get the same numbers without the web page:

```sh
PYTHONPATH=src python3 -m elevator_sim.report --minutes 120
PYTHONPATH=src python3 -m elevator_sim.report --minutes 120 --seed 7 --rate 30 --json run.json --trips trips.csv
```

`--minutes` is the measured time, after the warm-up. Building and elevator settings come from
`data/settings.json` (or the defaults); `--seed`, `--rate` and `--warmup` override it for one run.
The same seed and settings always give the same report. `GET /api/metrics` returns the report as
JSON and `GET /api/trips.csv` one row per person; the Metrics tab has download buttons for both.

**What is measured.** People who appear before the warm-up period (Settings, default 5 minutes)
are left out, and car, queue and hall-call figures only cover time after it, so the start from
an empty building with every car parked at the lobby doesn't skew a comparison.

| Metric | Meaning |
|---|---|
| Wait | Pressing the hall button to boarding. **People still waiting count with their wait so far**, so an algorithm can't look good by leaving people on a floor. |
| Ride | Boarding to arrival. |
| Whole trip | Pressing the button to arrival (wait + ride), for people who have arrived. |
| Average, median, 90th, 95th, 99th, longest | Reported for wait, ride and whole trip. The tail (95th and up) is where algorithms differ most. |
| Waits over 60 s | Share of people whose wait passed the long-wait threshold (Settings). |
| Left behind by a full car | People who were queued for a car's direction when it closed its doors full, and how many times that happened. |
| Other stops during a ride | Times a rider's car stopped at a floor that wasn't theirs. |
| By direction | People, average and 95th percentile wait, and average trip for up and down trips. |
| Hall call response | A hall button lighting to a car opening its doors there. This is what a real controller can measure. Also: buttons lit now and the oldest one. |
| Arrivals and delivered per hour | If delivered stays below arrivals, the cars aren't keeping up. |
| People waiting / riding | Time-averaged over the measured period, plus the peak number waiting. |
| Most delivered in one interval | Peak throughput (5-minute handling capacity, with the default interval). |
| Change in people waiting or riding | Now, compared with when measuring began. A large rise sets the "not keeping up" warning: waits are still growing and the averages understate them. |
| Floors traveled, starts, stops | Per car and in total. Energy isn't modeled; distance and starts stand in for it. |
| Empty travel | Share of distance with nobody aboard. |
| Moving / doors / idle | Share of each car's time. |
| Riders at departure, full departures, peak load | How full cars are when their doors close. |
| Floors traveled per person | Total distance divided by people delivered: lower is more efficient. |
| Over time | Per interval (default 5 minutes), by when people pressed the button: people arriving, delivered, average and 95th percentile wait, average and peak number waiting. |
| By floor | Trips from and to each floor, and average, 95th percentile and longest wait by starting floor. A floor far above the rest is being served unfairly. |

Counts on the Simulation tab (waiting, riding, delivered) are everyone in the building right now;
its averages use the same measured people as the Metrics tab.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Layout

```
src/elevator_sim/
  enums.py        Direction, CarState, FloorKind
  person.py       Person + location (AtFloor | InCar | Arrived)
  building.py     Floor (up/down queues), Building
  elevator.py     Elevator state machine + basic up/down sweep
  manager.py      HallCall, ElevatorManager, Dispatcher interface
  dispatchers.py  NearestCarDispatcher
  simulation.py   clock, people, board()/exit_car(), snapshot(), invariants
  traffic.py      RandomTraffic, FloorShareTraffic (trips weighted by special floor)
  metrics.py      MetricsCollector: running totals + the full report (definitions above)
  report.py       run headless and print the report: python -m elevator_sim.report
  settings.py     Settings (+ schema for the UI), validation, JSON store, make_simulation()
  server.py       web server: static files, live frames (SSE), controls, settings API
web/              index.html, style.css, renderer.js (canvas), metrics.js (tables + charts),
                  settings.js, app.js
```
