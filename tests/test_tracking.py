from __future__ import annotations

import unittest

from asmgcs.domain.contracts import SurfaceObservation
from asmgcs.fusion.tracking import AlphaBetaFusionConfig, SurfaceTrackFusionModel
from models import GeoReference


class SurfaceTrackFusionModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoReference(center_lat=33.6367, center_lon=-84.4281)
        self.model = SurfaceTrackFusionModel(
            geo=self.geo,
            config=AlphaBetaFusionConfig(alpha=0.7, beta=0.2, stale_after_s=10.0),
        )

    def test_ingest_keeps_only_ground_aircraft(self) -> None:
        self.model.ingest(
            [
                SurfaceObservation("AC1", "DAL123", 33.6367, -84.4281, 90.0, 5.0, True, "A320", 37.6, 34.1, "aircraft"),
                SurfaceObservation("AC2", "UAL456", 33.6400, -84.4200, 90.0, 50.0, False, "B738", 39.5, 35.8, "aircraft"),
            ],
            observed_at_monotonic=100.0,
        )

        snapshot = self.model.snapshot(100.5)
        self.assertIn("AC1", snapshot)
        self.assertNotIn("AC2", snapshot)

    def test_snapshot_extrapolates_motion_smoothly(self) -> None:
        first = SurfaceObservation("AC1", "DAL123", 33.6367, -84.4281, 90.0, 10.0, True, "A320", 37.6, 34.1, "aircraft")
        second = SurfaceObservation("AC1", "DAL123", 33.6367, -84.4279, 90.0, 10.0, True, "A320", 37.6, 34.1, "aircraft")

        self.model.ingest([first], observed_at_monotonic=100.0)
        self.model.ingest([second], observed_at_monotonic=101.0)
        snapshot = self.model.snapshot(101.5)

        self.assertIn("AC1", snapshot)
        self.assertGreater(snapshot["AC1"].speed_mps, 0.1)
        self.assertAlmostEqual(snapshot["AC1"].heading_deg, 90.0, delta=25.0)

    def test_stale_tracks_are_pruned(self) -> None:
        observation = SurfaceObservation("AC1", "DAL123", 33.6367, -84.4281, 90.0, 5.0, True, "A320", 37.6, 34.1, "aircraft")
        self.model.ingest([observation], observed_at_monotonic=100.0)
        self.model.ingest([], observed_at_monotonic=111.0)

        snapshot = self.model.snapshot(111.0)
        self.assertNotIn("AC1", snapshot)


if __name__ == "__main__":
    unittest.main()