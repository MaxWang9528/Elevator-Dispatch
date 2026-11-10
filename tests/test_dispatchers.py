import contextlib
import io
import unittest

from elevator_sim import (
    DISPATCHERS, Building, CallDemand, Direction, Elevator, ElevatorManager, HallCall, LookaheadDispatcher,
    NearestCarDispatcher, Parker, RunPlan, Settings, SettingsError, Simulation,
    make_dispatcher, make_simulation, parse_time,
)
from elevator_sim import compare as compare_cli
from elevator_sim.enums import CarState

DT = 0.1


def step_car(car, seconds, until=None):
    for _ in range(round(seconds / DT)):
        car.step(DT)
        if until is not None and until():
            return


def manager_with(*positions, top=37, dispatcher=None):
    cars = [Elevator(id=i, position=float(p), max_floor=top) for i, p in enumerate(positions)]
    return ElevatorManager(cars, dispatcher or NearestCarDispatcher())


class TestParkingCar(unittest.TestCase):
    def test_travels_to_park_floor_without_opening(self):
        car = Elevator(id=0, max_floor=37, park_floor=10)
        states = set()
        for _ in range(600):
            car.step(DT)
            states.add(car.state)
        self.assertEqual((car.position, car.state, car.park_floor), (10.0, CarState.IDLE, None))
        self.assertNotIn(CarState.DOORS_OPENING, states)
        self.assertAlmostEqual(car.odometer, 10.0, places=6)
        self.assertEqual(car.direction, Direction.IDLE)

    def test_a_real_stop_cancels_parking(self):
        manager = manager_with(0)
        car = manager.elevators[0]
        car.park_floor = 30
        step_car(car, 8)                       # on its way up
        self.assertTrue(car.is_parking())
        call = HallCall(20, Direction.DOWN, pressed_at=0)
        manager.hall_calls[call.key] = call
        manager.assign(call, car)
        self.assertIsNone(car.park_floor)
        step_car(car, 120, until=lambda: car.state is CarState.DOORS_OPENING)
        self.assertEqual((car.floor, car.state), (20, CarState.DOORS_OPENING))


class TestSweepTo(unittest.TestCase):
    def test_runs_to_the_end_past_its_last_stop(self):
        car = Elevator(id=0, max_floor=37)
        car.car_calls.add(10)
        car.sweep_to = 37
        opened_at = []
        for _ in range(1200):
            was = car.state
            car.step(DT)
            if was is not CarState.DOORS_OPENING and car.state is CarState.DOORS_OPENING:
                opened_at.append(car.floor)
                self.assertEqual(car.direction, Direction.UP)   # still going up at the stop
            if car.state is CarState.DOORS_OPEN:
                car.close_doors()
        self.assertEqual(opened_at, [10])                      # the end is not a stop
        self.assertEqual((car.position, car.state, car.sweep_to), (37.0, CarState.IDLE, None))


