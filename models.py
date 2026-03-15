from __future__ import annotations

import math
from dataclasses import dataclass

from geopy.distance import distance


TILE_SIZE = 256
PREDICTION_HORIZON_SECONDS = 15.0
TELEMETRY_INTERVAL_SECONDS = 7.0
REQUEST_TIMEOUT_SECONDS = 12.0
ANIMATION_FPS = 30


@dataclass(frozen=True, slots=True)
class AirportConfig:
    code: str
    display_name: str
    city: str
    center_lat: float
    center_lon: float
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    map_zoom: int


AIRPORTS: dict[str, AirportConfig] = {
    "SABE": AirportConfig(
        code="SABE",
        display_name="Aeroparque Jorge Newbery",
        city="Buenos Aires, Argentina",
        center_lat=-34.5592,
        center_lon=-58.4156,
        lat_min=-34.5680,
        lat_max=-34.5500,
        lon_min=-58.4250,
        lon_max=-58.4000,
        map_zoom=16,
    ),
    "ATL": AirportConfig(
        code="ATL",
        display_name="Hartsfield-Jackson Atlanta International",
        city="Atlanta, United States",
        center_lat=33.6367,
        center_lon=-84.4281,
        lat_min=33.6100,
        lat_max=33.6600,
        lon_min=-84.4500,
        lon_max=-84.3900,
        map_zoom=15,
    ),
    "SFO": AirportConfig(
        code="SFO",
        display_name="San Francisco International",
        city="San Francisco, United States",
        center_lat=37.6213,
        center_lon=-122.3790,
        lat_min=37.6070,
        lat_max=37.6318,
        lon_min=-122.3975,
        lon_max=-122.3600,
        map_zoom=15,
    ),
}


@dataclass(frozen=True, slots=True)
class AircraftProfile:
    code: str
    length_m: float
    wingspan_m: float


@dataclass(slots=True)
class AircraftState:
    icao24: str
    callsign: str
    latitude: float
    longitude: float
    heading_deg: float
    speed_mps: float
    on_ground: bool
    profile: AircraftProfile


@dataclass(slots=True)
class TelemetrySnapshot:
    airport_code: str
    aircraft: list[AircraftState]
    status: str
    updated_at_utc: str


@dataclass(slots=True)
class StaticObstacle:
    obstacle_id: str
    latitude: float
    longitude: float
    category: str
    description: str
    is_wildlife: bool
    hazard_radius_m: float
    in_conflict: bool = False
    conflicting_actor: str = ""


@dataclass(slots=True)
class RenderState:
    actor_id: str
    callsign: str
    latitude: float
    longitude: float
    heading_deg: float
    speed_mps: float
    length_m: float
    width_m: float
    actor_type: str
    profile_label: str


@dataclass(slots=True)
class TrackInterpolation:
    actor_id: str
    callsign: str
    previous_state: AircraftState
    target_state: AircraftState
    start_monotonic: float
    duration_s: float

    def current_state(self, now_monotonic: float) -> RenderState:
        ratio = clamp((now_monotonic - self.start_monotonic) / max(self.duration_s, 0.001), 0.0, 1.0)
        return RenderState(
            actor_id=self.actor_id,
            callsign=self.callsign,
            latitude=lerp(self.previous_state.latitude, self.target_state.latitude, ratio),
            longitude=lerp(self.previous_state.longitude, self.target_state.longitude, ratio),
            heading_deg=lerp_heading(self.previous_state.heading_deg, self.target_state.heading_deg, ratio),
            speed_mps=lerp(self.previous_state.speed_mps, self.target_state.speed_mps, ratio),
            length_m=self.target_state.profile.length_m,
            width_m=self.target_state.profile.wingspan_m,
            actor_type="aircraft",
            profile_label=self.target_state.profile.code,
        )


@dataclass(slots=True)
class GroundVehicleState:
    actor_id: str
    callsign: str
    latitude: float
    longitude: float
    heading_deg: float
    speed_mps: float
    length_m: float
    width_m: float
    color_hex: str
    vehicle_label: str

    def as_render_state(self) -> RenderState:
        return RenderState(
            actor_id=self.actor_id,
            callsign=self.callsign,
            latitude=self.latitude,
            longitude=self.longitude,
            heading_deg=self.heading_deg,
            speed_mps=self.speed_mps,
            length_m=self.length_m,
            width_m=self.width_m,
            actor_type="vehicle",
            profile_label=self.vehicle_label,
        )


