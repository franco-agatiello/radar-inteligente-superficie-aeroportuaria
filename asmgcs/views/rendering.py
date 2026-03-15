from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPainterPath, QPixmap, QPolygonF
from shapely.geometry import MultiPolygon, Polygon

from models import GeoReference, RenderState, StaticObstacle, WebMercatorMapper


VISIBILITY_HALO_ZOOM_THRESHOLD = 0.95
HALO_PIXEL_RADIUS = 26.0
VEHICLE_SPRITE_SCALE = 2.35
VEHICLE_MIN_SPRITE_WIDTH_M = 5.8
VEHICLE_MIN_SPRITE_LENGTH_M = 9.6
IMG_DIR = Path(__file__).resolve().parents[2] / "img"
SPRITE_FILENAMES = {
    "aircraft_twin": "avion_2_motores.tiff",
    "aircraft_heavy": "avion_4_motores_grande.tiff",
    "aircraft_medium": "avion_4_motores_pequeño.tiff",
    "helicopter": "helicoptero.tif",
    "vehicle": "utilitario.tiff",
}
SPRITE_CACHE: dict[str, QPixmap] = {}

AIRCRAFT_OUTLINE = [
    (0.00, -0.50),
    (0.05, -0.38),
    (0.08, -0.24),
    (0.34, -0.17),
    (0.54, -0.06),
    (0.19, -0.01),
    (0.13, 0.22),
    (0.22, 0.33),
    (0.17, 0.39),
    (0.06, 0.31),
    (0.03, 0.41),
    (0.03, 0.50),
    (-0.03, 0.50),
    (-0.03, 0.41),
    (-0.06, 0.31),
    (-0.17, 0.39),
    (-0.22, 0.33),
    (-0.13, 0.22),
    (-0.19, -0.01),
    (-0.54, -0.06),
    (-0.34, -0.17),
    (-0.08, -0.24),
    (-0.05, -0.38),
]

VEHICLE_OUTLINE = [
    (-0.50, -0.50),
    (0.50, -0.50),
    (0.50, 0.50),
    (-0.50, 0.50),
]

FUEL_TRUCK_OUTLINE = [
    (-0.50, -0.48),
    (0.50, -0.48),
    (0.50, 0.16),
    (0.34, 0.16),
    (0.34, 0.50),
    (-0.34, 0.50),
    (-0.34, 0.16),
    (-0.50, 0.16),
]

PUSHBACK_TUG_OUTLINE = [
    (-0.50, -0.20),
    (-0.08, -0.20),
    (-0.08, -0.50),
    (0.18, -0.50),
    (0.18, -0.20),
    (0.50, -0.20),
    (0.50, 0.50),
    (-0.50, 0.50),
]

FOLLOW_ME_OUTLINE = [
    (-0.46, -0.38),
    (0.46, -0.38),
    (0.34, 0.08),
    (0.40, 0.50),
    (-0.40, 0.50),
    (-0.34, 0.08),
]

BAGGAGE_CART_OUTLINE = [
    (-0.50, -0.46),
    (0.50, -0.46),
    (0.50, 0.30),
    (0.16, 0.30),
    (0.16, 0.50),
    (-0.16, 0.50),
    (-0.16, 0.30),
    (-0.50, 0.30),
]


