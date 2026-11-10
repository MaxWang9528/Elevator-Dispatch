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

## Residents and traffic

Every trip is made by one of the building's residents (Settings: 1,000 by default, spread evenly
over the residential floors). Each resident is at home, on the way somewhere, or away, and everyone
who goes somewhere comes back, so the amount of traffic follows from how many people live here.
With the defaults a resident makes about 4 elevator trips a day, around 4,100 for the building.

- **Outings** (default 1.5 per resident per day): errands, the gym, the mail room, visiting a
  neighbor. A resident at home goes to a special floor, picked by its share (Settings → Special
  floors), or otherwise to another resident's floor. They stay a while, depending on the kind of
  floor (a few minutes at the mailboxes up to an hour or two out of the building), then go home.
  In a timed run outings follow a daily curve: rare at night, steady through the day, busiest
  in the early evening.
- **Commutes**, only in runs with a time of day. Each day a share of residents (default 65%) leave
  in the morning rush through a Lobby or Parking / transit floor, split by those floors' shares
  (58 : 25 between floor 1 and floor 4 by default), and a share (default 55%) come home in the
  evening rush through the same floor they left by. Departures within a rush follow a smooth
  S-shaped ramp up to the peak and back down. Commuters who don't come home in the evening rush
  come home later in the evening; with the evening rush switched off, they come home spread over
  the afternoon and evening.

With the defaults, a morning rush run (6:30 to 10:30) has about 1,000 trips, peaking around 8:30
at about 8 to 9 a minute.

## Runs

The Simulation tab's **Start a run** section picks what kind of run to simulate:

| Run | What happens |
|---|---|
| **Morning rush** / **Evening rush** | From 30 minutes before that rush starts to 30 minutes after it ends, then stops. The clock shows the time of day. Residents who have already left for the day when the run begins start out of the building. |
| **From … to …** | Between any two times of day, with whichever rush hours fall in between, then stops. The same time twice is a full day; a stop before the start runs past midnight. |
| **Steady traffic** | The default: outings at an even rate, no time of day, no commutes. Runs until you pause it. |

Speeds run from 1× to 200× real time; **Fastest** simulates as fast as the computer allows
(the status line shows how fast that is), which suits running a whole day to look at the Metrics.
A run that reaches its stop time pauses itself; **Restart** runs it again from its start. The
slider sets outings per resident per day for the current run (raise it to stress the elevators).
During a timed run, a small curve under it shows the expected trips a minute over the whole run.
Below the stats, a line shows where the residents are right now.

## Dispatching

Settings → Dispatching → Algorithm picks how cars are sent. Every algorithm only uses what a real
group controller knows: which hall buttons are lit and when, and each car's position, speed,
direction, load and stops. None of them sees where waiting people want to go.

| Algorithm | What it does |
|---|---|
| `nearest` | The baseline. The car that would reach the caller soonest, counting the stops it already has. Each call is assigned once, and idle cars wait wherever they stopped. Each car turns around after its last stop in a direction (the SCAN variant known as LOOK). |
| `scan` | The classic elevator algorithm. Each car sweeps all the way to one end of the building and back, stopping for every call in its direction as it passes, and turns around only at the ends, even with nobody aboard. A call goes to the car that will pass it soonest going its way; a car resting at an end sets off when it would reach a waiting call before any sweeping car. |
| `lookahead` (default) | Looks ahead three ways. **Along each car's route:** it works out when the car would really reach the caller, stop by stop, and adds the delay the extra stop causes the car's riders and other pickups; a call that would leave someone waiting past 60 s gets extra weight, and a nearly full car is avoided. **To the next calls:** idle cars park where calls are expected (below). **To a better car:** every second, a call that its car isn't already braking for moves to another car if that one is at least 5 s cheaper. Ties go to the car that has traveled least. |

**Parking** learns where calls come from: each hall call counts, fading by half every 10 minutes.
A car that has been idle for 3 s is offered every floor, scored by the expected time for the
nearest waiting car to reach the next call; it moves only if that improves by at least 6 s
(checked again every 30 s while it stays idle). In practice idle cars spread through the upper
floors in the morning and gather at floors 1 and 4 in the evening. Parking costs travel: expect
10–25% more floors traveled per person.

### Comparing algorithms

```sh
PYTHONPATH=src python3 -m elevator_sim.compare --seeds 101-120
PYTHONPATH=src python3 -m elevator_sim.compare --dispatchers nearest,lookahead --runs morning,evening --seeds 1-20 --json compare.json
```

