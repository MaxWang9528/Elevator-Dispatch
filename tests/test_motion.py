import math
import unittest

from elevator_sim import CarState, Direction, Elevator, Settings, make_simulation

DT = 0.1


def drive(car: Elevator, stops: list[int], max_seconds: float = 600) -> dict:
    """Run a lone car through `stops` as car calls; close doors as soon as they open.
    Returns time spent moving and the floors it stopped at."""
    car.max_floor = 60
    car.car_calls.update(stops)
    moving, stopped_at, t = 0.0, [], 0.0
    while (car.car_calls or car.state is not CarState.IDLE) and t < max_seconds:
        was = car.state
        car.step(DT)
        t += DT
        if was is CarState.MOVING:
            moving += DT
            if car.state is CarState.DOORS_OPENING:
                stopped_at.append(car.position)
        if car.state is CarState.DOORS_OPEN:
            car.close_doors()
    return {"moving": moving, "stops": stopped_at, "total": t}


class TestTravelTimes(unittest.TestCase):
    def test_one_floor_hop_never_reaches_top_speed(self):
        car = Elevator(id=0, speed=1.0, acceleration=0.35)
        result = drive(car, [1])
        expected = 2 * math.sqrt(1 / 0.35)            # ~3.4 s, vs 1.0 s at constant speed
        self.assertAlmostEqual(result["moving"], expected, delta=0.25)
        self.assertAlmostEqual(car.travel_time(1), expected)

    def test_long_trip_cruises(self):
        car = Elevator(id=0, speed=1.0, acceleration=0.35)
        result = drive(car, [30])
        expected = 30 / 1.0 + 1.0 / 0.35              # cruise time + time lost to the ramps
        self.assertAlmostEqual(result["moving"], expected, delta=0.25)
        self.assertEqual(result["stops"], [30.0])

    def test_many_close_stops_cost_more_than_door_time(self):
        """Ten one-floor hops spend far more time moving than one ten-floor trip."""
        hops = drive(Elevator(id=0), list(range(1, 11)))
        nonstop = drive(Elevator(id=0), [10])
        self.assertEqual(hops["stops"], [float(f) for f in range(1, 11)])
        self.assertGreater(hops["moving"], 2.4 * nonstop["moving"])


class TestBraking(unittest.TestCase):
    def test_moving_car_skips_a_stop_it_cannot_brake_for(self):
        car = Elevator(id=0, speed=1.0, acceleration=0.35)
        car.max_floor = 60
        car.car_calls.add(30)
        while car.position < 10.2:
            if car.state is CarState.DOORS_OPEN:
                car.close_doors()
            car.step(DT)
        self.assertAlmostEqual(car.velocity, 1.0)
        self.assertGreater(car.braking_distance, 1.0)
        car.hall_stops.add((11, Direction.UP))        # 0.8 floors ahead: too close
        car.hall_stops.add((15, Direction.UP))        # plenty of room
        self.assertFalse(car.can_stop_at(11, Direction.UP))
        stops = []
        for _ in range(round(120 / DT)):
            if 30 in stops:
                break
            was = car.state
            car.step(DT)
            if was is CarState.MOVING and car.state is CarState.DOORS_OPENING:
                stops.append(car.floor)
            if car.state is CarState.DOORS_OPEN:
                car.hall_stops.discard((car.floor, car.direction))  # the manager does this in a sim
                car.close_doors()
        self.assertEqual(stops[:2], [15, 30])
        self.assertIn((11, Direction.UP), car.hall_stops)  # served later, after turning around

    def test_motion_limits_hold_under_traffic(self):
        sim, _, _ = make_simulation(Settings.from_dict({"outings": 30}))
        prev = {c.id: c.velocity for c in sim.elevators}
        for _ in range(round(1200 / sim.dt)):
            sim.step()
            for c in sim.elevators:
                self.assertLessEqual(abs(c.velocity), c.speed + 1e-9)
                # On the tick a car lands on its floor, the tiny speed left after braking at the
                # limit (well under 1% of a·dt) is zeroed as it snaps to the floor.
                limit = c.acceleration * sim.dt * (1.02 if c.velocity == 0 else 1) + 1e-6
                self.assertLessEqual(abs(c.velocity - prev[c.id]), limit,
                                     f"car {c.id} changed speed too fast at t={sim.time:.1f}")
                if c.state is not CarState.MOVING:
                    self.assertEqual(c.velocity, 0.0)
                    self.assertEqual(c.position, round(c.position), "stopped between floors")
                prev[c.id] = c.velocity
        sim.check_invariants()


if __name__ == "__main__":
    unittest.main()
