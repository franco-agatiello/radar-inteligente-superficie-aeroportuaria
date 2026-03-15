from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from asmgcs.domain.contracts import SmoothedTrackState, SurfaceObservation
from models import GeoReference, clamp


def _velocity_components(speed_mps: float, heading_deg: float) -> tuple[float, float]:
    heading_rad = math.radians(heading_deg)
    velocity_x = speed_mps * math.sin(heading_rad)
    velocity_y = -speed_mps * math.cos(heading_rad)
    return velocity_x, velocity_y


def _heading_from_velocity(velocity_x: float, velocity_y: float, fallback_heading_deg: float) -> float:
    if abs(velocity_x) < 1e-6 and abs(velocity_y) < 1e-6:
        return fallback_heading_deg
    return math.degrees(math.atan2(velocity_x, -velocity_y)) % 360.0


@dataclass(frozen=True, slots=True)
class KalmanFusionConfig:
    process_accel_sigma_mps2: float = 1.8
    position_measurement_sigma_m: float = 4.5
    velocity_measurement_sigma_mps: float = 2.8
    min_dt_s: float = 0.05
    max_dt_s: float = 8.0
    stale_after_s: float = 35.0
    startup_velocity_blend: float = 0.40
    minimum_speed_for_course_mps: float = 0.35


@dataclass(frozen=True, slots=True)
class AlphaBetaFusionConfig(KalmanFusionConfig):
    alpha: float = 0.72
    beta: float = 0.18


def _predict_axis(state: tuple[float, float], covariance: tuple[tuple[float, float], tuple[float, float]], dt_s: float, accel_sigma_mps2: float) -> tuple[tuple[float, float], tuple[tuple[float, float], tuple[float, float]]]:
    position_m, velocity_mps = state
    predicted_state = (position_m + (velocity_mps * dt_s), velocity_mps)

    p00, p01 = covariance[0]
    p10, p11 = covariance[1]
    dt2 = dt_s * dt_s
    dt3 = dt2 * dt_s
    dt4 = dt2 * dt2
    q = accel_sigma_mps2 * accel_sigma_mps2
    q00 = 0.25 * dt4 * q
    q01 = 0.5 * dt3 * q
    q11 = dt2 * q

    predicted_covariance = (
        (p00 + (dt_s * (p10 + p01)) + (dt2 * p11) + q00, p01 + (dt_s * p11) + q01),
        (p10 + (dt_s * p11) + q01, p11 + q11),
    )
    return predicted_state, predicted_covariance


def _update_axis_with_position(
    state: tuple[float, float],
    covariance: tuple[tuple[float, float], tuple[float, float]],
    measurement_position_m: float,
    measurement_sigma_m: float,
) -> tuple[tuple[float, float], tuple[tuple[float, float], tuple[float, float]]]:
    residual = measurement_position_m - state[0]
    measurement_variance = measurement_sigma_m * measurement_sigma_m
    innovation = covariance[0][0] + measurement_variance
    if innovation <= 1e-9:
        return state, covariance
    gain_0 = covariance[0][0] / innovation
    gain_1 = covariance[1][0] / innovation
    updated_state = (state[0] + (gain_0 * residual), state[1] + (gain_1 * residual))
    updated_covariance = (
        ((1.0 - gain_0) * covariance[0][0], (1.0 - gain_0) * covariance[0][1]),
        (covariance[1][0] - (gain_1 * covariance[0][0]), covariance[1][1] - (gain_1 * covariance[0][1])),
    )
    return updated_state, updated_covariance


