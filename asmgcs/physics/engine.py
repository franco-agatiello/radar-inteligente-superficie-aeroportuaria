from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import networkx as nx
from PySide6.QtCore import QObject, Signal, Slot
from shapely.geometry import Point, Polygon

from asmgcs.domain.contracts import (
    ConflictAlert,
    KinematicActorState,
    PhysicsFrameRequest,
    PhysicsFrameResult,
    PredictedBranch,
    SensorBands,
    SensorEnvelopeState,
    StaticHazardState,
)
from asmgcs.physics.safety_criteria import SectorAwareSafetyCriteria
from gis_manager import GISManager, RoutingSegment
from models import GeoReference, ProjectedPath, clamp


CRITICAL_TTC_SECONDS = 5.0
WARNING_TTC_SECONDS = 9.0
ADVISORY_TTC_SECONDS = 14.0
WILDLIFE_ALERT_BRANCH_ID = "ANM"
MIN_RELATIVE_SPEED_SQ = 1e-6


class BranchPredictor(Protocol):
    def project_branches(self, actor: KinematicActorState) -> tuple[PredictedBranch, ...]:
        ...


@dataclass(frozen=True, slots=True)
class NetworkXBranchPredictor:
    gis_manager: GISManager
    graph: nx.DiGraph
    routing_segments: tuple[RoutingSegment, ...]
    max_branch_count: int = 12

    def project_branches(self, actor: KinematicActorState) -> tuple[PredictedBranch, ...]:
        projected = self.gis_manager.project_branches_along_graph(
            self.graph,
            list(self.routing_segments),
            actor.latitude,
            actor.longitude,
            actor.heading_deg,
            _prediction_distance_m(actor),
            speed_mps=actor.speed_mps,
            actor_type=actor.actor_type,
            actor_width_m=actor.width_m,
            max_branch_count=self.max_branch_count,
        )
        return tuple(_to_predicted_branch(index, branch) for index, branch in enumerate(projected))


@dataclass(frozen=True, slots=True)
class _TrajectorySegment:
    start_t_s: float
    end_t_s: float
    start_xy: tuple[float, float]
    end_xy: tuple[float, float]
    velocity_xy_mps: tuple[float, float]
    heading_deg: float

    def position_at(self, t_s: float) -> tuple[float, float]:
        if self.end_t_s <= self.start_t_s:
            return self.end_xy
        clamped_t = clamp(t_s, self.start_t_s, self.end_t_s)
        delta_t = clamped_t - self.start_t_s
        return (
            self.start_xy[0] + (self.velocity_xy_mps[0] * delta_t),
            self.start_xy[1] + (self.velocity_xy_mps[1] * delta_t),
        )


@dataclass(frozen=True, slots=True)
class _BranchTrajectory:
    actor: KinematicActorState
    predicted_branch: PredictedBranch
    branch_index: int
    branch_id: str
    segments: tuple[_TrajectorySegment, ...]
    horizon_s: float


@dataclass(frozen=True, slots=True)
class _CpaEvaluation:
    cpa_time_s: float
    cpa_distance_m: float


