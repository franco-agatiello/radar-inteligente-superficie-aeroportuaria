from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Polygon

from asmgcs.domain.contracts import KinematicActorState, PhysicsFrameRequest, PredictedBranch
from asmgcs.physics.engine import SurfacePhysicsEngine
from asmgcs.physics.safety_criteria import SafetyCriteriaRepository, SafetyCriteriaRule, SectorAwareSafetyCriteria
from models import GeoReference


class _StaticPredictor:
    def __init__(self, branches: dict[str, tuple[PredictedBranch, ...]]) -> None:
        self._branches = branches

    def project_branches(self, actor: KinematicActorState) -> tuple[PredictedBranch, ...]:
        return self._branches[actor.actor_id]


class SafetyCriteriaRepositoryTests(unittest.TestCase):
    def test_repository_seeds_defaults_and_prefers_sector_specific_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SafetyCriteriaRepository(Path(temp_dir) / "criteria.db")
            repository.ensure_airport_defaults("TEST")

            runway_bands = repository.resolve_bands("TEST", "runway", "aircraft", "vehicle", 8.0)
            global_bands = repository.resolve_bands("TEST", "global", "aircraft", "vehicle", 8.0)

            self.assertIsNotNone(runway_bands)
            self.assertIsNotNone(global_bands)
            assert runway_bands is not None
            assert global_bands is not None
            self.assertGreater(runway_bands.green_m, global_bands.green_m)


class SafetyCriteriaPhysicsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoReference(center_lat=33.6367, center_lon=-84.4281)

    def test_runway_takeoff_roll_uses_prediction_only_bands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SafetyCriteriaRepository(Path(temp_dir) / "criteria.db")
            repository.ensure_airport_defaults("TEST")
            safety_criteria = SectorAwareSafetyCriteria(
                airport_code="TEST",
                repository=repository,
                geo=self.geo,
                sector_areas={"runway": Polygon([(-120.0, -120.0), (120.0, -120.0), (120.0, 120.0), (-120.0, 120.0)])},
            )
            aircraft = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 40.0, 37.6, 34.1, "aircraft", "A320")
            start_xy = self.geo.to_local_xy(aircraft.latitude, aircraft.longitude)
            straight_end_xy = (start_xy[0] + 600.0, start_xy[1])
            straight_end_latitude, straight_end_longitude = self.geo.to_geodetic(*straight_end_xy)

            bands = safety_criteria.resolve_for_actor(
                aircraft,
                "vehicle",
                8.0,
                predicted_branch=PredictedBranch("B1", straight_end_latitude, straight_end_longitude, 90.0, (start_xy, straight_end_xy)),
            )

        self.assertIsNotNone(bands)
        assert bands is not None
        self.assertLessEqual(bands.green_m, 14.0)
        self.assertLessEqual(bands.yellow_m, 6.1)
        self.assertLessEqual(bands.red_m, 3.1)

    def test_runway_high_speed_turn_keeps_sector_bands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SafetyCriteriaRepository(Path(temp_dir) / "criteria.db")
            repository.ensure_airport_defaults("TEST")
            safety_criteria = SectorAwareSafetyCriteria(
                airport_code="TEST",
                repository=repository,
                geo=self.geo,
                sector_areas={"runway": Polygon([(-120.0, -120.0), (120.0, -120.0), (120.0, 120.0), (-120.0, 120.0)])},
            )
            aircraft = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 40.0, 37.6, 34.1, "aircraft", "A320")
            start_xy = self.geo.to_local_xy(aircraft.latitude, aircraft.longitude)
            turn_xy = (start_xy[0] + 150.0, start_xy[1])
            exit_xy = (turn_xy[0] + 30.0, turn_xy[1] - 120.0)
            exit_latitude, exit_longitude = self.geo.to_geodetic(*exit_xy)

            bands = safety_criteria.resolve_for_actor(
                aircraft,
                "vehicle",
                8.0,
                predicted_branch=PredictedBranch("B1", exit_latitude, exit_longitude, 166.0, (start_xy, turn_xy, exit_xy)),
            )

        self.assertIsNotNone(bands)
        assert bands is not None
        self.assertGreater(bands.green_m, 50.0)
        self.assertGreater(bands.yellow_m, 20.0)
        self.assertGreater(bands.red_m, 10.0)

    def test_physics_engine_uses_database_override_for_runway_vehicle_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SafetyCriteriaRepository(Path(temp_dir) / "criteria.db")
            repository.ensure_airport_defaults("TEST")
            existing_rule = next(
                rule
                for rule in repository.list_rules("TEST", sector_name="runway", primary_kind="aircraft", other_kind="vehicle")
                if rule.speed_min_mps <= 8.0 < rule.speed_max_mps
            )
            repository.upsert_rule(
                SafetyCriteriaRule(
                    rule_id=existing_rule.rule_id,
                    airport_code=existing_rule.airport_code,
                    sector_name=existing_rule.sector_name,
                    primary_kind=existing_rule.primary_kind,
                    other_kind=existing_rule.other_kind,
                    speed_min_mps=existing_rule.speed_min_mps,
                    speed_max_mps=existing_rule.speed_max_mps,
                    green_m=140.0,
                    yellow_m=80.0,
                    red_m=30.0,
                )
            )

            safety_criteria = SectorAwareSafetyCriteria(
                airport_code="TEST",
                repository=repository,
                geo=self.geo,
                sector_areas={"runway": Polygon([(-80.0, -80.0), (80.0, -80.0), (80.0, 80.0), (-80.0, 80.0)])},
            )
            aircraft = KinematicActorState("AC1", "DAL123", 33.6367, -84.4281, 90.0, 8.0, 37.6, 34.1, "aircraft", "A320")
            vehicle = KinematicActorState("GV1", "TUG1", 33.6367, -84.4280, 180.0, 4.0, 8.0, 3.5, "vehicle", "Pushback Tug")
            aircraft_xy = self.geo.to_local_xy(aircraft.latitude, aircraft.longitude)
            vehicle_xy = self.geo.to_local_xy(vehicle.latitude, vehicle.longitude)
            predictor = _StaticPredictor(
                {
                    "AC1": (PredictedBranch("B1", vehicle.latitude, vehicle.longitude, 90.0, (aircraft_xy, vehicle_xy)),),
                    "GV1": (PredictedBranch("B1", aircraft.latitude, aircraft.longitude, 180.0, (vehicle_xy, aircraft_xy)),),
                }
            )
            engine = SurfacePhysicsEngine(self.geo, predictor, safety_criteria=safety_criteria)

            result = engine.process(PhysicsFrameRequest(aircraft=(aircraft,), vehicles=(vehicle,), hazards=()))

            self.assertIn("AC1", result.sensor_envelopes)
            self.assertEqual(result.sensor_envelopes["AC1"].bands.green_m, 140.0)
            self.assertEqual(result.sensor_envelopes["AC1"].bands.yellow_m, 80.0)
            self.assertEqual(result.sensor_envelopes["AC1"].bands.red_m, 30.0)


if __name__ == "__main__":
    unittest.main()