class TestScan(unittest.TestCase):
    def make(self, *positions, floors=38):
        cars = [Elevator(id=i, position=float(p)) for i, p in enumerate(positions)]
        return Simulation(Building.simple(floors), cars, make_dispatcher("scan")), cars

    def test_goes_to_the_end_before_turning(self):
        sim, (car,) = self.make(0)
        person = sim.spawn_person(origin=19, destination=0)   # waiting below the top, going down
        highest = 0.0
        while person.board_time is None and sim.time < 300:
            sim.step()
            highest = max(highest, car.position)
        self.assertEqual(highest, 37.0)                        # went all the way up first
        while person.arrive_time is None and sim.time < 600:
            sim.step()
        self.assertIsNotNone(person.arrive_time)
        sim.run(60)
        self.assertEqual((car.floor, car.state), (0, CarState.IDLE))   # rests at the bottom end

    def test_picks_up_calls_its_way_on_the_way(self):
        sim, (car,) = self.make(0)
        rider = sim.spawn_person(origin=0, destination=30)
        sim.run(5)
        on_the_way = sim.spawn_person(origin=15, destination=25)   # going up, ahead of the car
        behind = sim.spawn_person(origin=15, destination=2)        # going down: on the way back
        while on_the_way.board_time is None and sim.time < 120:
            sim.step()
        self.assertIsNone(behind.board_time)
        self.assertIsNone(rider.arrive_time)                   # picked up before the rider got off
        for _ in range(6000):
            if all(p.arrive_time is not None for p in (rider, on_the_way, behind)):
                break
            sim.step()
        self.assertGreater(behind.board_time, rider.arrive_time)
        sim.check_invariants()

    def test_the_car_that_passes_first_takes_the_call(self):
        sim, (low, high) = self.make(0, 20)
        sim.spawn_person(origin=0, destination=37)    # sends `low` sweeping up
        sim.spawn_person(origin=20, destination=37)   # sends `high` sweeping up
        sim.run(8)
        call_person = sim.spawn_person(origin=30, destination=36)
        while call_person.board_time is None and sim.time < 300:
            sim.step()
        self.assertEqual(call_person.car_id, high.id)

    def test_a_resting_car_sets_off_for_a_call(self):
        sim, (car,) = self.make(37)
        person = sim.spawn_person(origin=5, destination=0)
        while person.arrive_time is None and sim.time < 300:
            sim.step()
        self.assertIsNotNone(person.arrive_time)
        self.assertLess(person.wait_time, 60)


class TestCallDemand(unittest.TestCase):
    def test_learns_and_forgets(self):
        demand = CallDemand(10, half_life=60, prior=0.1)
        self.assertEqual(demand.probabilities(), [0.1] * 10)       # nothing seen: even
        for t in range(20):
            demand.observe(8, t)
        p = demand.probabilities()
        self.assertEqual(p.index(max(p)), 8)
        self.assertAlmostEqual(sum(p), 1)
        self.assertGreaterEqual(min(p), 0.1 / 10 - 1e-12)          # the prior keeps every floor
        for t in range(600, 605):                                  # ten half-lives later
            demand.observe(2, t)
        p = demand.probabilities()
        self.assertEqual(p.index(max(p)), 2)

    def test_survives_long_runs(self):
        demand = CallDemand(5, half_life=60)
        for t in range(0, 7 * 86400, 3600):
            demand.observe(t % 5, t)
        self.assertAlmostEqual(sum(demand.probabilities()), 1)


class TestParker(unittest.TestCase):
    def test_idle_car_goes_where_calls_come_from(self):
        manager = manager_with(0, 0)
        demand = CallDemand(38, prior=0.0)
        for t in range(30):
            demand.observe(30, t)
        parker = Parker(idle_delay=3)
        parker.step(manager, demand, 0)
        self.assertTrue(all(c.park_floor is None for c in manager.elevators))   # not idle long enough
        parker.step(manager, demand, 5)
        parked = [c.park_floor for c in manager.elevators]
        self.assertEqual(sorted(parked, key=str), [30, None])     # one goes; the other isn't needed

    def test_small_gains_are_ignored(self):
        manager = manager_with(29)
        demand = CallDemand(38, prior=0.0)
        demand.observe(30, 0)
        Parker(idle_delay=0, min_gain=5).step(manager, demand, 1)
        self.assertIsNone(manager.elevators[0].park_floor)        # one floor (3.4 s) away: not worth it
        Parker(idle_delay=0, min_gain=3).step(manager, demand, 1)
        self.assertEqual(manager.elevators[0].park_floor, 30)

    def test_spreads_cars_over_even_demand(self):
        manager = manager_with(0, 0, 0)
        Parker(idle_delay=0, min_gain=1).step(manager, CallDemand(38), 1)
        floors = sorted(c.park_floor if c.park_floor is not None else c.floor for c in manager.elevators)
        self.assertEqual(len(set(floors)), 3)
        self.assertLess(floors[0], 12)
        self.assertGreaterEqual(floors[2], 24)
        # With the default 3 s threshold, the third car isn't worth moving: it would cut the
        # expected response by less than that, and moving costs travel.
        manager = manager_with(0, 0, 0)
        Parker(idle_delay=0).step(manager, CallDemand(38), 1)
        self.assertEqual(sum(c.park_floor is not None for c in manager.elevators), 1)


