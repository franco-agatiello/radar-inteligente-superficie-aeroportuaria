from __future__ import annotations

import time
from dataclasses import dataclass

from asmgcs.domain.contracts import SurfaceObservation
from models import AircraftState, TelemetrySnapshot


@dataclass(frozen=True, slots=True)
class TelemetryIngestBatch:
    observed_at_monotonic: float
    observations: tuple[SurfaceObservation, ...]
    source_status: str
    updated_at_utc: str


def snapshot_to_surface_batch(snapshot: TelemetrySnapshot, observed_at_monotonic: float | None = None) -> TelemetryIngestBatch:
    timestamp = observed_at_monotonic if observed_at_monotonic is not None else time.monotonic()
    observations = tuple(_aircraft_to_observation(aircraft) for aircraft in snapshot.aircraft if aircraft.on_ground)
    return TelemetryIngestBatch(
        observed_at_monotonic=timestamp,
        observations=observations,
        source_status=snapshot.status,
        updated_at_utc=snapshot.updated_at_utc,
    )


def _aircraft_to_observation(aircraft: AircraftState) -> SurfaceObservation:
    return SurfaceObservation(
        actor_id=aircraft.icao24,
        callsign=aircraft.callsign,
        latitude=aircraft.latitude,
        longitude=aircraft.longitude,
        heading_deg=aircraft.heading_deg,
        speed_mps=aircraft.speed_mps,
        on_ground=aircraft.on_ground,
        profile_label=aircraft.profile.code,
        length_m=aircraft.profile.length_m,
        width_m=aircraft.profile.wingspan_m,
        actor_type="aircraft",
    )