def _update_axis_with_velocity(
    state: tuple[float, float],
    covariance: tuple[tuple[float, float], tuple[float, float]],
    measurement_velocity_mps: float,
    measurement_sigma_mps: float,
) -> tuple[tuple[float, float], tuple[tuple[float, float], tuple[float, float]]]:
    residual = measurement_velocity_mps - state[1]
    measurement_variance = measurement_sigma_mps * measurement_sigma_mps
    innovation = covariance[1][1] + measurement_variance
    if innovation <= 1e-9:
        return state, covariance
    gain_0 = covariance[0][1] / innovation
    gain_1 = covariance[1][1] / innovation
    updated_state = (state[0] + (gain_0 * residual), state[1] + (gain_1 * residual))
    updated_covariance = (
        (covariance[0][0] - (gain_0 * covariance[1][0]), covariance[0][1] - (gain_0 * covariance[1][1])),
        ((1.0 - gain_1) * covariance[1][0], (1.0 - gain_1) * covariance[1][1]),
    )
    return updated_state, updated_covariance


@dataclass(slots=True)
class _TrackFilterState:
    actor_id: str
    callsign: str
    profile_label: str
    length_m: float
    width_m: float
    actor_type: str
    axis_x_state: tuple[float, float]
    axis_y_state: tuple[float, float]
    axis_x_covariance: tuple[tuple[float, float], tuple[float, float]]
    axis_y_covariance: tuple[tuple[float, float], tuple[float, float]]
    heading_deg: float
    speed_mps: float
    last_update_monotonic: float

    def predict(self, dt_s: float, accel_sigma_mps2: float) -> tuple[float, float, float, float]:
        axis_x_state, _ = _predict_axis(self.axis_x_state, self.axis_x_covariance, dt_s, accel_sigma_mps2)
        axis_y_state, _ = _predict_axis(self.axis_y_state, self.axis_y_covariance, dt_s, accel_sigma_mps2)
        return (
            axis_x_state[0],
            axis_y_state[0],
            axis_x_state[1],
            axis_y_state[1],
        )


