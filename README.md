# Elevator simulator

Deployed version with limited functionality:
https://elevator-dispatch.onrender.com/

A multi-elevator apartment building simulation (Python, standard library only) with a live
web visualization. Defaults approximate Marquee at Block 37 in Chicago: 38 floors served by
6 elevators, ~1,000 residents, with special floors for the lobby (1), parking / Block 37 mall /
CTA Red and Blue lines (4), common areas (5) and the roof deck and gym (38).

Settings are edited on the Settings tab and saved to `data/settings.json` (in Docker, the
`settings` volume), so they persist across restarts. To add a setting, add a field to
`Settings` in `src/elevator_sim/settings.py`; the Settings tab is generated from it.

## Presets

A preset is a whole set of settings under a name: the building, its elevators, its residents,
rush hours and everything else on the Settings tab. The Presets section at the top of that tab
lists them, marks the one in use, and loads one with a click (which saves it as the settings and
restarts the simulation). **Save preset** stores what's on the tab under a name, including edits
not yet saved; saving under an existing name replaces that preset. Saved presets go in
`presets.json` next to the settings file (`data/presets.json`, or the `settings` volume in Docker).

Five come built in and can't be changed or deleted: **Marquee at Block 37** (the defaults),
**Mid-rise, 12 floors** (2 slower cars, 300 residents), **Tower, 60 floors** (2,000 residents,
a low-rise bank to floor 30 and a high-rise bank that runs express from the garage and shops to
the sky lounge on 30) and **Office tower, 50 floors** (4,000 people at work, three zones of 4
cars from the lobby and food court, and a lunch peak) and **One World Trade Center**.

**One World Trade Center** follows public sources where they exist: floor numbers to 104, the
lobby and base on 1-19, offices on 20-90, the sky lobby on 64, the observatory on 100-102, and
5 Sky Pods from the lobby to 102 (calibrated to the published ride of about 47 seconds). The
rest is estimated, because it isn't published: the banks (four of 8 cars from the lobby for 20-63,
8 shuttles from the lobby to 64, and two local banks of 6 cars from 64 for 65-90; 57 cars in all,
against the building's 71-73 including service cars), their speeds, and 13,000 people at work
(about one per 200 sq ft of its 2.6 million sq ft of offices). As in the real building, people
working above 64 ride a shuttle to the sky lobby and change there to a local car, both ways.
Destination dispatch isn't modeled. About 4,400 observatory visitors a day (15 million from the 2015 opening to November
2024) ride the Sky Pods up to 102 and back down from 100, between 9:00 and 21:00 with last entry
at 20:15, busiest in the afternoon, staying 75 minutes on average.

The command-line tools take `--preset NAME` (case doesn't matter) to use a preset's settings
instead of the saved ones:

```sh
PYTHONPATH=src python3 -m elevator_sim.report --preset "Tower, 60 floors" --rush morning
PYTHONPATH=src python3 -m elevator_sim.compare --preset "Mid-rise, 12 floors" --seeds 1-10
```

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

## Floors with no stops

Settings → Building → **Floors with no stops** lists floors nobody lives or works on and no
elevator has to stop at: mechanical floors, or floor numbers a building skips (floor numbers and
ranges, like `2-19, 91-99`). Nobody's home is there and no trip goes there, so no elevator group
has to serve them. They are drawn greyed out.

## Elevator groups

Settings → Elevators lists the building's elevator groups (banks). Each group has a name, a
number of cars, the floors it serves (`all`, or floor numbers and ranges like `1, 20-38`), and
its own car size and speed. Acceleration and door times apply to every car.

Each group works like a separate bank in a real building: it has its own hall buttons and its own
controller running the chosen algorithm, and it only sees its own cars and calls. A person rides
one group's cars at a time. When more than one group serves both their floors, they take the one
that serves the fewest floors (an express bank before a local one that also stops there).

**Changing cars.** When no one group serves both floors, people change cars, as at a sky lobby:
the simulator finds the route with the fewest changes (at most three), then the shortest, at
floors two groups share (never at a floor with no stops). At each change they get out, walk 15
seconds to the next group and press its button. Each ride counts as its own trip in the
passenger figures (so the people count is rides, not journeys); the trips file links a journey's
rides with its `journey` and `leg` columns, and the Metrics tab adds the wait for the next car and
the whole journey from the first button press to the final arrival. Commutes, lunch, outings to
special floors and visitors change cars when they need to; visits to another resident's (or
office) floor stay within floors one ride away, as neighbors and colleagues mostly are. Settings
won't save unless every floor is served and every residential floor can reach a Lobby or Parking /
transit floor, changing cars if need be.

On the Simulation tab, groups are drawn apart with their names above, and a shaft is dimmed
where its car doesn't stop. Settings files saved before groups existed load as one group of
every car serving every floor.

## Visitors

