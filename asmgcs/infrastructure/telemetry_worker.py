from __future__ import annotations

import time

import requests
from PySide6.QtCore import QThread, Signal

from asmgcs.infrastructure.opensky_client import OpenSkyClient, format_network_error
from models import AirportConfig, TELEMETRY_INTERVAL_SECONDS, TelemetrySnapshot


class OpenSkyTelemetryWorker(QThread):
    telemetry_updated = Signal(object)

    def __init__(self, airport: AirportConfig) -> None:
        super().__init__()
        self.airport = airport
        self._running = True
        self._client = OpenSkyClient(airport)
        self._last_good_aircraft = self._client.load_snapshot_cache()
        self._backoff_until_monotonic = 0.0

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        has_auth = self._client.has_authentication
        cycle_seconds = TELEMETRY_INTERVAL_SECONDS if has_auth else max(TELEMETRY_INTERVAL_SECONDS, 15.0)
        while self._running:
            timestamp = time.strftime("%H:%M:%S UTC", time.gmtime())
            try:
                if time.monotonic() < self._backoff_until_monotonic:
                    raise requests.RequestException("OpenSky cooldown active after rate limit")
                aircraft = self._client.fetch_ground_aircraft()
                if aircraft:
                    self._last_good_aircraft = aircraft
                    self._client.store_snapshot_cache(aircraft)
                    status = f"{self.airport.code} OpenSky ground telemetry nominal | cycle {cycle_seconds:.0f} s"
                else:
                    cached_aircraft = self._client.load_snapshot_cache()
                    if cached_aircraft:
                        aircraft = cached_aircraft
                        self._last_good_aircraft = cached_aircraft
                        status = f"{self.airport.code} OpenSky returned no ground aircraft | showing cached real snapshot"
                    else:
                        seeded_aircraft = self._client.load_seed_ground_cache()
                        if seeded_aircraft:
                            aircraft = seeded_aircraft
                            self._last_good_aircraft = seeded_aircraft
                            status = f"{self.airport.code} OpenSky returned no ground aircraft | showing seeded airport ground traffic"
                        else:
                            self._last_good_aircraft = []
                            status = f"{self.airport.code} OpenSky returned no ground aircraft in the selected box"
            except (requests.RequestException, ValueError) as exc:
                aircraft = self._last_good_aircraft
                error_text = format_network_error(exc)
                if "rate limit" in error_text.lower() or "429" in error_text:
                    self._backoff_until_monotonic = time.monotonic() + 90.0
                if aircraft:
                    status = f"{self.airport.code} OpenSky degraded: {error_text} | using last valid snapshot"
                else:
                    cached_aircraft = self._client.load_snapshot_cache()
                    if cached_aircraft:
                        aircraft = cached_aircraft
                        self._last_good_aircraft = cached_aircraft
                        status = f"{self.airport.code} OpenSky degraded: {error_text} | showing cached real snapshot"
                    else:
                        seeded_aircraft = self._client.load_seed_ground_cache()
                        if seeded_aircraft:
                            aircraft = seeded_aircraft
                            self._last_good_aircraft = seeded_aircraft
                            status = f"{self.airport.code} OpenSky degraded: {error_text} | showing seeded airport ground traffic"
                        elif has_auth:
                            status = f"{self.airport.code} OpenSky degraded: {error_text} | no valid snapshot yet"
                        else:
                            status = f"{self.airport.code} OpenSky degraded: {error_text} | configure OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET or username/password"

            self.telemetry_updated.emit(
                TelemetrySnapshot(
                    airport_code=self.airport.code,
                    aircraft=aircraft,
                    status=status,
                    updated_at_utc=timestamp,
                )
            )
            self.msleep(int(cycle_seconds * 1000))