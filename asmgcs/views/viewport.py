from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from asmgcs.domain.contracts import SensorEnvelopeState
from asmgcs.views.rendering import basis_vectors
from models import GeoReference, RenderState


ViewportMode = Literal["tower", "cockpit"]


@dataclass(frozen=True, slots=True)
class ViewportFrameState:
    mode: ViewportMode
    focus_actor_id: str | None
    target_zoom: float
    target_rotation_deg: float
    culling_radius_m: float
    forward_range_m: float
    forward_half_angle_deg: float


class ViewportStateMachine:
    def __init__(self) -> None:
        self._mode: ViewportMode = "tower"
        self._focus_actor_id: str | None = None

    @property
    def mode(self) -> ViewportMode:
        return self._mode

    @property
    def focus_actor_id(self) -> str | None:
        return self._focus_actor_id

    def enter_tower_mode(self) -> None:
        self._mode = "tower"
        self._focus_actor_id = None

    def enter_cockpit_mode(self, actor_id: str) -> None:
        self._mode = "cockpit"
        self._focus_actor_id = actor_id

    def build_frame_state(
        self,
        aircraft_states: dict[str, RenderState],
        sensor_envelopes: dict[str, SensorEnvelopeState],
    ) -> ViewportFrameState:
        if self._mode != "cockpit" or self._focus_actor_id not in aircraft_states:
            return ViewportFrameState(
                mode="tower",
                focus_actor_id=None,
                target_zoom=1.0,
                target_rotation_deg=0.0,
                culling_radius_m=0.0,
                forward_range_m=0.0,
                forward_half_angle_deg=0.0,
            )
        focused_state = aircraft_states[self._focus_actor_id]
        envelope = sensor_envelopes.get(self._focus_actor_id)
        forward_range_m = max(envelope.bands.green_m * 1.25 if envelope is not None else 90.0, 90.0)
        culling_radius_m = max(forward_range_m * 0.7, 55.0)
        return ViewportFrameState(
            mode="cockpit",
            focus_actor_id=self._focus_actor_id,
            target_zoom=3.15,
            target_rotation_deg=-focused_state.heading_deg,
            culling_radius_m=culling_radius_m,
            forward_range_m=forward_range_m,
            forward_half_angle_deg=46.0,
        )


def is_relevant_to_focus(
    viewport_state: ViewportFrameState,
    geo: GeoReference,
    focus_state: RenderState,
    candidate_state: RenderState,
) -> bool:
    if viewport_state.mode != "cockpit" or viewport_state.focus_actor_id is None:
        return True
    if candidate_state.actor_id == viewport_state.focus_actor_id:
        return True
    focus_xy = geo.to_local_xy(focus_state.latitude, focus_state.longitude)
    candidate_xy = geo.to_local_xy(candidate_state.latitude, candidate_state.longitude)
    rel_x = candidate_xy[0] - focus_xy[0]
    rel_y = candidate_xy[1] - focus_xy[1]
    right_vector, forward_vector = basis_vectors(focus_state.heading_deg)
    forward_distance_m = (rel_x * forward_vector[0]) + (rel_y * forward_vector[1])
    lateral_distance_m = abs((rel_x * right_vector[0]) + (rel_y * right_vector[1]))
    radial_distance_m = (rel_x ** 2 + rel_y ** 2) ** 0.5
    if radial_distance_m <= viewport_state.culling_radius_m:
        return True
    if forward_distance_m < 0.0 or forward_distance_m > viewport_state.forward_range_m:
        return False
    lateral_limit_m = max(focus_state.width_m * 2.4, viewport_state.forward_range_m * 0.42)
    return lateral_distance_m <= lateral_limit_m


def _surface_awareness_radius_m(
    viewport_state: ViewportFrameState,
    focus_state: RenderState,
    contact_size_m: float,
) -> float:
    return max(
        viewport_state.forward_range_m * 1.9,
        viewport_state.culling_radius_m * 2.4,
        focus_state.length_m + focus_state.width_m + contact_size_m + 72.0,
    )


def is_ground_contact_relevant_to_focus(
    viewport_state: ViewportFrameState,
    geo: GeoReference,
    focus_state: RenderState,
    candidate_state: RenderState,
) -> bool:
    if viewport_state.mode != "cockpit" or viewport_state.focus_actor_id is None:
        return True
    focus_xy = geo.to_local_xy(focus_state.latitude, focus_state.longitude)
    candidate_xy = geo.to_local_xy(candidate_state.latitude, candidate_state.longitude)
    radial_distance_m = ((candidate_xy[0] - focus_xy[0]) ** 2 + (candidate_xy[1] - focus_xy[1]) ** 2) ** 0.5
    awareness_radius_m = _surface_awareness_radius_m(
        viewport_state,
        focus_state,
        max(candidate_state.length_m, candidate_state.width_m),
    )
    if radial_distance_m <= awareness_radius_m:
        return True
    return is_relevant_to_focus(viewport_state, geo, focus_state, candidate_state)


def is_obstacle_relevant_to_focus(
    viewport_state: ViewportFrameState,
    geo: GeoReference,
    focus_state: RenderState,
    obstacle_latitude: float,
    obstacle_longitude: float,
    obstacle_radius_m: float,
) -> bool:
    if viewport_state.mode != "cockpit" or viewport_state.focus_actor_id is None:
        return True
    focus_xy = geo.to_local_xy(focus_state.latitude, focus_state.longitude)
    obstacle_xy = geo.to_local_xy(obstacle_latitude, obstacle_longitude)
    radial_distance_m = ((obstacle_xy[0] - focus_xy[0]) ** 2 + (obstacle_xy[1] - focus_xy[1]) ** 2) ** 0.5
    awareness_radius_m = _surface_awareness_radius_m(viewport_state, focus_state, obstacle_radius_m * 2.0)
    if radial_distance_m <= awareness_radius_m:
        return True
    obstacle_state = RenderState(
        actor_id="obstacle",
        callsign="obstacle",
        latitude=obstacle_latitude,
        longitude=obstacle_longitude,
        heading_deg=focus_state.heading_deg,
        speed_mps=0.0,
        length_m=max(obstacle_radius_m * 2.0, 1.0),
        width_m=max(obstacle_radius_m * 2.0, 1.0),
        actor_type="vehicle",
        profile_label="obstacle",
    )
    return is_relevant_to_focus(viewport_state, geo, focus_state, obstacle_state)


__all__ = [
    "ViewportFrameState",
    "ViewportMode",
    "is_ground_contact_relevant_to_focus",
    "ViewportStateMachine",
    "is_obstacle_relevant_to_focus",
    "is_relevant_to_focus",
]