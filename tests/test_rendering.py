from __future__ import annotations

import unittest

from asmgcs.views.rendering import (
    VEHICLE_OUTLINE,
    build_actor_geometry,
    build_aircraft_geometry,
    build_aircraft_silhouette_path,
    build_relative_hitbox,
    build_relative_path,
)
from models import AIRPORTS, GeoReference, RenderState, WebMercatorMapper


class RenderingGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        airport = AIRPORTS["ATL"]
        self.geo = GeoReference(airport.center_lat, airport.center_lon)
        self.mapper = WebMercatorMapper(airport)

    def test_aircraft_geometry_matches_legacy_helpers(self) -> None:
        state = RenderState(
            actor_id="AC1",
            callsign="DAL123",
            latitude=33.6367,
            longitude=-84.4281,
            heading_deg=92.0,
            speed_mps=8.3,
            length_m=37.6,
            width_m=34.1,
            actor_type="aircraft",
            profile_label="A320",
        )

        legacy_center, legacy_path = build_aircraft_silhouette_path(state, self.geo, self.mapper)
        _, legacy_hitbox = build_relative_hitbox(state, self.geo, self.mapper)
        center_point, path, hitbox = build_aircraft_geometry(state, self.geo, self.mapper)

        self.assertAlmostEqual(center_point.x(), legacy_center.x(), places=6)
        self.assertAlmostEqual(center_point.y(), legacy_center.y(), places=6)
        self.assertEqual(path.elementCount(), legacy_path.elementCount())
        self.assertEqual(hitbox.count(), legacy_hitbox.count())
        self.assertEqual(path.boundingRect(), legacy_path.boundingRect())
        self.assertEqual(hitbox.boundingRect(), legacy_hitbox.boundingRect())

    def test_vehicle_geometry_matches_legacy_helpers(self) -> None:
        state = RenderState(
            actor_id="GSE-1",
            callsign="TUG-1",
            latitude=33.6371,
            longitude=-84.4276,
            heading_deg=210.0,
            speed_mps=4.0,
            length_m=8.0,
            width_m=3.6,
            actor_type="vehicle",
            profile_label="Pushback Tug",
        )

        legacy_center, legacy_path = build_relative_path(state, VEHICLE_OUTLINE, self.geo, self.mapper)
        _, legacy_hitbox = build_relative_hitbox(state, self.geo, self.mapper)
        center_point, path, hitbox = build_actor_geometry(state, VEHICLE_OUTLINE, self.geo, self.mapper)

        self.assertAlmostEqual(center_point.x(), legacy_center.x(), places=6)
        self.assertAlmostEqual(center_point.y(), legacy_center.y(), places=6)
        self.assertEqual(path.elementCount(), legacy_path.elementCount())
        self.assertEqual(hitbox.count(), legacy_hitbox.count())
        self.assertEqual(path.boundingRect(), legacy_path.boundingRect())
        self.assertEqual(hitbox.boundingRect(), legacy_hitbox.boundingRect())


if __name__ == "__main__":
    unittest.main()