from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from asmgcs.infrastructure.opensky_client import OpenSkyClient, format_network_error
from asmgcs.infrastructure.telemetry_worker import OpenSkyTelemetryWorker
from models import AIRPORTS, AircraftProfile, AircraftState


class OpenSkyClientTests(unittest.TestCase):
    def test_format_network_error_handles_rate_limit(self) -> None:
        response = requests.Response()
        response.status_code = 429
        error = requests.HTTPError(response=response)

        self.assertEqual(format_network_error(error), "OpenSky rate limit exceeded")

    def test_load_snapshot_cache_returns_empty_on_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = OpenSkyClient(AIRPORTS["SABE"], cache_root=Path(temp_dir))
            client._snapshot_cache_path.write_text("{broken", encoding="utf-8")

            self.assertEqual(client.load_snapshot_cache(), [])


class _FakeOpenSkyClient:
    def __init__(self, aircraft: list[AircraftState] | None = None, error: Exception | None = None, has_authentication: bool = True) -> None:
        self._aircraft = aircraft or []
        self._error = error
        self._has_authentication = has_authentication
        self._snapshot_cache: list[AircraftState] = []
        self._seed_cache: list[AircraftState] = []

    @property
    def has_authentication(self) -> bool:
        return self._has_authentication

    def fetch_ground_aircraft(self) -> list[AircraftState]:
        if self._error is not None:
            raise self._error
        return list(self._aircraft)

    def store_snapshot_cache(self, aircraft: list[AircraftState]) -> None:
        self._snapshot_cache = list(aircraft)

    def load_snapshot_cache(self) -> list[AircraftState]:
        return list(self._snapshot_cache)

    def load_seed_ground_cache(self) -> list[AircraftState]:
        return list(self._seed_cache)


class OpenSkyTelemetryWorkerTests(unittest.TestCase):
    def _sample_aircraft(self) -> AircraftState:
        airport = AIRPORTS["SABE"]
        return AircraftState(
            icao24="abc123",
            callsign="TEST123",
            latitude=airport.lat_min + 0.001,
            longitude=airport.lon_min + 0.001,
            heading_deg=90.0,
            speed_mps=5.0,
            on_ground=True,
            profile=AircraftProfile("A320", 37.6, 34.1),
        )

    def test_worker_uses_cached_snapshot_on_rate_limit(self) -> None:
        response = requests.Response()
        response.status_code = 429
        cached_aircraft = [self._sample_aircraft()]
        fake_client = _FakeOpenSkyClient(error=requests.HTTPError(response=response), has_authentication=True)
        fake_client._snapshot_cache = cached_aircraft

        with patch("asmgcs.infrastructure.telemetry_worker.OpenSkyClient", return_value=fake_client):
            worker = OpenSkyTelemetryWorker(AIRPORTS["SABE"])

        emitted: list[object] = []
        worker.telemetry_updated.connect(emitted.append)

        def _stop_after_first_cycle(_milliseconds: int) -> None:
            worker._running = False

        worker.msleep = _stop_after_first_cycle
        worker.run()

        self.assertEqual(len(emitted), 1)
        snapshot = emitted[0]
        self.assertEqual(snapshot.aircraft, cached_aircraft)
        self.assertIn("using last valid snapshot", snapshot.status)
        self.assertGreater(worker._backoff_until_monotonic, 0.0)

    def test_worker_uses_seeded_ground_cache_when_live_feed_is_empty(self) -> None:
        seeded_aircraft = [self._sample_aircraft()]
        fake_client = _FakeOpenSkyClient(aircraft=[], has_authentication=False)
        fake_client._seed_cache = seeded_aircraft

        with patch("asmgcs.infrastructure.telemetry_worker.OpenSkyClient", return_value=fake_client):
            worker = OpenSkyTelemetryWorker(AIRPORTS["SABE"])

        emitted: list[object] = []
        worker.telemetry_updated.connect(emitted.append)

        def _stop_after_first_cycle(_milliseconds: int) -> None:
            worker._running = False

        worker.msleep = _stop_after_first_cycle
        worker.run()

        self.assertEqual(len(emitted), 1)
        snapshot = emitted[0]
        self.assertEqual(snapshot.aircraft, seeded_aircraft)
        self.assertIn("showing seeded airport ground traffic", snapshot.status)


if __name__ == "__main__":
    unittest.main()