class TestEta(unittest.TestCase):
    def test_idle_car(self):
        manager = manager_with(0)
        car = manager.elevators[0]
        eta, delay = LookaheadDispatcher().estimate(car, HallCall(10, Direction.DOWN, 0))
        self.assertAlmostEqual(eta, car.travel_time(10) + car.door_time, places=6)
        self.assertEqual(delay, 0)

    def test_on_the_way_versus_behind(self):
        manager = manager_with(0)
        car = manager.elevators[0]
        car.direction = Direction.UP
        car.car_calls.add(20)
        car.passengers = [object()] * 4        # only the count matters to the estimate
        d = LookaheadDispatcher()
        on_way, delay = d.estimate(car, HallCall(10, Direction.UP, 0))
        after_turn, delay_after = d.estimate(car, HallCall(10, Direction.DOWN, 0))
        self.assertLess(on_way, after_turn)
        self.assertGreater(delay, 0)            # the four riders to 20 get one more stop
        self.assertEqual(delay_after, 0)        # picked up after they are off
        self.assertGreater(after_turn, car.travel_time(20) + car.travel_time(10))

    def test_prefers_free_car_over_busy_one(self):
        manager = manager_with(5, 5, dispatcher=LookaheadDispatcher())
        busy, free = manager.elevators
        for f in (8, 12, 16):
            busy.car_calls.add(f)
        busy.direction = Direction.UP
        busy.passengers = [object()] * 6
        call = HallCall(10, Direction.UP, 0)
        self.assertIs(manager.dispatcher.assign(call, manager), free)

    def test_ties_go_to_the_car_that_has_worked_less(self):
        manager = manager_with(0, 0, dispatcher=LookaheadDispatcher())
        manager.elevators[0].odometer = 100
        self.assertIs(manager.dispatcher.assign(HallCall(9, Direction.DOWN, 0), manager), manager.elevators[1])


class TestReassign(unittest.TestCase):
    def test_moves_call_to_a_much_better_car(self):
        dispatcher = LookaheadDispatcher(margin=5)
        manager = manager_with(0, 30, dispatcher=dispatcher)
        far, near = manager.elevators
        call = HallCall(29, Direction.DOWN, 0)
        manager.hall_calls[call.key] = call
        manager.assign(call, far)                # a poor choice made earlier
        manager.step(1.0)
        self.assertEqual(call.assigned_to, near.id)
        self.assertNotIn(call.key, far.hall_stops)
        self.assertEqual(dispatcher.reassignments, 1)

    def test_keeps_call_with_car_that_is_braking_for_it(self):
        dispatcher = LookaheadDispatcher()
        manager = manager_with(0, 6, dispatcher=dispatcher)
        coming, other = manager.elevators
        call = HallCall(6, Direction.UP, 0)
        manager.hall_calls[call.key] = call
        manager.assign(call, coming)
        step_car(coming, 60, until=lambda: coming.state is CarState.MOVING
                 and abs(6 - coming.position) <= coming.braking_distance + 0.5)
        manager.step(1.0)
        self.assertEqual(call.assigned_to, coming.id)