class SurfacePhysicsEngine:
    """Deterministic TTC/CPA-aware collision engine intended to run outside the UI thread."""

    def __init__(self, geo: GeoReference, predictor: BranchPredictor, safety_criteria: SectorAwareSafetyCriteria | None = None) -> None:
        self._geo = geo
        self._predictor = predictor
        self._safety_criteria = safety_criteria

    def process(self, frame: PhysicsFrameRequest) -> PhysicsFrameResult:
        predictions: dict[str, tuple[PredictedBranch, ...]] = {}
        aircraft_conflicts: set[str] = set()
        branch_conflicts: set[tuple[str, int]] = set()
        alerts: list[ConflictAlert] = []
        sensor_levels: dict[str, str] = {}
        sensor_envelopes: dict[str, SensorEnvelopeState] = {}

        all_actors = tuple(frame.aircraft) + tuple(frame.vehicles)
        actor_xy = {actor.actor_id: actor.to_local_xy(self._geo) for actor in all_actors}
        branch_states: list[_BranchTrajectory] = []

        for actor in all_actors:
            branches = self._predictor.project_branches(actor)
            predictions[actor.actor_id] = branches
            for branch_index, branch in enumerate(branches):
                branch_states.append(_build_branch_trajectory(actor, branch, branch_index, self._geo))

        for branch_state in branch_states:
            if branch_state.actor.actor_type != "aircraft":
                continue
            for hazard in frame.hazards:
                bands = _hazard_sensor_bands(branch_state.actor, hazard, self._safety_criteria, predicted_branch=branch_state.predicted_branch)
                if bands.green_m <= 0.0:
                    continue
                if not hazard.is_wildlife and not _is_forward_relevant(branch_state.actor, self._geo.to_local_xy(hazard.latitude, hazard.longitude), self._geo, bands):
                    continue
                evaluation = _evaluate_actor_hazard_cpa(branch_state, hazard, self._geo)
                severity = _breach_severity(evaluation.cpa_distance_m, bands, evaluation.cpa_time_s)
                if severity is None:
                    continue
                branch_conflicts.add((branch_state.actor.actor_id, branch_state.branch_index))
                if severity != "advisory":
                    aircraft_conflicts.add(branch_state.actor.actor_id)
                alerts.append(
                    ConflictAlert(
                        actor_id=branch_state.actor.actor_id,
                        actor_callsign=branch_state.actor.callsign,
                        branch_id=branch_state.branch_id,
                        other_id=hazard.hazard_id,
                        other_label=f"{hazard.category} {hazard.description}",
                        severity=severity,
                        threshold_m=_threshold_for_severity(bands, severity),
                        measured_distance_m=evaluation.cpa_distance_m,
                        ttc_seconds=evaluation.cpa_time_s,
                        summary=(
                            f"{branch_state.actor.callsign} {branch_state.branch_id} predicted {severity} conflict with "
                            f"{hazard.category} ({hazard.description})"
                        ),
                    )
                )

        for index, branch_state in enumerate(branch_states):
            for other_branch in branch_states[index + 1 :]:
                if branch_state.actor.actor_id == other_branch.actor.actor_id:
                    continue
                if branch_state.actor.actor_type == "vehicle" and other_branch.actor.actor_type == "vehicle":
                    continue
                bands = _pair_sensor_bands(branch_state.actor, other_branch.actor, self._safety_criteria, predicted_branch=branch_state.predicted_branch)
                evaluation = _evaluate_actor_actor_cpa(branch_state, other_branch, self._geo)
                severity = _breach_severity(evaluation.cpa_distance_m, bands, evaluation.cpa_time_s)
                if severity is None:
                    continue
                if branch_state.actor.actor_type == "aircraft":
                    branch_conflicts.add((branch_state.actor.actor_id, branch_state.branch_index))
                    if severity != "advisory":
                        aircraft_conflicts.add(branch_state.actor.actor_id)
                    alerts.append(_build_dynamic_alert(branch_state, other_branch.actor, severity, bands, evaluation))
                if other_branch.actor.actor_type == "aircraft":
                    branch_conflicts.add((other_branch.actor.actor_id, other_branch.branch_index))
                    if severity != "advisory":
                        aircraft_conflicts.add(other_branch.actor.actor_id)
                    alerts.append(_build_dynamic_alert(other_branch, branch_state.actor, severity, bands, evaluation))

        alerts.extend(_build_wildlife_alerts(frame.aircraft, frame.hazards, predictions, self._geo, self._safety_criteria))
        for alert in alerts:
            if alert.branch_id == WILDLIFE_ALERT_BRANCH_ID and alert.severity != "advisory":
                aircraft_conflicts.add(alert.actor_id)

        default_branches = {
            actor.actor_id: _build_branch_trajectory(actor, predictions[actor.actor_id][0], 0, self._geo)
            for actor in frame.aircraft
            if predictions.get(actor.actor_id)
        }
        comparison_branches = {
            actor.actor_id: _build_branch_trajectory(actor, predictions[actor.actor_id][0], 0, self._geo)
            for actor in all_actors
            if predictions.get(actor.actor_id)
        }

        for aircraft in frame.aircraft:
            envelope = _build_aircraft_sensor_envelope(
                aircraft,
                frame.aircraft,
                frame.vehicles,
                frame.hazards,
                default_branches,
                comparison_branches,
                actor_xy,
                self._geo,
                self._safety_criteria,
            )
            sensor_levels[aircraft.actor_id] = envelope.level
            sensor_envelopes[aircraft.actor_id] = envelope

        return PhysicsFrameResult(
            predictions=predictions,
            aircraft_conflicts=frozenset(aircraft_conflicts),
            branch_conflicts=frozenset(branch_conflicts),
            alerts=tuple(_deduplicate_alerts(alerts)),
            sensor_levels=sensor_levels,
            sensor_envelopes=sensor_envelopes,
        )