class SurfaceTrackFusionModel:
    """Deterministic surface-only track fusion using a constant-velocity Kalman filter per actor."""

    def __init__(
        self,
        geo: GeoReference,
        config: KalmanFusionConfig | None = None,
        surface_membership_check: Callable[[float, float], bool] | None = None,
    ) -> None:
        self._geo = geo
        self._config = config or AlphaBetaFusionConfig()
        self._surface_membership_check = surface_membership_check
        self._tracks: dict[str, _TrackFilterState] = {}

    def ingest(self, observations: list[SurfaceObservation], observed_at_monotonic: float) -> None:
        incoming_ids: set[str] = set()
        for observation in observations:
            if not observation.on_ground:
                continue
            if observation.actor_type != "aircraft":
                continue
            if self._surface_membership_check is not None and not self._surface_membership_check(observation.latitude, observation.longitude):
                continue
            incoming_ids.add(observation.actor_id)
            self._update_track(observation, observed_at_monotonic)

        stale_ids = [
            actor_id
            for actor_id, track in self._tracks.items()
            if (observed_at_monotonic - track.last_update_monotonic) > self._config.stale_after_s and actor_id not in incoming_ids
        ]
        for actor_id in stale_ids:
            self._tracks.pop(actor_id, None)

    def snapshot(self, now_monotonic: float) -> dict[str, SmoothedTrackState]:
        fused: dict[str, SmoothedTrackState] = {}
        for actor_id, track in self._tracks.items():
            age_s = max(0.0, now_monotonic - track.last_update_monotonic)
            predicted_x, predicted_y, velocity_x, velocity_y = track.predict(min(age_s, self._config.max_dt_s), self._config.process_accel_sigma_mps2)
            latitude, longitude = self._geo.to_geodetic(predicted_x, predicted_y)
            speed_mps = math.hypot(velocity_x, velocity_y)
            fused[actor_id] = SmoothedTrackState(
                actor_id=track.actor_id,
                callsign=track.callsign,
                latitude=latitude,
                longitude=longitude,
                heading_deg=_heading_from_velocity(velocity_x, velocity_y, track.heading_deg if speed_mps >= self._config.minimum_speed_for_course_mps else track.heading_deg),
                speed_mps=speed_mps,
                length_m=track.length_m,
                width_m=track.width_m,
                profile_label=track.profile_label,
                actor_type="aircraft",
                source_age_s=age_s,
            )
        return fused

    def remove(self, actor_id: str) -> None:
        self._tracks.pop(actor_id, None)

    def clear(self) -> None:
        self._tracks.clear()

    def _update_track(self, observation: SurfaceObservation, observed_at_monotonic: float) -> None:
        measurement_x, measurement_y = self._geo.to_local_xy(observation.latitude, observation.longitude)
        measured_vx, measured_vy = _velocity_components(observation.speed_mps, observation.heading_deg)
        existing = self._tracks.get(observation.actor_id)

        if existing is None:
            initial_position_variance = self._config.position_measurement_sigma_m * self._config.position_measurement_sigma_m
            initial_velocity_variance = self._config.velocity_measurement_sigma_mps * self._config.velocity_measurement_sigma_mps
            self._tracks[observation.actor_id] = _TrackFilterState(
                actor_id=observation.actor_id,
                callsign=observation.callsign,
                profile_label=observation.profile_label,
                length_m=observation.length_m,
                width_m=observation.width_m,
                actor_type=observation.actor_type,
                axis_x_state=(measurement_x, measured_vx),
                axis_y_state=(measurement_y, measured_vy),
                axis_x_covariance=((initial_position_variance, 0.0), (0.0, initial_velocity_variance)),
                axis_y_covariance=((initial_position_variance, 0.0), (0.0, initial_velocity_variance)),
                heading_deg=observation.heading_deg,
                speed_mps=observation.speed_mps,
                last_update_monotonic=observed_at_monotonic,
            )
            return

        dt_s = clamp(observed_at_monotonic - existing.last_update_monotonic, self._config.min_dt_s, self._config.max_dt_s)
        predicted_x_state, predicted_x_covariance = _predict_axis(existing.axis_x_state, existing.axis_x_covariance, dt_s, self._config.process_accel_sigma_mps2)
        predicted_y_state, predicted_y_covariance = _predict_axis(existing.axis_y_state, existing.axis_y_covariance, dt_s, self._config.process_accel_sigma_mps2)

        updated_x_state, updated_x_covariance = _update_axis_with_position(
            predicted_x_state,
            predicted_x_covariance,
            measurement_x,
            self._config.position_measurement_sigma_m,
        )
        updated_y_state, updated_y_covariance = _update_axis_with_position(
            predicted_y_state,
            predicted_y_covariance,
            measurement_y,
            self._config.position_measurement_sigma_m,
        )
        updated_x_state, updated_x_covariance = _update_axis_with_velocity(
            updated_x_state,
            updated_x_covariance,
            measured_vx,
            self._config.velocity_measurement_sigma_mps,
        )
        updated_y_state, updated_y_covariance = _update_axis_with_velocity(
            updated_y_state,
            updated_y_covariance,
            measured_vy,
            self._config.velocity_measurement_sigma_mps,
        )

        blended_vx = ((1.0 - self._config.startup_velocity_blend) * updated_x_state[1]) + (self._config.startup_velocity_blend * measured_vx)
        blended_vy = ((1.0 - self._config.startup_velocity_blend) * updated_y_state[1]) + (self._config.startup_velocity_blend * measured_vy)

        existing.axis_x_state = (updated_x_state[0], blended_vx)
        existing.axis_y_state = (updated_y_state[0], blended_vy)
        existing.axis_x_covariance = updated_x_covariance
        existing.axis_y_covariance = updated_y_covariance
        existing.speed_mps = math.hypot(blended_vx, blended_vy)
        existing.heading_deg = _heading_from_velocity(
            blended_vx,
            blended_vy,
            observation.heading_deg if existing.speed_mps < self._config.minimum_speed_for_course_mps else existing.heading_deg,
        )
        existing.callsign = observation.callsign
        existing.profile_label = observation.profile_label
        existing.length_m = observation.length_m
        existing.width_m = observation.width_m
        existing.last_update_monotonic = observed_at_monotonic