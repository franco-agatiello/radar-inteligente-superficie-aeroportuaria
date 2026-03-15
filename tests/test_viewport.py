from __future__ import annotations

import unittest

from asmgcs.views.viewport import ViewportFrameState, is_ground_contact_relevant_to_focus, is_obstacle_relevant_to_focus
from models import GeoReference, RenderState


class ViewportRelevanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoReference(center_lat=33.6367, center_lon=-84.4281)
        self.focus = RenderState(
            actor_id="AC1",
            callsign="DAL123",
            latitude=33.6367,
            longitude=-84.4281,
            heading_deg=90.0,
            speed_mps=8.0,
            length_m=37.6,
            width_m=34.1,
            actor_type="aircraft",
            profile_label="A320",
        )
        self.viewport = ViewportFrameState(
            mode="cockpit",
            focus_actor_id="AC1",
            target_zoom=3.15,
            target_rotation_deg=-90.0,
            culling_radius_m=60.0,
            forward_range_m=110.0,
            forward_half_angle_deg=46.0,
        )

    def test_cockpit_keeps_obstacle_visible_when_close_behind(self) -> None:
        behind_latitude, behind_longitude = self.geo.to_geodetic(-35.0, 0.0)

        visible = is_obstacle_relevant_to_focus(
            self.viewport,
            self.geo,
            self.focus,
            behind_latitude,
            behind_longitude,
            2.0,
        )

        self.assertTrue(visible)

    def test_cockpit_rejects_far_obstacle_outside_awareness_bubble(self) -> None:
        far_latitude, far_longitude = self.geo.to_geodetic(-260.0, 0.0)

        visible = is_obstacle_relevant_to_focus(
            self.viewport,
            self.geo,
            self.focus,
            far_latitude,
            far_longitude,
            2.0,
        )

        self.assertFalse(visible)

    def test_cockpit_keeps_nearby_vehicle_visible_off_axis(self) -> None:
        nearby_vehicle = RenderState(
            actor_id="GV1",
            callsign="TUG1",
            latitude=self.geo.to_geodetic(-95.0, 65.0)[0],
            longitude=self.geo.to_geodetic(-95.0, 65.0)[1],
            heading_deg=180.0,
            speed_mps=3.0,
            length_m=8.0,
            width_m=3.5,
            actor_type="vehicle",
            profile_label="Pushback Tug",
        )

        visible = is_ground_contact_relevant_to_focus(self.viewport, self.geo, self.focus, nearby_vehicle)

        self.assertTrue(visible)


if __name__ == "__main__":
    unittest.main()