Settings → **Visitors** (off by default; any building) adds people from outside who come to see
one floor, like an observatory, in runs with a time of day. Set how many come a day, the floor
they enter on, the floor they go up to and the one they ride down from (the same, or a lower level
they walk down to), the opening time, busiest time, last entry and closing time, and how long a
visit lasts on average. They come in groups of 1 to 5 (2.5 on average) and leave together once the
whole group has been up there for its visit; nobody stays past closing. 30% arrive at an even rate
through opening hours and the rest around the busiest time. Their trips are `visit` and
`visit_return` in the trips file, the Simulation tab counts the visitors in the building, and the
traffic curve includes them. Every visitor needs one elevator group that serves both their
entrance and the floor they visit (and the one they leave from).

## Office buildings and lunch

**Building → Office building** swaps the rushes. Residents become the people who work here and
their floor is where they work. In the morning rush they arrive, up from the Lobby and Parking /
transit floors; in the evening rush they leave, back down through the floor they came in by.
Outside working hours they are out of the building. Outings (meetings, coffee, the mail room) are
made only by people at work: on average each person who comes in makes the set number, spread over
their own hours at work and busiest mid-morning and mid-afternoon. One that comes up while they're
busy (at lunch, say) waits until they're back. The morning share is who comes in
today; anyone who came in but isn't in the evening share works late and leaves later in the evening.
Someone away from their floor when it's time to leave goes once they're back.

**Lunch → Simulate a lunch peak** (off by default; any building) sends a share of the people who
are home, or at work, to a Lobby, Parking / transit or Retail floor (by those floors' shares)
between its start and end, mostly around its peak. They come back after about 45 minutes. Trips
are marked `lunch` and `lunch_return` in the trips file. Like the rushes, lunch only happens in
runs with a time of day. The Simulation tab has a Lunch rush run and a checkbox to switch lunch on
or off during a run (see Runs).

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
  in the early evening. Each outing is two elevator trips, there and back; commutes and lunch
  are counted separately. The setting is an average over all residents (the building makes
  residents × outings a day), so residents who are home more make more of them.
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
| **Morning rush** / **Lunch rush** / **Evening rush** | From 30 minutes before that rush starts to 30 minutes after it ends, then stops. The clock shows the time of day. Residents who have already left for the day when the run begins start out of the building. A lunch rush run has the lunch peak on even if it's off in Settings. |
| **From … to …** | Between any two times of day, with whichever rush hours fall in between, then stops. The same time twice is a full day; a stop before the start runs past midnight. |
| **Steady traffic** | The default: outings at an even rate, no time of day, no commutes. Runs until you pause it. |

Speeds run from 1× to 200× real time; **Fastest** simulates as fast as the computer allows
(the status line shows how fast that is), which suits running a whole day to look at the Metrics.
A run that reaches its stop time pauses itself; **Restart** runs it again from its start. The
slider sets outings per resident per day for the current run (raise it to stress the elevators).
During a timed run, a small curve under it shows the expected trips a minute over the whole run,
with a **Lunch peak** checkbox that switches lunch on or off for the current run (it starts as set in
Settings, or on in a lunch rush run). Switched on, the rest of the day's lunches are planned; switched
off, no more people go out, but those already out for lunch still come back.
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
(`commute_out`, `commute_in`, `outing`, `outing_return`, `lunch`, `lunch_return`, `visit`,
`visit_return`); the Metrics tab has download buttons for both.
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

**Sample data.** The Import tab has a button for each algorithm under Sample data. Each opens
that algorithm's whole day (0:00 to 23:59) averaged over 10 runs (seeds 1 to 10, the same
residents for all three) in a new browser tab, which links to the other two. They are fixed
files in `web/samples/` (`lookahead.json`, `nearest.json`, `scan.json`); one opens directly at
`/?sample=scan#import`.

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
| Rides after changing cars | Rides that start where the person changed cars, and their wait. Only when someone changed cars. |
| Whole journey when changing cars | First button press to final arrival for journeys that changed cars. |
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
  manager.py      HallCall, ElevatorManager (one per elevator group), Dispatcher interface
  dispatchers.py  NearestCarDispatcher, LookaheadDispatcher (with CallDemand and Parker), DISPATCHERS
  simulation.py   clock, people, board()/exit_car(), snapshot(), invariants
  clock.py        times of day, RunPlan (steady, a rush, or chosen hours)
  traffic.py      ResidentTraffic (residents or office workers, outings, commutes, lunch), RushHour;
                  simpler memoryless sources
  metrics.py      MetricsCollector: running totals + the full report (definitions above)
  report.py       run headless and print the report: python -m elevator_sim.report
  compare.py      compare algorithms across seeds and scenarios: python -m elevator_sim.compare
  settings.py     Settings (+ schema for the UI), validation, JSON store, make_simulation()
  presets.py      built-in presets and PresetStore (saved presets, JSON)
  server.py       web server: static files, live frames (SSE), controls, runs, settings API
web/              index.html, style.css, renderer.js (canvas), metrics.js (tables + charts),
                  settings.js, app.js
```