class PhysicsWorker(QObject):
    frame_processed = Signal(object)
    processing_failed = Signal(str)

    def __init__(self, engine: SurfacePhysicsEngine) -> None:
        super().__init__()
        self._engine = engine

    @Slot(object)
    def process_frame(self, frame: PhysicsFrameRequest) -> None:
        try:
            result = self._engine.process(frame)
        except Exception as exc:
            self.processing_failed.emit(str(exc))
            return
        self.frame_processed.emit(result.to_payload())


def _to_predicted_branch(index: int, branch: ProjectedPath) -> PredictedBranch:
    return PredictedBranch(
        branch_id=branch.branch_id or f"B{index + 1}",
        final_latitude=branch.final_latitude,
        final_longitude=branch.final_longitude,
        final_heading_deg=branch.final_heading_deg,
        local_polyline=tuple(branch.local_polyline),
    )


def _basis_vectors(heading_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    heading_rad = math.radians(heading_deg)
    right_vector = (math.cos(heading_rad), math.sin(heading_rad))
    forward_vector = (math.sin(heading_rad), -math.cos(heading_rad))
    return right_vector, forward_vector


def _heading_from_vector(velocity_xy_mps: tuple[float, float], fallback_heading_deg: float) -> float:
    if abs(velocity_xy_mps[0]) < 1e-6 and abs(velocity_xy_mps[1]) < 1e-6:
        return fallback_heading_deg
    return math.degrees(math.atan2(velocity_xy_mps[0], -velocity_xy_mps[1])) % 360.0


def _build_branch_trajectory(actor: KinematicActorState, branch: PredictedBranch, branch_index: int, geo: GeoReference) -> _BranchTrajectory:
    actor_xy = actor.to_local_xy(geo)
    raw_polyline = list(branch.local_polyline) if branch.local_polyline else [actor_xy]
    if math.hypot(raw_polyline[0][0] - actor_xy[0], raw_polyline[0][1] - actor_xy[1]) > 0.25:
        raw_polyline.insert(0, actor_xy)
    else:
        raw_polyline[0] = actor_xy

    horizon_s = _prediction_horizon_s(actor)
    if actor.speed_mps < 0.25 or len(raw_polyline) < 2:
        return _BranchTrajectory(
            actor=actor,
            predicted_branch=branch,
            branch_index=branch_index,
            branch_id=branch.branch_id,
            segments=(
                _TrajectorySegment(0.0, horizon_s, actor_xy, actor_xy, (0.0, 0.0), actor.heading_deg),
            ),
            horizon_s=horizon_s,
        )

    segments: list[_TrajectorySegment] = []
    current_time_s = 0.0
    current_xy = raw_polyline[0]
    last_heading_deg = actor.heading_deg
    for next_xy in raw_polyline[1:]:
        segment_dx = next_xy[0] - current_xy[0]
        segment_dy = next_xy[1] - current_xy[1]
        segment_length_m = math.hypot(segment_dx, segment_dy)
        if segment_length_m <= 1e-6:
            current_xy = next_xy
            continue
        segment_heading_deg = math.degrees(math.atan2(segment_dx, -segment_dy)) % 360.0
        travel_time_s = segment_length_m / max(actor.speed_mps, 0.25)
        if current_time_s + travel_time_s >= horizon_s:
            fraction = (horizon_s - current_time_s) / max(travel_time_s, 1e-6)
            final_xy = (current_xy[0] + (segment_dx * fraction), current_xy[1] + (segment_dy * fraction))
            velocity_xy = ((final_xy[0] - current_xy[0]) / max(horizon_s - current_time_s, 1e-6), (final_xy[1] - current_xy[1]) / max(horizon_s - current_time_s, 1e-6))
            segments.append(_TrajectorySegment(current_time_s, horizon_s, current_xy, final_xy, velocity_xy, segment_heading_deg))
            return _BranchTrajectory(
                actor=actor,
                predicted_branch=branch,
                branch_index=branch_index,
                branch_id=branch.branch_id,
                segments=tuple(segments),
                horizon_s=horizon_s,
            )
        velocity_xy = (segment_dx / travel_time_s, segment_dy / travel_time_s)
        segments.append(_TrajectorySegment(current_time_s, current_time_s + travel_time_s, current_xy, next_xy, velocity_xy, segment_heading_deg))
        current_time_s += travel_time_s
        current_xy = next_xy
        last_heading_deg = segment_heading_deg

    if current_time_s < horizon_s:
        segments.append(_TrajectorySegment(current_time_s, horizon_s, current_xy, current_xy, (0.0, 0.0), last_heading_deg))
    return _BranchTrajectory(
        actor=actor,
        predicted_branch=branch,
        branch_index=branch_index,
        branch_id=branch.branch_id,
        segments=tuple(segments),
        horizon_s=horizon_s,
    )


def _build_actor_polygon_xy(center_xy: tuple[float, float], length_m: float, width_m: float, heading_deg: float) -> Polygon:
    right_vector, forward_vector = _basis_vectors(heading_deg)
    half_width = width_m / 2.0
    half_length = length_m / 2.0
    corners = [
        (-half_width, -half_length),
        (half_width, -half_length),
        (half_width, half_length),
        (-half_width, half_length),
    ]
    points: list[tuple[float, float]] = []
    for local_x, local_y in corners:
        points.append(
            (
                center_xy[0] + (local_x * right_vector[0]) + (local_y * forward_vector[0]),
                center_xy[1] + (local_x * right_vector[1]) + (local_y * forward_vector[1]),
            )
        )
    return Polygon(points)


def _prediction_distance_m(actor: KinematicActorState) -> float:
    if actor.speed_mps < 0.5:
        return 0.0
    return actor.speed_mps * _prediction_horizon_s(actor)


def _prediction_horizon_s(actor: KinematicActorState) -> float:
    return 8.0 if actor.actor_type == "vehicle" else 15.0


def _legacy_dynamic_sensor_bands(actor: KinematicActorState, other_kind: str, other_speed_mps: float) -> SensorBands:
    if actor.speed_mps < 0.25:
        return SensorBands(0.0, 0.0, 0.0)
    reference_speed = max(actor.speed_mps, other_speed_mps)
    footprint_m = max(actor.length_m, actor.width_m)
    if other_kind == "aircraft":
        return SensorBands(
            green_m=max(24.0, footprint_m * 0.75) + (reference_speed * 8.0),
            yellow_m=max(14.0, footprint_m * 0.42) + (reference_speed * 5.0),
            red_m=max(7.0, footprint_m * 0.22) + (reference_speed * 3.0),
        )
    if other_kind == "vehicle":
        return SensorBands(
            green_m=max(18.0, footprint_m * 0.55) + (reference_speed * 6.0),
            yellow_m=max(10.0, footprint_m * 0.30) + (reference_speed * 3.8),
            red_m=max(5.0, footprint_m * 0.16) + (reference_speed * 2.2),
        )
    if other_kind == "wildlife":
        return SensorBands(
            green_m=max(120.0, footprint_m * 2.2) + (actor.speed_mps * 12.0),
            yellow_m=max(80.0, footprint_m * 1.6) + (actor.speed_mps * 8.0),
            red_m=max(40.0, footprint_m * 1.0) + (actor.speed_mps * 4.8),
        )
    return SensorBands(
        green_m=max(16.0, footprint_m * 0.50) + (actor.speed_mps * 5.0),
        yellow_m=max(9.0, footprint_m * 0.28) + (actor.speed_mps * 3.1),
        red_m=max(4.5, footprint_m * 0.14) + (actor.speed_mps * 1.8),
    )


def _dynamic_sensor_bands(
    actor: KinematicActorState,
    other_kind: str,
    other_speed_mps: float,
    safety_criteria: SectorAwareSafetyCriteria | None,
    predicted_branch: PredictedBranch | None = None,
) -> SensorBands:
    if safety_criteria is not None:
        resolved = safety_criteria.resolve_for_actor(actor, other_kind, other_speed_mps, predicted_branch=predicted_branch)
        if resolved is not None:
            return resolved
    return _legacy_dynamic_sensor_bands(actor, other_kind, other_speed_mps)


def _hazard_sensor_bands(
    actor: KinematicActorState,
    hazard: StaticHazardState,
    safety_criteria: SectorAwareSafetyCriteria | None,
    predicted_branch: PredictedBranch | None = None,
) -> SensorBands:
    return _dynamic_sensor_bands(actor, "wildlife" if hazard.is_wildlife else "hazard", 0.0, safety_criteria, predicted_branch=predicted_branch)


def _pair_sensor_bands(
    primary: KinematicActorState,
    secondary: KinematicActorState,
    safety_criteria: SectorAwareSafetyCriteria | None,
    predicted_branch: PredictedBranch | None = None,
) -> SensorBands:
    return _dynamic_sensor_bands(
        primary,
        "aircraft" if secondary.actor_type == "aircraft" else "vehicle",
        secondary.speed_mps,
        safety_criteria,
        predicted_branch=predicted_branch,
    )


def _breach_severity(distance_m: float, bands: SensorBands, ttc_seconds: float | None) -> str | None:
    if bands.green_m <= 0.0 or ttc_seconds is None:
        return None
    if distance_m <= bands.red_m and ttc_seconds <= CRITICAL_TTC_SECONDS:
        return "critical"
    if distance_m <= bands.yellow_m and ttc_seconds <= WARNING_TTC_SECONDS:
        return "warning"
    if distance_m <= bands.green_m and ttc_seconds <= ADVISORY_TTC_SECONDS:
        return "advisory"
    return None


def _sensor_level(distance_m: float | None, bands: SensorBands, ttc_seconds: float | None) -> str:
    if distance_m is None:
        return "clear"
    severity = _breach_severity(distance_m, bands, ttc_seconds)
    if severity == "critical":
        return "red"
    if severity == "warning":
        return "yellow"
    if severity == "advisory":
        return "green"
    return "clear"


def _evaluate_actor_actor_cpa(primary: _BranchTrajectory, secondary: _BranchTrajectory, geo: GeoReference) -> _CpaEvaluation:
    best: _CpaEvaluation | None = None
    for primary_segment in primary.segments:
        for secondary_segment in secondary.segments:
            overlap_start = max(primary_segment.start_t_s, secondary_segment.start_t_s)
            overlap_end = min(primary_segment.end_t_s, secondary_segment.end_t_s)
            if overlap_end < overlap_start:
                continue
            evaluation = _evaluate_segment_overlap(
                primary_segment,
                secondary_segment,
                overlap_start,
                overlap_end,
                lambda position_a, position_b: _build_actor_polygon_xy(position_a, primary.actor.length_m, primary.actor.width_m, primary_segment.heading_deg).distance(
                    _build_actor_polygon_xy(position_b, secondary.actor.length_m, secondary.actor.width_m, secondary_segment.heading_deg)
                ),
            )
            if best is None or evaluation.cpa_distance_m < best.cpa_distance_m or (
                math.isclose(evaluation.cpa_distance_m, best.cpa_distance_m, abs_tol=0.01) and evaluation.cpa_time_s < best.cpa_time_s
            ):
                best = evaluation
    if best is not None:
        return best
    current_distance = _build_actor_polygon_xy(primary.actor.to_local_xy(geo), primary.actor.length_m, primary.actor.width_m, primary.actor.heading_deg).distance(
        _build_actor_polygon_xy(secondary.actor.to_local_xy(geo), secondary.actor.length_m, secondary.actor.width_m, secondary.actor.heading_deg)
    )
    return _CpaEvaluation(0.0, current_distance)


def _evaluate_actor_hazard_cpa(primary: _BranchTrajectory, hazard: StaticHazardState, geo: GeoReference) -> _CpaEvaluation:
    hazard_xy = geo.to_local_xy(hazard.latitude, hazard.longitude)
    hazard_polygon = Point(hazard_xy).buffer(max(hazard.radius_m, 0.2))
    best: _CpaEvaluation | None = None
    for segment in primary.segments:
        evaluation = _evaluate_segment_overlap(
            segment,
            _TrajectorySegment(0.0, primary.horizon_s, hazard_xy, hazard_xy, (0.0, 0.0), primary.actor.heading_deg),
            max(0.0, segment.start_t_s),
            min(segment.end_t_s, primary.horizon_s),
            lambda position_a, _: _build_actor_polygon_xy(position_a, primary.actor.length_m, primary.actor.width_m, segment.heading_deg).distance(hazard_polygon),
        )
        if best is None or evaluation.cpa_distance_m < best.cpa_distance_m or (
            math.isclose(evaluation.cpa_distance_m, best.cpa_distance_m, abs_tol=0.01) and evaluation.cpa_time_s < best.cpa_time_s
        ):
            best = evaluation
    assert best is not None
    return best


def _evaluate_segment_overlap(
    primary_segment: _TrajectorySegment,
    secondary_segment: _TrajectorySegment,
    overlap_start_s: float,
    overlap_end_s: float,
    distance_builder,
) -> _CpaEvaluation:
    if overlap_end_s < overlap_start_s:
        overlap_end_s = overlap_start_s
    primary_position = primary_segment.position_at(overlap_start_s)
    secondary_position = secondary_segment.position_at(overlap_start_s)
    relative_position = (
        secondary_position[0] - primary_position[0],
        secondary_position[1] - primary_position[1],
    )
    relative_velocity = (
        secondary_segment.velocity_xy_mps[0] - primary_segment.velocity_xy_mps[0],
        secondary_segment.velocity_xy_mps[1] - primary_segment.velocity_xy_mps[1],
    )
    relative_speed_sq = (relative_velocity[0] * relative_velocity[0]) + (relative_velocity[1] * relative_velocity[1])
    window_s = max(0.0, overlap_end_s - overlap_start_s)
    if relative_speed_sq <= MIN_RELATIVE_SPEED_SQ:
        local_cpa_s = 0.0
    else:
        local_cpa_s = clamp(-((relative_position[0] * relative_velocity[0]) + (relative_position[1] * relative_velocity[1])) / relative_speed_sq, 0.0, window_s)
    global_cpa_s = overlap_start_s + local_cpa_s
    cpa_position_a = primary_segment.position_at(global_cpa_s)
    cpa_position_b = secondary_segment.position_at(global_cpa_s)
    return _CpaEvaluation(global_cpa_s, distance_builder(cpa_position_a, cpa_position_b))


def _forward_metrics(primary: KinematicActorState, target_xy: tuple[float, float], geo: GeoReference) -> tuple[float, float, float]:
    primary_xy = primary.to_local_xy(geo)
    rel_x = target_xy[0] - primary_xy[0]
    rel_y = target_xy[1] - primary_xy[1]
    right_vector, forward_vector = _basis_vectors(primary.heading_deg)
    forward_distance = (rel_x * forward_vector[0]) + (rel_y * forward_vector[1])
    lateral_distance = abs((rel_x * right_vector[0]) + (rel_y * right_vector[1]))
    return forward_distance, lateral_distance, math.hypot(rel_x, rel_y)


def _is_forward_relevant(primary: KinematicActorState, target_xy: tuple[float, float], geo: GeoReference, bands: SensorBands) -> bool:
    forward_distance, lateral_distance, radial_distance = _forward_metrics(primary, target_xy, geo)
    immediate_radius_m = max(primary.length_m * 0.75, bands.red_m + 2.0)
    forward_range_m = max(bands.green_m * 1.15, immediate_radius_m)
    lateral_gate_m = max(primary.width_m * 2.0, bands.green_m * 0.42)
    return radial_distance <= immediate_radius_m or (0.0 <= forward_distance <= forward_range_m and lateral_distance <= lateral_gate_m)


def _default_sensor_bands(
    primary: KinematicActorState,
    aircraft: tuple[KinematicActorState, ...],
    vehicles: tuple[KinematicActorState, ...],
    hazards: tuple[StaticHazardState, ...],
    safety_criteria: SectorAwareSafetyCriteria | None,
) -> tuple[SensorBands, str]:
    if len(vehicles) > 0:
        return _dynamic_sensor_bands(primary, "vehicle", 0.0, safety_criteria), "vehicle"
    if any(hazard.is_wildlife for hazard in hazards):
        return _dynamic_sensor_bands(primary, "wildlife", 0.0, safety_criteria), "hazard"
    if hazards:
        return _dynamic_sensor_bands(primary, "hazard", 0.0, safety_criteria), "hazard"
    return SensorBands(0.0, 0.0, 0.0), "none"


def _build_aircraft_sensor_envelope(
    primary: KinematicActorState,
    aircraft: tuple[KinematicActorState, ...],
    vehicles: tuple[KinematicActorState, ...],
    hazards: tuple[StaticHazardState, ...],
    default_branches: dict[str, _BranchTrajectory],
    comparison_branches: dict[str, _BranchTrajectory],
    actor_xy: dict[str, tuple[float, float]],
    geo: GeoReference,
    safety_criteria: SectorAwareSafetyCriteria | None,
) -> SensorEnvelopeState:
    default_bands, default_kind = _default_sensor_bands(primary, aircraft, vehicles, hazards, safety_criteria)
    primary_branch = default_branches.get(primary.actor_id)
    if primary_branch is None:
        return SensorEnvelopeState(default_bands, "clear", None, "forward path clear", "", default_kind, None)

    best_candidate: tuple[int, float, float, SensorBands, str, str, str, float | None] | None = None
    for vehicle in vehicles:
        comparison = comparison_branches.get(vehicle.actor_id)
        if comparison is None:
            continue
        bands = _dynamic_sensor_bands(primary, "vehicle", vehicle.speed_mps, safety_criteria, predicted_branch=primary_branch.predicted_branch)
        if bands.green_m <= 0.0 or not _is_forward_relevant(primary, actor_xy[vehicle.actor_id], geo, bands):
            continue
        evaluation = _evaluate_actor_actor_cpa(primary_branch, comparison, geo)
        level = _sensor_level(evaluation.cpa_distance_m, bands, evaluation.cpa_time_s)
        candidate = (_level_rank(level), -(evaluation.cpa_time_s if evaluation.cpa_time_s is not None else 9999.0), -evaluation.cpa_distance_m, bands, vehicle.callsign, vehicle.actor_id, "vehicle", evaluation.cpa_time_s)
        if best_candidate is None or candidate[:3] > best_candidate[:3]:
            best_candidate = candidate

    for hazard in hazards:
        bands = _hazard_sensor_bands(primary, hazard, safety_criteria, predicted_branch=primary_branch.predicted_branch)
        if bands.green_m <= 0.0:
            continue
        target_xy = geo.to_local_xy(hazard.latitude, hazard.longitude)
        if not hazard.is_wildlife and not _is_forward_relevant(primary, target_xy, geo, bands):
            continue
        evaluation = _evaluate_actor_hazard_cpa(primary_branch, hazard, geo)
        level = _sensor_level(evaluation.cpa_distance_m, bands, evaluation.cpa_time_s)
        candidate = (_level_rank(level), -(evaluation.cpa_time_s if evaluation.cpa_time_s is not None else 9999.0), -evaluation.cpa_distance_m, bands, f"{hazard.category} {hazard.description}", hazard.hazard_id, "hazard", evaluation.cpa_time_s)
        if best_candidate is None or candidate[:3] > best_candidate[:3]:
            best_candidate = candidate

    if best_candidate is None:
        return SensorEnvelopeState(default_bands, "clear", None, "forward path clear", "", default_kind, None)

    _, _, distance_key, bands, nearest_label, nearest_id, reference_kind, ttc_seconds = best_candidate
    nearest_distance_m = -distance_key
    return SensorEnvelopeState(
        bands=bands,
        level=_sensor_level(nearest_distance_m, bands, ttc_seconds),
        nearest_distance_m=nearest_distance_m,
        nearest_label=nearest_label,
        nearest_id=nearest_id,
        reference_kind=reference_kind,
        ttc_seconds=ttc_seconds,
    )


def _level_rank(level: str) -> int:
    return {"clear": 0, "green": 1, "yellow": 2, "red": 3}.get(level, 0)


def _build_dynamic_alert(
    branch_state: _BranchTrajectory,
    other_actor: KinematicActorState,
    severity: str,
    bands: SensorBands,
    evaluation: _CpaEvaluation,
) -> ConflictAlert:
    return ConflictAlert(
        actor_id=branch_state.actor.actor_id,
        actor_callsign=branch_state.actor.callsign,
        branch_id=branch_state.branch_id,
        other_id=other_actor.actor_id,
        other_label=other_actor.callsign,
        severity=severity,
        threshold_m=_threshold_for_severity(bands, severity),
        measured_distance_m=evaluation.cpa_distance_m,
        ttc_seconds=evaluation.cpa_time_s,
        summary=f"{branch_state.actor.callsign} {branch_state.branch_id} predicted {severity} conflict with {other_actor.callsign}",
    )


def _threshold_for_severity(bands: SensorBands, severity: str) -> float:
    if severity == "critical":
        return bands.red_m
    if severity == "warning":
        return bands.yellow_m
    return bands.green_m


def _build_wildlife_alerts(
    aircraft: tuple[KinematicActorState, ...],
    hazards: tuple[StaticHazardState, ...],
    predictions: dict[str, tuple[PredictedBranch, ...]],
    geo: GeoReference,
    safety_criteria: SectorAwareSafetyCriteria | None,
) -> list[ConflictAlert]:
    alerts: list[ConflictAlert] = []
    for aircraft_state in aircraft:
        branches = predictions.get(aircraft_state.actor_id)
        if not branches:
            continue
        primary_branch = _build_branch_trajectory(aircraft_state, branches[0], 0, geo)
        for hazard in hazards:
            if not hazard.is_wildlife:
                continue
            bands = _hazard_sensor_bands(aircraft_state, hazard, safety_criteria, predicted_branch=primary_branch.predicted_branch)
            evaluation = _evaluate_actor_hazard_cpa(primary_branch, hazard, geo)
            severity = _wildlife_breach_severity(evaluation.cpa_distance_m, bands, evaluation.cpa_time_s)
            if severity is None:
                continue
            alerts.append(
                ConflictAlert(
                    actor_id=aircraft_state.actor_id,
                    actor_callsign=aircraft_state.callsign,
                    branch_id=WILDLIFE_ALERT_BRANCH_ID,
                    other_id=hazard.hazard_id,
                    other_label=f"Animal en pista ({hazard.description})",
                    severity=severity,
                    threshold_m=_threshold_for_severity(bands, severity),
                    measured_distance_m=evaluation.cpa_distance_m,
                    ttc_seconds=evaluation.cpa_time_s,
                    summary=(
                        f"ANIMAL EN PISTA | {aircraft_state.callsign} dentro de zona {_severity_color_label(severity)} "
                        f"respecto de {hazard.description}"
                    ),
                )
            )
    return alerts


def _severity_color_label(severity: str) -> str:
    return {"advisory": "VERDE", "warning": "AMARILLA", "critical": "ROJA"}.get(severity, severity.upper())


def _wildlife_breach_severity(distance_m: float, bands: SensorBands, ttc_seconds: float | None) -> str | None:
    if bands.green_m <= 0.0 or ttc_seconds is None:
        return None
    if distance_m <= bands.red_m and ttc_seconds <= 8.0:
        return "critical"
    if distance_m <= bands.yellow_m and ttc_seconds <= 12.0:
        return "warning"
    if distance_m <= bands.green_m and ttc_seconds <= 30.0:
        return "advisory"
    return None


def _deduplicate_alerts(alerts: list[ConflictAlert]) -> list[ConflictAlert]:
    unique: dict[tuple[str, str, str, str], ConflictAlert] = {}
    for alert in alerts:
        key = (alert.actor_id, alert.branch_id, alert.other_id, alert.severity)
        existing = unique.get(key)
        if existing is None or alert.measured_distance_m < existing.measured_distance_m:
            unique[key] = alert
    return sorted(
        unique.values(),
        key=lambda entry: (_severity_rank(entry.severity), entry.ttc_seconds if entry.ttc_seconds is not None else 9999.0, entry.measured_distance_m),
    )


def _severity_rank(severity: str) -> int:
    return {"critical": 0, "warning": 1, "advisory": 2}.get(severity, 3)
