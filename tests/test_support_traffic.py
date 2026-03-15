from __future__ import annotations

from types import SimpleNamespace
import unittest

from shapely.geometry import Point, Polygon

from asmgcs.app.support_traffic import GROUND_VEHICLE_COUNT, FOD_COUNT, SupportTrafficService, WILDLIFE_COUNT


class _FakeGeoReference:
    def to_local_xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        return latitude, longitude

    def to_geodetic(self, local_x: float, local_y: float) -> tuple[float, float]:
        return local_x, local_y


class _FakeGISManager:
    def sample_point_within_drivable_area(self, drivable_area, rng):
        x = rng.uniform(-10.0, 10.0)
        y = rng.uniform(-10.0, 10.0)
        return x, y, 0.0, 0.0

    def sample_graph_position(self, routing_segments, rng):
        x = rng.uniform(-5.0, 5.0)
        y = rng.uniform(-5.0, 5.0)
        return x, y, 0.0, 0.0, rng.uniform(0.0, 360.0)

    def project_along_graph(self, graph, routing_segments, latitude, longitude, heading_deg, distance_m, rng):
        return SimpleNamespace(final_latitude=latitude + 0.1, final_longitude=longitude + 0.1, final_heading_deg=heading_deg)


class SupportTrafficServiceTests(unittest.TestCase):
    def test_activate_airport_spawns_expected_counts(self) -> None:
        service = SupportTrafficService(seed=7)
        apron = Polygon([(-20, -20), (20, -20), (20, 20), (-20, 20)])
        service.activate_airport(
            _FakeGeoReference(),
            _FakeGISManager(),
            SimpleNamespace(drivable_area=apron, routing_segments=[], graph=None, sector_areas={"apron": apron}),
        )

        self.assertEqual(len(service.obstacles), FOD_COUNT + WILDLIFE_COUNT)
        self.assertEqual(len(service.vehicles), GROUND_VEHICLE_COUNT)
        self.assertEqual(sum(1 for obstacle in service.obstacles if obstacle.is_wildlife), 1)

    def test_update_moves_vehicle_and_keeps_wildlife_inside_area(self) -> None:
        service = SupportTrafficService(seed=7)
        drivable_area = Polygon([(-20, -20), (20, -20), (20, 20), (-20, 20)])
        service.activate_airport(
            _FakeGeoReference(),
            _FakeGISManager(),
            SimpleNamespace(drivable_area=drivable_area, routing_segments=[], graph=None, sector_areas={"apron": drivable_area}),
        )
        initial_vehicle_position = (service.vehicles[0].latitude, service.vehicles[0].longitude)

        service.update(1.0 / 30.0)

        updated_vehicle_position = (service.vehicles[0].latitude, service.vehicles[0].longitude)
        self.assertNotEqual(initial_vehicle_position, updated_vehicle_position)
        for obstacle in service.obstacles:
            if obstacle.is_wildlife:
                self.assertTrue(drivable_area.contains(Point(obstacle.latitude, obstacle.longitude)))

    def test_clear_resets_state(self) -> None:
        service = SupportTrafficService(seed=7)
        apron = Polygon([(-20, -20), (20, -20), (20, 20), (-20, 20)])
        service.activate_airport(
            _FakeGeoReference(),
            _FakeGISManager(),
            SimpleNamespace(drivable_area=apron, routing_segments=[], graph=None, sector_areas={"apron": apron}),
        )

        service.clear()

        self.assertEqual(service.obstacles, [])
        self.assertEqual(service.vehicles, [])

    def test_vehicle_modes_assign_apron_service_to_apron(self) -> None:
        service = SupportTrafficService(seed=7)
        service._apron_area = Polygon([(-20, -20), (20, -20), (20, 20), (-20, 20)])
        service._taxiway_segments = []

        self.assertEqual(service._vehicle_mode_for_spec("Fuel Truck"), "apron")
        self.assertEqual(service._vehicle_mode_for_spec("Pushback Tug"), "apron")
        self.assertEqual(service._vehicle_mode_for_spec("Baggage Cart"), "apron")

    def test_vehicle_modes_assign_follow_me_to_taxiway_when_available(self) -> None:
        service = SupportTrafficService(seed=7)
        service._apron_area = Polygon([(-20, -20), (20, -20), (20, 20), (-20, 20)])
        service._taxiway_segments = [object()]

        self.assertEqual(service._vehicle_mode_for_spec("Follow-Me"), "taxiway")


if __name__ == "__main__":
    unittest.main()