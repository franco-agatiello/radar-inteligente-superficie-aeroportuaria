from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from models import GeoReference, RenderState


ActorKind = Literal["aircraft", "vehicle"]
HazardKind = Literal["fod", "wildlife"]
ConflictSeverity = Literal["advisory", "warning", "critical"]
SensorLevel = Literal["clear", "green", "yellow", "red"]
EnvelopeReferenceKind = Literal["aircraft", "vehicle", "hazard", "none"]


@dataclass(frozen=True, slots=True)
class SurfaceObservation:
    actor_id: str
    callsign: str
    latitude: float
    longitude: float
    heading_deg: float
    speed_mps: float
    on_ground: bool
    profile_label: str
    length_m: float
    width_m: float
    actor_type: ActorKind = "aircraft"


@dataclass(frozen=True, slots=True)
class SmoothedTrackState:
    actor_id: str
    callsign: str
    latitude: float
    longitude: float
    heading_deg: float
    speed_mps: float
    length_m: float
    width_m: float
    profile_label: str
    actor_type: ActorKind = "aircraft"
    source_age_s: float = 0.0

    def to_render_state(self) -> RenderState:
        return RenderState(
            actor_id=self.actor_id,
            callsign=self.callsign,
            latitude=self.latitude,
            longitude=self.longitude,
            heading_deg=self.heading_deg,
            speed_mps=self.speed_mps,
            length_m=self.length_m,
            width_m=self.width_m,
            actor_type=self.actor_type,
            profile_label=self.profile_label,
        )


@dataclass(frozen=True, slots=True)
class KinematicActorState:
    actor_id: str
    callsign: str
    latitude: float
    longitude: float
    heading_deg: float
    speed_mps: float
    length_m: float
    width_m: float
    actor_type: ActorKind
    profile_label: str

    def to_local_xy(self, geo: GeoReference) -> tuple[float, float]:
        return geo.to_local_xy(self.latitude, self.longitude)


@dataclass(frozen=True, slots=True)
class StaticHazardState:
    hazard_id: str
    latitude: float
    longitude: float
    radius_m: float
    category: str
    description: str
    hazard_type: HazardKind

    @property
    def is_wildlife(self) -> bool:
        return self.hazard_type == "wildlife"


@dataclass(frozen=True, slots=True)
class PredictedBranch:
    branch_id: str
    final_latitude: float
    final_longitude: float
    final_heading_deg: float
    local_polyline: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class SensorBands:
    green_m: float
    yellow_m: float
    red_m: float


@dataclass(frozen=True, slots=True)
class SensorEnvelopeState:
    bands: SensorBands
    level: SensorLevel
    nearest_distance_m: float | None
    nearest_label: str
    nearest_id: str
    reference_kind: EnvelopeReferenceKind
    ttc_seconds: float | None


@dataclass(frozen=True, slots=True)
class ConflictAlert:
    actor_id: str
    actor_callsign: str
    branch_id: str
    other_id: str
    other_label: str
    severity: ConflictSeverity
    threshold_m: float
    measured_distance_m: float
    ttc_seconds: float | None
    summary: str


@dataclass(frozen=True, slots=True)
class PhysicsFrameRequest:
    aircraft: tuple[KinematicActorState, ...]
    vehicles: tuple[KinematicActorState, ...]
    hazards: tuple[StaticHazardState, ...]


@dataclass(frozen=True, slots=True)
class PhysicsFrameResult:
    predictions: dict[str, tuple[PredictedBranch, ...]]
    aircraft_conflicts: frozenset[str]
    branch_conflicts: frozenset[tuple[str, int]]
    alerts: tuple[ConflictAlert, ...]
    sensor_levels: dict[str, SensorLevel] = field(default_factory=dict)
    sensor_envelopes: dict[str, SensorEnvelopeState] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "predictions": self.predictions,
            "aircraft_conflicts": tuple(sorted(self.aircraft_conflicts)),
            "branch_conflicts": tuple(sorted(self.branch_conflicts)),
            "alerts": self.alerts,
            "sensor_levels": self.sensor_levels,
            "sensor_envelopes": self.sensor_envelopes,
        }