@dataclass(frozen=True, slots=True)
class VehicleSpec:
    label: str
    color_hex: str
    length_m: float
    width_m: float


GROUND_VEHICLE_SPECS: list[VehicleSpec] = [
    VehicleSpec("Fuel Truck", "#ffe082", 12.0, 4.0),
    VehicleSpec("Pushback Tug", "#ffcc80", 8.0, 3.6),
    VehicleSpec("Follow-Me", "#80cbc4", 4.8, 2.2),
    VehicleSpec("Baggage Cart", "#aed581", 6.0, 2.5),
]


@dataclass(slots=True)
class ProjectedPath:
    final_latitude: float
    final_longitude: float
    final_heading_deg: float
    local_polyline: list[tuple[float, float]]
    branch_id: str = ""


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def lerp(start: float, end: float, ratio: float) -> float:
    return start + ((end - start) * ratio)


def lerp_heading(start_deg: float, end_deg: float, ratio: float) -> float:
    delta = ((end_deg - start_deg + 180.0) % 360.0) - 180.0
    return (start_deg + (delta * ratio)) % 360.0


def heading_diff_deg(reference_deg: float, candidate_deg: float) -> float:
    return abs(((candidate_deg - reference_deg + 180.0) % 360.0) - 180.0)


def segment_heading_deg(start_xy: tuple[float, float], end_xy: tuple[float, float]) -> float:
    dx = end_xy[0] - start_xy[0]
    dy = end_xy[1] - start_xy[1]
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def project_linear(latitude: float, longitude: float, heading_deg: float, speed_mps: float, horizon_s: float) -> tuple[float, float]:
    projected = distance(meters=speed_mps * horizon_s).destination((latitude, longitude), heading_deg)
    return projected.latitude, projected.longitude


class GeoReference:
    """Converts airport geodetic coordinates to a local tangent plane in meters."""

    def __init__(self, center_lat: float, center_lon: float) -> None:
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.center_lat_rad = math.radians(center_lat)
        self.meters_per_deg_lat = 111_132.92
        self.meters_per_deg_lon = 111_412.84 * math.cos(self.center_lat_rad)

    def to_local_xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        x_meters = (longitude - self.center_lon) * self.meters_per_deg_lon
        y_meters = (self.center_lat - latitude) * self.meters_per_deg_lat
        return x_meters, y_meters

    def to_geodetic(self, x_meters: float, y_meters: float) -> tuple[float, float]:
        latitude = self.center_lat - (y_meters / self.meters_per_deg_lat)
        longitude = self.center_lon + (x_meters / self.meters_per_deg_lon)
        return latitude, longitude


class WebMercatorMapper:
    """Maps geodetic coordinates onto the stitched raster background in pixels."""

    def __init__(self, airport: AirportConfig) -> None:
        self.airport = airport
        self.origin_global_x, self.origin_global_y = self._global_pixel(airport.lat_max, airport.lon_min)
        self.end_global_x, self.end_global_y = self._global_pixel(airport.lat_min, airport.lon_max)
        self.pixel_width = max(1, int(math.ceil(self.end_global_x - self.origin_global_x)))
        self.pixel_height = max(1, int(math.ceil(self.end_global_y - self.origin_global_y)))

    def geo_to_pixel(self, latitude: float, longitude: float) -> tuple[float, float]:
        global_x, global_y = self._global_pixel(latitude, longitude)
        return global_x - self.origin_global_x, global_y - self.origin_global_y

    def _global_pixel(self, latitude: float, longitude: float) -> tuple[float, float]:
        latitude = clamp(latitude, -85.05112878, 85.05112878)
        sin_lat = math.sin(math.radians(latitude))
        map_scale = TILE_SIZE * (2**self.airport.map_zoom)
        x_coord = ((longitude + 180.0) / 360.0) * map_scale
        y_coord = (0.5 - (math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi))) * map_scale
        return x_coord, y_coord