from __future__ import annotations

import unittest

from asmgcs.domain.contracts import KinematicActorState, PhysicsFrameRequest, PredictedBranch, StaticHazardState
from asmgcs.physics.engine import SurfacePhysicsEngine
from models import GeoReference


class _StaticPredictor:
    def __init__(self, branches: dict[str, tuple[PredictedBranch, ...]]) -> None:
        self._branches = branches

    def project_branches(self, actor: KinematicActorState) -> tuple[PredictedBranch, ...]:
        return self._branches[actor.actor_id]


class SurfacePhysicsEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoReference(center_lat=33.6367, center_lon=-84.4281)

    def test_predictive_alert_on_hazard_intersection(self) -> None:
        aircraft = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 8.0, 37.6, 34.1, "aircraft", "A320")
        hazard = StaticHazardState("HZ1", 33.6367, -84.4280, 1.5, "FOD", "Metal Tooling", "fod")
        start_xy = self.geo.to_local_xy(aircraft.latitude, aircraft.longitude)
        hazard_xy = self.geo.to_local_xy(hazard.latitude, hazard.longitude)
        predictor = _StaticPredictor(
            {
                "AC1": (
                    PredictedBranch(
                        branch_id="B1",
                        final_latitude=hazard.latitude,
                        final_longitude=hazard.longitude,
                        final_heading_deg=90.0,
                        local_polyline=(start_xy, hazard_xy),
                    ),
                )
            }
        )
        engine = SurfacePhysicsEngine(self.geo, predictor)

        result = engine.process(PhysicsFrameRequest(aircraft=(aircraft,), vehicles=(), hazards=(hazard,)))

        self.assertIn("AC1", result.aircraft_conflicts)
        self.assertTrue(result.alerts)
        self.assertEqual(result.alerts[0].severity, "critical")
        self.assertLess(result.alerts[0].ttc_seconds or 0.0, 5.0)

    def test_predictive_alert_on_aircraft_vehicle_branch_conflict(self) -> None:
        aircraft = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 8.0, 37.6, 34.1, "aircraft", "A320")
        vehicle = KinematicActorState("GV1", "TUG1", 33.6367, -84.4280, 180.0, 4.0, 8.0, 3.5, "vehicle", "Pushback Tug")
        air_start_xy = self.geo.to_local_xy(aircraft.latitude, aircraft.longitude)
        vehicle_xy = self.geo.to_local_xy(vehicle.latitude, vehicle.longitude)
        predictor = _StaticPredictor(
            {
                "AC1": (
                    PredictedBranch("B1", vehicle.latitude, vehicle.longitude, 90.0, (air_start_xy, vehicle_xy)),
                ),
                "GV1": (
                    PredictedBranch("B1", aircraft.latitude, aircraft.longitude, 180.0, (vehicle_xy, air_start_xy)),
                ),
            }
        )
        engine = SurfacePhysicsEngine(self.geo, predictor)

        result = engine.process(PhysicsFrameRequest(aircraft=(aircraft,), vehicles=(vehicle,), hazards=()))

        self.assertIn("AC1", result.aircraft_conflicts)
        self.assertTrue(any(alert.other_id == "GV1" for alert in result.alerts))
        self.assertIn("AC1", result.sensor_levels)
        self.assertIn(result.sensor_levels["AC1"], {"green", "yellow", "red"})
        self.assertIn("AC1", result.sensor_envelopes)

    def test_aircraft_aircraft_conflicts_without_proximity_radar_envelope(self) -> None:
        aircraft_a = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 10.0, 37.6, 34.1, "aircraft", "A320")
        aircraft_b = KinematicActorState("AC2", "AAL456", 33.6367, -84.42795, 270.0, 9.0, 39.5, 35.8, "aircraft", "B738")
        air_a_xy = self.geo.to_local_xy(aircraft_a.latitude, aircraft_a.longitude)
        air_b_xy = self.geo.to_local_xy(aircraft_b.latitude, aircraft_b.longitude)
        predictor = _StaticPredictor(
            {
                "AC1": (PredictedBranch("B1", aircraft_b.latitude, aircraft_b.longitude, 90.0, (air_a_xy, air_b_xy)),),
                "AC2": (PredictedBranch("B1", aircraft_a.latitude, aircraft_a.longitude, 270.0, (air_b_xy, air_a_xy)),),
            }
        )
        engine = SurfacePhysicsEngine(self.geo, predictor)

        result = engine.process(PhysicsFrameRequest(aircraft=(aircraft_a, aircraft_b), vehicles=(), hazards=()))

        self.assertIn("AC1", result.aircraft_conflicts)
        self.assertIn("AC2", result.aircraft_conflicts)
        severities = {alert.severity for alert in result.alerts}
        self.assertIn("critical", severities)
        self.assertEqual(result.sensor_levels["AC1"], "clear")
        self.assertEqual(result.sensor_envelopes["AC1"].reference_kind, "none")
        self.assertEqual(result.sensor_envelopes["AC1"].bands.green_m, 0.0)

    def test_wildlife_generates_animal_on_runway_alert(self) -> None:
        aircraft = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 8.0, 37.6, 34.1, "aircraft", "A320")
        hazard_latitude, hazard_longitude = self.geo.to_geodetic(170.0, 0.0)
        wildlife = StaticHazardState("ANM-1", hazard_latitude, hazard_longitude, 0.8, "Wildlife", "Fox", "wildlife")
        start_xy = self.geo.to_local_xy(aircraft.latitude, aircraft.longitude)
        hazard_xy = self.geo.to_local_xy(wildlife.latitude, wildlife.longitude)
        predictor = _StaticPredictor(
            {
                "AC1": (
                    PredictedBranch("B1", wildlife.latitude, wildlife.longitude, 90.0, (start_xy, hazard_xy)),
                )
            }
        )
        engine = SurfacePhysicsEngine(self.geo, predictor)

        result = engine.process(PhysicsFrameRequest(aircraft=(aircraft,), vehicles=(), hazards=(wildlife,)))

        animal_alerts = [alert for alert in result.alerts if alert.other_id == "ANM-1"]
        self.assertTrue(animal_alerts)
        self.assertTrue(any("ANIMAL EN PISTA" in alert.summary for alert in animal_alerts))
        self.assertEqual(animal_alerts[0].severity, "advisory")

    def test_parallel_taxiing_does_not_raise_temporal_conflict(self) -> None:
        aircraft_a = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 8.0, 37.6, 34.1, "aircraft", "A320")
        aircraft_b_latitude, aircraft_b_longitude = self.geo.to_geodetic(0.0, 130.0)
        aircraft_b = KinematicActorState("AC2", "AAL456", aircraft_b_latitude, aircraft_b_longitude, 90.0, 8.0, 39.5, 35.8, "aircraft", "B738")
        a_start_xy = self.geo.to_local_xy(aircraft_a.latitude, aircraft_a.longitude)
        a_end_xy = (a_start_xy[0] + 120.0, a_start_xy[1])
        b_start_xy = self.geo.to_local_xy(aircraft_b.latitude, aircraft_b.longitude)
        b_end_xy = (b_start_xy[0] + 120.0, b_start_xy[1])
        a_end_latitude, a_end_longitude = self.geo.to_geodetic(*a_end_xy)
        b_end_latitude, b_end_longitude = self.geo.to_geodetic(*b_end_xy)
        predictor = _StaticPredictor(
            {
                "AC1": (PredictedBranch("B1", a_end_latitude, a_end_longitude, 90.0, (a_start_xy, a_end_xy)),),
                "AC2": (PredictedBranch("B1", b_end_latitude, b_end_longitude, 90.0, (b_start_xy, b_end_xy)),),
            }
        )
        engine = SurfacePhysicsEngine(self.geo, predictor)

        result = engine.process(PhysicsFrameRequest(aircraft=(aircraft_a, aircraft_b), vehicles=(), hazards=()))

        self.assertFalse(result.alerts)
        self.assertNotIn("AC1", result.aircraft_conflicts)
        self.assertNotIn("AC2", result.aircraft_conflicts)


if __name__ == "__main__":
    unittest.main()