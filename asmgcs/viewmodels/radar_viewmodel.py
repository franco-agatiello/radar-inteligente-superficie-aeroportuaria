from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from asmgcs.domain.contracts import (
    KinematicActorState,
    PhysicsFrameRequest,
    SensorEnvelopeState,
    SmoothedTrackState,
    StaticHazardState,
)
from asmgcs.fusion.telemetry import TelemetryIngestBatch, snapshot_to_surface_batch
from asmgcs.fusion.tracking import SurfaceTrackFusionModel
from asmgcs.physics.engine import SurfacePhysicsEngine
from models import GroundVehicleState, StaticObstacle, TelemetrySnapshot


LOGIC_TICK_HZ = 10.0


@dataclass(frozen=True, slots=True)
class RadarViewState:
    tracks: dict[str, SmoothedTrackState]
    predictions: dict[str, tuple[object, ...]]
    aircraft_conflicts: frozenset[str]
    branch_conflicts: frozenset[tuple[str, int]]
    alerts: tuple[object, ...]
    sensor_levels: dict[str, str]
    sensor_envelopes: dict[str, SensorEnvelopeState]
    telemetry_status: str
    telemetry_updated_at_utc: str


@dataclass(slots=True)
class _RuntimeState:
    tracks: dict[str, SmoothedTrackState] = field(default_factory=dict)
    vehicles: dict[str, KinematicActorState] = field(default_factory=dict)
    hazards: dict[str, StaticHazardState] = field(default_factory=dict)
    telemetry_status: str = "Idle"
    telemetry_updated_at_utc: str = "--"


class _LogicLoopWorker(QObject):
    view_state_ready = Signal(object)
    processing_failed = Signal(str)

    def __init__(self, fusion_model: SurfaceTrackFusionModel, physics_engine: SurfacePhysicsEngine, tick_hz: float = LOGIC_TICK_HZ) -> None:
        super().__init__()
        self._fusion_model = fusion_model
        self._physics_engine = physics_engine
        self._runtime = _RuntimeState()
        self._tick_interval_ms = max(1, int(round(1000.0 / max(tick_hz, 1.0))))
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self._tick_interval_ms)
        self._timer.timeout.connect(self._process_tick)
        self._timer.start()

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    @Slot(object)
    def ingest_telemetry_batch(self, batch: TelemetryIngestBatch) -> None:
        self._fusion_model.ingest(list(batch.observations), batch.observed_at_monotonic)
        self._runtime.telemetry_status = batch.source_status
        self._runtime.telemetry_updated_at_utc = batch.updated_at_utc

    @Slot(object)
    def update_support_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        vehicles = payload.get("vehicles", ())
        hazards = payload.get("hazards", ())
        self._runtime.vehicles = {vehicle.actor_id: vehicle for vehicle in vehicles if isinstance(vehicle, KinematicActorState)}
        self._runtime.hazards = {hazard.hazard_id: hazard for hazard in hazards if isinstance(hazard, StaticHazardState)}

    @Slot()
    def clear(self) -> None:
        self._fusion_model.clear()
        self._runtime = _RuntimeState()

    def _process_tick(self) -> None:
        try:
            timestamp = time.monotonic()
            tracks = self._fusion_model.snapshot(timestamp)
            self._runtime.tracks = tracks
            frame = PhysicsFrameRequest(
                aircraft=tuple(_track_to_actor_state(track) for track in tracks.values()),
                vehicles=tuple(self._runtime.vehicles.values()),
                hazards=tuple(self._runtime.hazards.values()),
            )
            result = self._physics_engine.process(frame)
            view_state = RadarViewState(
                tracks=dict(tracks),
                predictions=result.predictions,
                aircraft_conflicts=result.aircraft_conflicts,
                branch_conflicts=result.branch_conflicts,
                alerts=result.alerts,
                sensor_levels=result.sensor_levels,
                sensor_envelopes=result.sensor_envelopes,
                telemetry_status=self._runtime.telemetry_status,
                telemetry_updated_at_utc=self._runtime.telemetry_updated_at_utc,
            )
        except Exception as exc:
            self.processing_failed.emit(str(exc))
            return
        self.view_state_ready.emit(view_state)