def basis_vectors(heading_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    heading_rad = math.radians(heading_deg)
    right_vector = (math.cos(heading_rad), math.sin(heading_rad))
    forward_vector = (math.sin(heading_rad), -math.cos(heading_rad))
    return right_vector, forward_vector


def scene_point_from_local_xy(geo: GeoReference, mapper: WebMercatorMapper, local_xy: tuple[float, float]) -> QPointF:
    latitude, longitude = geo.to_geodetic(local_xy[0], local_xy[1])
    pixel_x, pixel_y = mapper.geo_to_pixel(latitude, longitude)
    return QPointF(pixel_x, pixel_y)


def proximity_bands_m(primary_speed_mps: float, secondary_speed_mps: float | None, involves_aircraft: bool) -> tuple[float, float, float]:
    other_speed = secondary_speed_mps or 0.0
    reference_speed = max(primary_speed_mps, other_speed)
    if involves_aircraft:
        if primary_speed_mps < 0.5 and other_speed < 0.5:
            return 6.0, 3.0, 0.0
        if reference_speed < 1.0:
            return 10.0, 6.0, 2.0
        if reference_speed < 3.0:
            return 16.0, 10.0, 6.0
        if reference_speed < 8.0:
            return 24.0, 16.0, 10.0
        return 36.0, 24.0, 15.0
    if reference_speed < 1.0:
        return 4.0, 2.0, 0.0
    if reference_speed < 4.0:
        return 8.0, 5.0, 3.0
    return 12.0, 8.0, 4.0


def append_sensor_summary(tooltip_text: str, level: str, nearest_distance_m: float | None, bands_m: tuple[float, float, float], nearest_label: str) -> str:
    if nearest_distance_m is None:
        return tooltip_text + "\nSensor: clear"
    green_band, yellow_band, red_band = bands_m
    return (
        tooltip_text
        + f"\nSensor: {level.upper()}"
        + f"\nNearest: {nearest_label} at {nearest_distance_m:.1f} m"
        + f"\nBands G/Y/R: {green_band:.0f} / {yellow_band:.0f} / {red_band:.0f} m"
    )


def aircraft_display_color(_: RenderState) -> QColor:
    return QColor("#ffd54a")


def build_actor_tooltip(state: RenderState, actor_type: str) -> str:
    type_label = "Aircraft" if actor_type == "aircraft" else "Ground Vehicle"
    speed_band = "Stopped" if state.speed_mps < 0.5 else "Slow taxi" if state.speed_mps < 3.0 else "Taxi" if state.speed_mps < 8.0 else "Fast taxi"
    lines = [
        f"{state.callsign} | {type_label}",
        f"Profile: {state.profile_label}",
        f"Heading: {state.heading_deg:.1f} deg",
        f"Speed: {state.speed_mps:.1f} m/s",
        f"Behavior: {speed_band}",
        f"Position: {state.latitude:+.5f}, {state.longitude:+.5f}",
    ]
    if actor_type == "aircraft":
        lines.append("Click to lock follow camera")
    return "\n".join(lines)


def load_sprite(sprite_key: str) -> QPixmap | None:
    sprite_name = SPRITE_FILENAMES.get(sprite_key)
    if not sprite_name:
        return None
    if sprite_name not in SPRITE_CACHE:
        pixmap = QPixmap(str(IMG_DIR / sprite_name))
        SPRITE_CACHE[sprite_name] = pixmap
    pixmap = SPRITE_CACHE[sprite_name]
    return None if pixmap.isNull() else pixmap


def resolve_actor_sprite(state: RenderState) -> tuple[QPixmap | None, float, float]:
    if state.actor_type == "vehicle":
        return None, 0.0, 0.0
    profile_code = state.profile_label.upper()
    if "HEL" in profile_code:
        return load_sprite("helicopter"), state.width_m * 1.35, state.length_m * 1.35
    if state.length_m >= 65.0 or state.width_m >= 60.0:
        return load_sprite("aircraft_heavy"), state.width_m * 1.12, state.length_m * 1.12
    if state.length_m >= 50.0 or state.width_m >= 44.0:
        return load_sprite("aircraft_medium"), state.width_m * 1.08, state.length_m * 1.08
    return load_sprite("aircraft_twin"), state.width_m * 1.08, state.length_m * 1.08


def fit_sprite_size_scene(pixmap: QPixmap | None, target_width_scene: float, target_length_scene: float) -> tuple[float, float]:
    if pixmap is None or pixmap.isNull() or target_width_scene <= 0.0 or target_length_scene <= 0.0:
        return target_width_scene, target_length_scene
    source_width = max(1, pixmap.width())
    source_height = max(1, pixmap.height())
    scale = min(target_width_scene / source_width, target_length_scene / source_height)
    return source_width * scale, source_height * scale


def vehicle_outline_for_profile(profile_label: str) -> list[tuple[float, float]]:
    label = profile_label.strip().lower()
    if "fuel" in label:
        return FUEL_TRUCK_OUTLINE
    if "pushback" in label or "tug" in label:
        return PUSHBACK_TUG_OUTLINE
    if "follow" in label:
        return FOLLOW_ME_OUTLINE
    if "baggage" in label or "cart" in label:
        return BAGGAGE_CART_OUTLINE
    return VEHICLE_OUTLINE


def build_obstacle_tooltip(obstacle: StaticObstacle) -> str:
    color_hint = "Wildlife / brown animal marker" if obstacle.is_wildlife else "FOD / silver debris marker"
    lines = [
        f"{obstacle.obstacle_id} | {obstacle.category}",
        f"Type: {'Wildlife' if obstacle.is_wildlife else 'FOD'}",
        obstacle.description,
        f"Color ID: {color_hint}",
        f"Radius: {obstacle.hazard_radius_m:.2f} m",
        f"Position: {obstacle.latitude:+.5f}, {obstacle.longitude:+.5f}",
    ]
    if obstacle.in_conflict and obstacle.conflicting_actor:
        lines.append(f"Conflict: {obstacle.conflicting_actor}")
    return "\n".join(lines)


def build_aircraft_silhouette_path(state: RenderState, geo: GeoReference, mapper: WebMercatorMapper) -> tuple[QPointF, QPainterPath]:
    center_point, path, _ = build_aircraft_geometry(state, geo, mapper)
    return center_point, path


def build_aircraft_geometry(state: RenderState, geo: GeoReference, mapper: WebMercatorMapper) -> tuple[QPointF, QPainterPath, QPolygonF]:
    center_xy = geo.to_local_xy(state.latitude, state.longitude)
    center_point = scene_point_from_local_xy(geo, mapper, center_xy)
    right_vector, forward_vector = basis_vectors(state.heading_deg)

    def relative_point(x_norm: float, y_norm: float) -> QPointF:
        local_x = x_norm * state.width_m
        local_y = y_norm * state.length_m
        world_xy = (
            center_xy[0] + (local_x * right_vector[0]) + (local_y * forward_vector[0]),
            center_xy[1] + (local_x * right_vector[1]) + (local_y * forward_vector[1]),
        )
        scene_point = scene_point_from_local_xy(geo, mapper, world_xy)
        return QPointF(scene_point.x() - center_point.x(), scene_point.y() - center_point.y())

    path = QPainterPath()
    path.moveTo(relative_point(0.00, -0.56))
    path.cubicTo(relative_point(0.05, -0.52), relative_point(0.08, -0.43), relative_point(0.08, -0.30))
    path.lineTo(relative_point(0.18, -0.25))
    path.lineTo(relative_point(0.57, -0.20))
    path.lineTo(relative_point(0.63, -0.12))
    path.lineTo(relative_point(0.22, -0.05))
    path.lineTo(relative_point(0.14, 0.02))
    path.lineTo(relative_point(0.18, 0.26))
    path.lineTo(relative_point(0.29, 0.37))
    path.lineTo(relative_point(0.21, 0.43))
    path.lineTo(relative_point(0.08, 0.30))
    path.lineTo(relative_point(0.05, 0.43))
    path.lineTo(relative_point(0.03, 0.56))
    path.lineTo(relative_point(-0.03, 0.56))
    path.lineTo(relative_point(-0.05, 0.43))
    path.lineTo(relative_point(-0.08, 0.30))
    path.lineTo(relative_point(-0.21, 0.43))
    path.lineTo(relative_point(-0.29, 0.37))
    path.lineTo(relative_point(-0.18, 0.26))
    path.lineTo(relative_point(-0.14, 0.02))
    path.lineTo(relative_point(-0.22, -0.05))
    path.lineTo(relative_point(-0.63, -0.12))
    path.lineTo(relative_point(-0.57, -0.20))
    path.lineTo(relative_point(-0.18, -0.25))
    path.lineTo(relative_point(-0.08, -0.30))
    path.cubicTo(relative_point(-0.08, -0.43), relative_point(-0.05, -0.52), relative_point(0.00, -0.56))
    path.closeSubpath()
    polygon = QPolygonF()
    for local_x, local_y in (
        (-state.width_m / 2.0, -state.length_m / 2.0),
        (state.width_m / 2.0, -state.length_m / 2.0),
        (state.width_m / 2.0, state.length_m / 2.0),
        (-state.width_m / 2.0, state.length_m / 2.0),
    ):
        world_xy = (
            center_xy[0] + (local_x * right_vector[0]) + (local_y * forward_vector[0]),
            center_xy[1] + (local_x * right_vector[1]) + (local_y * forward_vector[1]),
        )
        scene_point = scene_point_from_local_xy(geo, mapper, world_xy)
        polygon.append(QPointF(scene_point.x() - center_point.x(), scene_point.y() - center_point.y()))
    return center_point, path, polygon


def build_relative_path(
    state: RenderState,
    outline: list[tuple[float, float]],
    geo: GeoReference,
    mapper: WebMercatorMapper,
) -> tuple[QPointF, QPainterPath]:
    center_point, path, _ = build_actor_geometry(state, outline, geo, mapper)
    return center_point, path


def build_actor_geometry(
    state: RenderState,
    outline: list[tuple[float, float]],
    geo: GeoReference,
    mapper: WebMercatorMapper,
) -> tuple[QPointF, QPainterPath, QPolygonF]:
    center_xy = geo.to_local_xy(state.latitude, state.longitude)
    center_point = scene_point_from_local_xy(geo, mapper, center_xy)
    right_vector, forward_vector = basis_vectors(state.heading_deg)
    path = QPainterPath()
    for index, (x_norm, y_norm) in enumerate(outline):
        local_x = x_norm * state.width_m
        local_y = y_norm * state.length_m
        world_xy = (
            center_xy[0] + (local_x * right_vector[0]) + (local_y * forward_vector[0]),
            center_xy[1] + (local_x * right_vector[1]) + (local_y * forward_vector[1]),
        )
        scene_point = scene_point_from_local_xy(geo, mapper, world_xy)
        relative_point = QPointF(scene_point.x() - center_point.x(), scene_point.y() - center_point.y())
        if index == 0:
            path.moveTo(relative_point)
        else:
            path.lineTo(relative_point)
    path.closeSubpath()
    polygon = QPolygonF()
    for local_x, local_y in (
        (-state.width_m / 2.0, -state.length_m / 2.0),
        (state.width_m / 2.0, -state.length_m / 2.0),
        (state.width_m / 2.0, state.length_m / 2.0),
        (-state.width_m / 2.0, state.length_m / 2.0),
    ):
        world_xy = (
            center_xy[0] + (local_x * right_vector[0]) + (local_y * forward_vector[0]),
            center_xy[1] + (local_x * right_vector[1]) + (local_y * forward_vector[1]),
        )
        scene_point = scene_point_from_local_xy(geo, mapper, world_xy)
        polygon.append(QPointF(scene_point.x() - center_point.x(), scene_point.y() - center_point.y()))
    return center_point, path, polygon


def build_relative_hitbox(state: RenderState, geo: GeoReference, mapper: WebMercatorMapper) -> tuple[QPointF, QPolygonF]:
    if state.actor_type == "aircraft":
        center_point, _, polygon = build_aircraft_geometry(state, geo, mapper)
        return center_point, polygon
    center_point, _, polygon = build_actor_geometry(state, VEHICLE_OUTLINE, geo, mapper)
    return center_point, polygon


def build_scene_path_from_local_polyline(
    polyline_local: list[tuple[float, float]],
    geo: GeoReference,
    mapper: WebMercatorMapper,
) -> QPainterPath:
    path = QPainterPath()
    for index, local_xy in enumerate(polyline_local):
        point = scene_point_from_local_xy(geo, mapper, local_xy)
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    return path


def build_scene_path_from_local_polygon(
    polygon_local: Polygon,
    geo: GeoReference,
    mapper: WebMercatorMapper,
) -> QPainterPath:
    path = QPainterPath()
    exterior_coords = list(polygon_local.exterior.coords)
    if exterior_coords:
        for index, local_xy in enumerate(exterior_coords):
            point = scene_point_from_local_xy(geo, mapper, local_xy)
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
    for interior in polygon_local.interiors:
        interior_coords = list(interior.coords)
        if not interior_coords:
            continue
        for index, local_xy in enumerate(interior_coords):
            point = scene_point_from_local_xy(geo, mapper, local_xy)
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
    return path


def build_scene_path_from_local_geometry(
    geometry_local: Polygon | MultiPolygon,
    geo: GeoReference,
    mapper: WebMercatorMapper,
) -> QPainterPath:
    path = QPainterPath()
    if isinstance(geometry_local, Polygon):
        return build_scene_path_from_local_polygon(geometry_local, geo, mapper)
    for polygon in geometry_local.geoms:
        path.addPath(build_scene_path_from_local_polygon(polygon, geo, mapper))
    return path


__all__ = [
    "AIRCRAFT_OUTLINE",
    "HALO_PIXEL_RADIUS",
    "VEHICLE_OUTLINE",
    "VISIBILITY_HALO_ZOOM_THRESHOLD",
    "aircraft_display_color",
    "append_sensor_summary",
    "basis_vectors",
    "build_actor_geometry",
    "build_actor_tooltip",
    "build_aircraft_geometry",
    "build_aircraft_silhouette_path",
    "build_scene_path_from_local_geometry",
    "build_scene_path_from_local_polygon",
    "build_obstacle_tooltip",
    "build_relative_hitbox",
    "build_relative_path",
    "build_scene_path_from_local_polyline",
    "fit_sprite_size_scene",
    "proximity_bands_m",
    "resolve_actor_sprite",
    "scene_point_from_local_xy",
]