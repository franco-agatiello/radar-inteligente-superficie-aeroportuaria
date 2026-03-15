from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from aircraft_db import resolve_aircraft_profile
from models import AircraftState, AirportConfig, REQUEST_TIMEOUT_SECONDS


OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
OPENSKY_TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
HTTP_USER_AGENT = "A-SMGCS-HMI/4.0"


def format_network_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code == 429:
            return "OpenSky rate limit exceeded"
        return f"HTTP {exc.response.status_code}"
    return str(exc)


class OpenSkyClient:
    def __init__(self, airport: AirportConfig, cache_root: Path | None = None) -> None:
        self.airport = airport
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": HTTP_USER_AGENT})
        username = os.getenv("OPENSKY_USERNAME")
        password = os.getenv("OPENSKY_PASSWORD")
        self._client_id = os.getenv("OPENSKY_CLIENT_ID")
        self._client_secret = os.getenv("OPENSKY_CLIENT_SECRET")
        self._cache_root = cache_root or (Path(__file__).resolve().parents[2] / ".cache")
        self._cache_dir = self._cache_root / self.airport.code.lower()
        self._snapshot_cache_path = self._cache_dir / "opensky_ground_snapshot.json"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._telemetry_config_dir = self._cache_root / "telemetry"
        self._telemetry_config_dir.mkdir(parents=True, exist_ok=True)
        self._oauth_config_path = self._telemetry_config_dir / "opensky_oauth.json"
        self._seed_ground_path = self._telemetry_config_dir / f"{self.airport.code.lower()}_ground.json"
        if (not self._client_id or not self._client_secret) and self._oauth_config_path.exists():
            try:
                oauth_payload = json.loads(self._oauth_config_path.read_text(encoding="utf-8"))
                self._client_id = self._client_id or str(oauth_payload.get("clientId") or "").strip() or None
                self._client_secret = self._client_secret or str(oauth_payload.get("clientSecret") or "").strip() or None
            except (json.JSONDecodeError, OSError):
                pass
        self._is_authenticated = bool(username and password)
        self._uses_oauth = bool(self._client_id and self._client_secret)
        self._access_token: str | None = None
        self._access_token_expires_monotonic = 0.0
        if username and password:
            self._session.auth = (username, password)

    @property
    def has_authentication(self) -> bool:
        return self._is_authenticated or self._uses_oauth

    def fetch_ground_aircraft(self) -> list[AircraftState]:
        headers: dict[str, str] = {}
        if self._uses_oauth:
            headers["Authorization"] = f"Bearer {self._get_access_token()}"
        response = self._session.get(
            OPENSKY_STATES_URL,
            params={
                "lamin": self.airport.lat_min,
                "lamax": self.airport.lat_max,
                "lomin": self.airport.lon_min,
                "lomax": self.airport.lon_max,
            },
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        states = payload.get("states") or []
        aircraft: list[AircraftState] = []
        for state in states:
            if not bool(state[8]):
                continue
            latitude = state[6]
            longitude = state[5]
            if latitude is None or longitude is None:
                continue
            if not (self.airport.lat_min <= latitude <= self.airport.lat_max and self.airport.lon_min <= longitude <= self.airport.lon_max):
                continue
            icao24 = (state[0] or "unknown").strip().lower()
            callsign = (state[1] or icao24.upper() or "UNKNOWN").strip() or "UNKNOWN"
            velocity = float(state[9] or 0.0)
            heading = float(state[10] or 0.0)
            aircraft.append(
                AircraftState(
                    icao24=icao24,
                    callsign=callsign,
                    latitude=float(latitude),
                    longitude=float(longitude),
                    heading_deg=heading,
                    speed_mps=velocity,
                    on_ground=True,
                    profile=resolve_aircraft_profile(icao24, callsign),
                )
            )
        aircraft.sort(key=lambda item: item.callsign)
        return aircraft

    def store_snapshot_cache(self, aircraft: list[AircraftState]) -> None:
        payload = {
            "airport_code": self.airport.code,
            "saved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "aircraft": [
                {
                    "icao24": item.icao24,
                    "callsign": item.callsign,
                    "latitude": item.latitude,
                    "longitude": item.longitude,
                    "heading_deg": item.heading_deg,
                    "speed_mps": item.speed_mps,
                    "profile_code": item.profile.code,
                }
                for item in aircraft
            ],
        }
        self._snapshot_cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_snapshot_cache(self) -> list[AircraftState]:
        if not self._snapshot_cache_path.exists():
            return []
        return self._load_aircraft_cache_file(self._snapshot_cache_path)

    def load_seed_ground_cache(self) -> list[AircraftState]:
        if not self._seed_ground_path.exists():
            return []
        return self._load_aircraft_cache_file(self._seed_ground_path)

    def _load_aircraft_cache_file(self, cache_path: Path) -> list[AircraftState]:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        cached_aircraft: list[AircraftState] = []
        if isinstance(payload, dict):
            entries = payload.get("aircraft", [])
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = []
        if not isinstance(entries, list):
            return []
        for item in entries:
            icao24 = str(item.get("icao24") or "unknown").strip().lower()
            callsign = str(item.get("callsign") or icao24.upper() or "UNKNOWN").strip() or "UNKNOWN"
            latitude = item.get("latitude")
            longitude = item.get("longitude")
            if latitude is None or longitude is None:
                continue
            cached_aircraft.append(
                AircraftState(
                    icao24=icao24,
                    callsign=callsign,
                    latitude=float(latitude),
                    longitude=float(longitude),
                    heading_deg=float(item.get("heading_deg") or 0.0),
                    speed_mps=float(item.get("speed_mps") or 0.0),
                    on_ground=True,
                    profile=resolve_aircraft_profile(icao24, callsign),
                )
            )
        cached_aircraft.sort(key=lambda entry: entry.callsign)
        return cached_aircraft

    def _get_access_token(self) -> str:
        if self._access_token is not None and time.monotonic() < self._access_token_expires_monotonic:
            return self._access_token
        if not self._client_id or not self._client_secret:
            raise requests.RequestException("Missing OpenSky OAuth client credentials")
        response = self._session.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload.get("access_token")
        expires_in = int(payload.get("expires_in") or 300)
        if not access_token:
            raise requests.RequestException("OpenSky token response missing access token")
        self._access_token = str(access_token)
        self._access_token_expires_monotonic = time.monotonic() + max(expires_in - 30, 30)
        return self._access_token