class RadarViewModel(QObject):
    """Runs deterministic fusion/physics on a headless thread and emits immutable view snapshots."""

    view_state_ready = Signal(object)
    physics_failed = Signal(str)
    telemetry_status_changed = Signal(str)
    submit_telemetry_batch = Signal(object)
    submit_support_payload = Signal(object)
    clear_runtime = Signal()
    stop_runtime = Signal()

    def __init__(self, fusion_model: SurfaceTrackFusionModel, physics_engine: SurfacePhysicsEngine) -> None:
        super().__init__()
        self._logic_thread = QThread(self)
        self._worker = _LogicLoopWorker(fusion_model, physics_engine)
        self._worker.moveToThread(self._logic_thread)
        self._logic_thread.started.connect(self._worker.start)
        self.stop_runtime.connect(self._worker.stop)
        self.submit_telemetry_batch.connect(self._worker.ingest_telemetry_batch)
        self.submit_support_payload.connect(self._worker.update_support_payload)
        self.clear_runtime.connect(self._worker.clear)
        self._worker.view_state_ready.connect(self._on_worker_view_state_ready)
        self._worker.processing_failed.connect(self.physics_failed)
        self._logic_thread.start()

    def shutdown(self) -> None:
        self.stop_runtime.emit()
        self._logic_thread.quit()
        self._logic_thread.wait(5000)

    def ingest_telemetry_snapshot(self, snapshot: TelemetrySnapshot, observed_at_monotonic: float | None = None) -> None:
        batch = snapshot_to_surface_batch(snapshot, observed_at_monotonic)
        self.ingest_telemetry_batch(batch)

    def ingest_telemetry_batch(self, batch: TelemetryIngestBatch) -> None:
        self.submit_telemetry_batch.emit(batch)
        self.telemetry_status_changed.emit(batch.source_status)

    def update_support_actors(self, vehicles: list[GroundVehicleState], hazards: list[StaticObstacle]) -> None:
        self.submit_support_payload.emit(
            {
                "vehicles": tuple(_vehicle_to_actor_state(vehicle) for vehicle in vehicles),
                "hazards": tuple(_hazard_to_state(hazard) for hazard in hazards),
            }
        )

    def tick(self, now_monotonic: float | None = None) -> None:
        del now_monotonic

    def clear(self) -> None:
        self.clear_runtime.emit()

    @Slot(object)
    def _on_worker_view_state_ready(self, view_state: object) -> None:
        if not isinstance(view_state, RadarViewState):
            self.physics_failed.emit("Invalid logic payload")
            return
        self.view_state_ready.emit(view_state)


def _track_to_actor_state(track: SmoothedTrackState) -> KinematicActorState:
    return KinematicActorState(
        actor_id=track.actor_id,
        callsign=track.callsign,
        latitude=track.latitude,
        longitude=track.longitude,
        heading_deg=track.heading_deg,
        speed_mps=track.speed_mps,
        length_m=track.length_m,
        width_m=track.width_m,
        actor_type=track.actor_type,
        profile_label=track.profile_label,
    )


def _vehicle_to_actor_state(vehicle: GroundVehicleState) -> KinematicActorState:
    return KinematicActorState(
        actor_id=vehicle.actor_id,
        callsign=vehicle.callsign,
        latitude=vehicle.latitude,
        longitude=vehicle.longitude,
        heading_deg=vehicle.heading_deg,
        speed_mps=vehicle.speed_mps,
        length_m=vehicle.length_m,
        width_m=vehicle.width_m,
        actor_type="vehicle",
        profile_label=vehicle.vehicle_label,
    )


def _hazard_to_state(hazard: StaticObstacle) -> StaticHazardState:
    return StaticHazardState(
        hazard_id=hazard.obstacle_id,
        latitude=hazard.latitude,
        longitude=hazard.longitude,
        radius_m=hazard.hazard_radius_m,
        category=hazard.category,
        description=hazard.description,
        hazard_type="wildlife" if hazard.is_wildlife else "fod",
    )
