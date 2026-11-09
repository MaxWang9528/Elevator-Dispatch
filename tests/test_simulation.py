import unittest

from elevator_sim import (
    Arrived, AtFloor, Building, CarState, Direction, Elevator, InCar, RandomTraffic, Simulation,
)


def make_sim(floors=10, cars=1, start=0.0, **kw):
    building = Building.simple(floors)
    elevators = [Elevator(id=i, position=start) for i in range(cars)]
    return Simulation(building, elevators, **kw)


def run_until(sim, cond, max_seconds=600, check=True):
    for _ in range(round(max_seconds / sim.dt)):
        if cond():
            return
        sim.step()
        if check:
            sim.check_invariants()
    raise AssertionError(f"condition not met within {max_seconds}s; summary={sim.summary()}")


class TestSinglePerson(unittest.TestCase):
    def test_picked_up_and_delivered(self):
        sim = make_sim(floors=40)
        p = sim.spawn_person(origin=29, destination=0)
        self.assertEqual(p.direction, Direction.DOWN)
        self.assertTrue(sim.manager.is_lit(29, Direction.DOWN))

        run_until(sim, lambda: isinstance(p.location, InCar))
        self.assertEqual(sim.elevators[0].floor, 29)
        self.assertFalse(sim.manager.is_lit(29, Direction.DOWN))
        self.assertIn(0, sim.elevators[0].car_calls)

        run_until(sim, lambda: isinstance(p.location, Arrived))
        self.assertEqual(p.location, Arrived(0))
        self.assertGreater(p.wait_time, 29)  # 29 floors at 1 floor/s + doors
        self.assertEqual(sim.elevators[0].car_calls, set())

    def test_person_in_exactly_one_place(self):
        sim = make_sim()
        p = sim.spawn_person(origin=3, destination=7)
        self.assertEqual(p.location, AtFloor(3))
        self.assertIn(p, sim.building[3].up_queue)
        run_until(sim, lambda: isinstance(p.location, InCar))
        self.assertNotIn(p, sim.building[3].up_queue)
        self.assertIn(p, sim.elevators[0].passengers)

    def test_same_floor_trip_rejected(self):
        with self.assertRaises(ValueError):
            make_sim().spawn_person(origin=4, destination=4)


class TestDirectionRules(unittest.TestCase):
    def test_up_car_skips_down_call_then_returns(self):
        """Car going up to 35 passes the (20, DOWN) pickup and gets it on the way back."""
        sim = make_sim(floors=40, start=5.0)
        rider = sim.spawn_person(origin=5, destination=35)
        run_until(sim, lambda: sim.elevators[0].state is CarState.MOVING)

        down_person = sim.spawn_person(origin=20, destination=2)
        car = sim.elevators[0]
        stops_seen = []
        while not isinstance(down_person.location, InCar):
            before = car.state
            sim.step()
            sim.check_invariants()
            if before is CarState.MOVING and car.state is CarState.DOORS_OPENING:
                stops_seen.append(car.floor)
        self.assertEqual(stops_seen, [35, 20])
        self.assertIsInstance(rider.location, Arrived)

    def test_people_only_board_car_going_their_way(self):
        sim = make_sim(floors=20, start=10.0)
        up = sim.spawn_person(origin=10, destination=15)
        down = sim.spawn_person(origin=10, destination=2)
        run_until(sim, lambda: isinstance(up.location, InCar))
        car = sim.elevators[0]
        self.assertEqual(car.direction, Direction.UP)
        self.assertIsInstance(down.location, AtFloor)  # still waiting
        run_until(sim, lambda: isinstance(down.location, Arrived) and isinstance(up.location, Arrived))


class TestCapacity(unittest.TestCase):
    def test_full_car_leaves_people_who_repress(self):
        building = Building.simple(10)
        sim = Simulation(building, [Elevator(id=0, capacity=3)])
        crowd = [sim.spawn_person(origin=0, destination=9) for _ in range(5)]
        run_until(sim, lambda: sim.elevators[0].state is CarState.MOVING)
        self.assertEqual(sim.elevators[0].load, 3)
        self.assertTrue(sim.manager.is_lit(0, Direction.UP))  # the 2 left behind pressed again
        run_until(sim, lambda: all(isinstance(p.location, Arrived) for p in crowd))


class TestManyPeople(unittest.TestCase):
    def test_random_traffic_everyone_eventually_arrives(self):
        building = Building.simple(30, basements=2)
        cars = [Elevator(id=i, position=2.0, capacity=12) for i in range(4)]
        traffic = RandomTraffic(per_minute=20, lobby=2, lobby_share=0.6)
        sim = Simulation(building, cars, traffic=[traffic], seed=42)
        for _ in range(round(15 * 60 / sim.dt)):  # 15 simulated minutes
            sim.step()
            sim.check_invariants()
        self.assertGreater(len(sim.people), 200)

        sim.traffic.clear()
        run_until(sim, lambda: all(isinstance(p.location, Arrived) for p in sim.people.values()),
                  max_seconds=1800)
        self.assertFalse(sim.manager.hall_calls)

    def test_same_seed_same_result(self):
        def run(seed):
            sim = make_sim(floors=20, cars=3, traffic=[RandomTraffic(per_minute=15)], seed=seed)
            sim.run(300)
            return sim.summary(), sim.snapshot()

        self.assertEqual(run(7), run(7))
        self.assertNotEqual(run(7)[1], run(8)[1])


class TestSnapshot(unittest.TestCase):
    def test_snapshot_groups_by_container(self):
        sim = make_sim(floors=10, cars=2)
        p = sim.spawn_person(origin=6, destination=1)
        snap = sim.snapshot()
        self.assertEqual(snap["floors"], [{"i": 6, "up": [], "down": [p.id]}])
        self.assertEqual(len(snap["cars"]), 2)
        layout = sim.layout()
        self.assertEqual(layout["floors"][0]["label"], "L")
        self.assertEqual(len(layout["floors"]), 10)


if __name__ == "__main__":
    unittest.main()