Every algorithm runs every scenario on every seed, in parallel. A seed fixes the residents and
their plans: traffic has its own random numbers, separate from anything the elevators do, so the
same seed gives the same day with a different algorithm. The first algorithm listed is the
baseline. For each other one the table shows the average change across seeds with its 95%
confidence interval, and calls it better or worse only when that interval excludes zero.
Scenarios: `morning`, `evening`, `midday` (11:00–15:00), `day` (06:00–20:00), `heavy` (the morning
rush with 4× the outings), or any `HH:MM-HH:MM`. Tune on one set of seeds and judge on another,
so a result isn't just fitted to its seeds.

## Metrics

The Metrics tab shows everything below, live. To get the same numbers without the web page:

```sh
PYTHONPATH=src python3 -m elevator_sim.report --minutes 120
PYTHONPATH=src python3 -m elevator_sim.report --minutes 120 --seed 7 --outings 4 --json run.json --trips trips.csv
PYTHONPATH=src python3 -m elevator_sim.report --rush morning --timeline
PYTHONPATH=src python3 -m elevator_sim.report --start 06:00 --stop 20:00
```

`--minutes` is the measured time of a steady run, after the warm-up. `--rush morning|evening` and
`--start`/`--stop` run the same timed runs as the Simulation tab; in those the warm-up is the first
part of the run. `--timeline` adds a row per chart interval, labeled with the time of day.
Building and elevator settings come from
`data/settings.json` (or the defaults); `--seed`, `--dispatcher`, `--residents`, `--outings` and
`--warmup` override it for one run.
The same seed and settings always give the same report. `GET /api/metrics` returns the report as
JSON and `GET /api/trips.csv` one row per trip, including which resident made it and why
(`commute_out`, `commute_in`, `outing`, `outing_return`); the Metrics tab has download buttons for both.
Downloaded files are named after the algorithm, the run, and the simulated time covered so far,
e.g. `lookahead_hours_0600-2000_trips.csv`, `nearest_morning-rush_0630-0812_report.json` or
`scan_steady_2h15m_report.json`.

**Import tab.** Open a saved report (JSON) or trips file (CSV) to see it laid out exactly like
the Metrics tab. A report already has everything. A trips file is replayed through the same
metrics code, tick by tick, so its passenger figures, queues and charts match what the Metrics
tab showed for that run; it doesn't record the elevators or the hall buttons, so those figures
are blank. Trips files carry the run's algorithm, seed, warm-up, long-wait threshold, chart
interval and length on every row so they can be read back on their own (older files still
import, with those worked out from the trips). Recent imports are remembered in the browser.

**What is measured.** People who appear before the warm-up period (Settings, default 5 minutes)
are left out, and car, queue and hall-call figures only cover time after it, so the start from
an empty building with every car parked at the lobby doesn't skew a comparison.

| Metric | Meaning |
|---|---|
| Wait | Pressing the hall button to boarding. **People still waiting count with their wait so far**, so an algorithm can't look good by leaving people on a floor. |
| Ride | Boarding to arrival. |
| Whole trip | Pressing the button to arrival (wait + ride), for people who have arrived. |
| Average, median, 90th, 95th, 99th, longest | Reported for wait, ride and whole trip. The tail (95th and up) is where algorithms differ most. |
| Whole trip by percentile | A chart under the passengers table: the whole-trip time at every percentile from 0 (fastest) to 100 (slowest). In the report as `passengers.trip_percentiles` (101 values). |
| Waits over 60 s | Share of people whose wait passed the long-wait threshold (Settings). |
| Left behind by a full car | People who were queued for a car's direction when it closed its doors full, and how many times that happened. |
| Other stops during a ride | Times a rider's car stopped at a floor that wasn't theirs. |
| Straight shots | Share of delivered riders whose car went straight to their floor without stopping anywhere else. Also shown for up and down trips. |
| By direction | People, average and 95th percentile wait, average trip, and straight shots for up and down trips. |
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
  elevator.py     Elevator state machine + basic up/down sweep, parking
  manager.py      HallCall, ElevatorManager, Dispatcher interface
  dispatchers.py  NearestCarDispatcher, LookaheadDispatcher (with CallDemand and Parker), DISPATCHERS
  simulation.py   clock, people, board()/exit_car(), snapshot(), invariants
  clock.py        times of day, RunPlan (steady, a rush, or chosen hours)
  traffic.py      ResidentTraffic (residents, outings, commutes), RushHour; simpler memoryless sources
  metrics.py      MetricsCollector: running totals + the full report (definitions above)
  report.py       run headless and print the report: python -m elevator_sim.report
  compare.py      compare algorithms across seeds and scenarios: python -m elevator_sim.compare
  settings.py     Settings (+ schema for the UI), validation, JSON store, make_simulation()
  server.py       web server: static files, live frames (SSE), controls, runs, settings API
web/              index.html, style.css, renderer.js (canvas), metrics.js (tables + charts),
                  settings.js, app.js
```