class TestAllDispatchers(unittest.TestCase):
    PLAN = RunPlan.between(parse_time("08:15"), parse_time("08:45"))

    def run_with(self, name):
        settings = Settings.from_dict({"dispatcher": name, "warmup": 0})
        sim, traffic, _ = make_simulation(settings, self.PLAN)
        sim.run(self.PLAN.duration)
        sim.check_invariants()
        return sim

    def test_every_dispatcher_runs_and_faces_the_same_residents(self):
        runs = {name: self.run_with(name) for name in DISPATCHERS}
        first = {}
        for name, sim in runs.items():
            self.assertEqual(sim.report()["run"]["dispatcher"], name)
            self.assertGreater(sim.report()["passengers"]["delivered"], 100)
            # Outings start at the same moments, whatever the elevators do.
            first[name] = [round(p.spawn_time, 1) for p in sim.people.values()
                           if p.purpose == "outing" and p.spawn_time < 600]
        values = list(first.values())
        self.assertTrue(all(v == values[0] for v in values[1:]))

    def test_deterministic(self):
        a = self.run_with("lookahead").report()["passengers"]
        b = self.run_with("lookahead").report()["passengers"]
        self.assertEqual(a, b)

    def test_registry_and_setting(self):
        self.assertEqual(list(DISPATCHERS), ["nearest", "scan", "lookahead"])
        self.assertEqual(make_dispatcher("scan").name, "scan")
        self.assertIsInstance(make_dispatcher("lookahead"), LookaheadDispatcher)
        self.assertEqual(make_dispatcher("lookahead").name, "lookahead")
        for old in ("eta+park+reassign", "eta+park", "nearest+park"):       # saved by older versions
            self.assertEqual(Settings.from_dict({"dispatcher": old}).dispatcher, "lookahead")
            with self.assertRaises(ValueError):
                make_dispatcher(old)
        with self.assertRaises(ValueError):
            make_dispatcher("magic")
        with self.assertRaises(SettingsError) as ctx:
            Settings.from_dict({"dispatcher": "magic"})
        self.assertEqual(ctx.exception.field, "dispatcher")
        field = next(f for f in Settings.schema() if f["key"] == "dispatcher")
        self.assertEqual(field["type"], "choice")
        self.assertEqual([o["value"] for o in field["options"]], list(DISPATCHERS))

    def test_express_car_only_parks_where_it_stops(self):
        cars = [Elevator(id=0, served_floors=frozenset({0, 20, 21, 22})), Elevator(id=1)]
        sim = Simulation(Building.simple(30), cars, make_dispatcher("lookahead"))
        for t in range(10):
            sim.spawn_person(origin=12, destination=0)
            sim.run(30)
        sim.run(120)
        self.assertIn(cars[0].floor, cars[0].served_floors)


class TestCompare(unittest.TestCase):
    def test_paired_statistics(self):
        mean, half = compare_cli.paired_change([10, 12, 14], [8, 10, 12])
        self.assertEqual((mean, half), (-2, 0))
        self.assertEqual(compare_cli.verdict((mean, half), -1), "better")
        self.assertEqual(compare_cli.verdict((1.0, 2.0), -1), "no clear difference")
        self.assertEqual(compare_cli.verdict((3.0, 1.0), -1), "worse")
        self.assertEqual(compare_cli.verdict((3.0, 1.0), 1), "better")
        self.assertIsNone(compare_cli.paired_change([None], [1]))

    def test_same_algorithm_shows_no_difference(self):
        result = compare_cli.compare(["nearest", "lookahead"], ["08:20-08:30"], [1, 2], Settings())
        changes = result["results"]["08:20-08:30"]["changes"]["lookahead"]
        self.assertIn("wait_mean", changes)
        self.assertEqual(result["results"]["08:20-08:30"]["values"]["nearest"]["people"],
                         result["results"]["08:20-08:30"]["values"]["lookahead"]["people"])

        twin = compare_cli.compare(["nearest", "nearest"], ["08:20-08:30"], [1, 2], Settings())
        for key, change in twin["results"]["08:20-08:30"]["changes"]["nearest"].items():
            self.assertIn(change["verdict"], ("same", "", "-"), key)

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            compare_cli.main(["--dispatchers", "nearest,lookahead", "--runs", "08:20-08:30",
                              "--seeds", "1-2", "--jobs", "1", "--settings", "/nonexistent.json"])
        text = out.getvalue()
        self.assertIn("Average wait (s)", text)
        self.assertIn("change (lookahead)", text)
        for bad in (["--dispatchers", "nearest"], ["--dispatchers", "nearest,magic"], ["--runs", "lunch"]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                compare_cli.main([*bad, "--settings", "/nonexistent.json"])


if __name__ == "__main__":
    unittest.main()
