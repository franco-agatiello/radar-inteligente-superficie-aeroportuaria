from __future__ import annotations

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsScene, QGraphicsSimpleTextItem

from asmgcs.domain.contracts import SensorEnvelopeState
from asmgcs.views.graphics_items import SurfaceActorGraphicsItem, SurfaceObstacleGraphicsItem, SurfaceRadarView
from asmgcs.views.rendering import (
    HALO_PIXEL_RADIUS,
    VEHICLE_OUTLINE,
    VISIBILITY_HALO_ZOOM_THRESHOLD,
    aircraft_display_color,
    append_sensor_summary,
    build_actor_geometry,
    build_actor_tooltip,
    build_aircraft_geometry,
    build_obstacle_tooltip,
    build_scene_path_from_local_geometry,
    build_scene_path_from_local_polyline,
    fit_sprite_size_scene,
    resolve_actor_sprite,
    vehicle_outline_for_profile,
)
from asmgcs.views.viewport import ViewportFrameState, is_ground_contact_relevant_to_focus, is_obstacle_relevant_to_focus, is_relevant_to_focus
from models import AircraftState, RenderState, StaticObstacle, WebMercatorMapper


class RadarSceneController(QObject):
    aircraft_selected = Signal(str)
    tooltip_requested = Signal(object, str)
    tooltip_hidden = Signal()

    def __init__(self, scene: QGraphicsScene, radar_view: SurfaceRadarView) -> None:
        super().__init__()
        self.scene = scene
        self.radar_view = radar_view
        self.actor_items: dict[str, SurfaceActorGraphicsItem] = {}
        self.route_items: dict[str, list[QGraphicsPathItem]] = {}
        self.route_signatures: dict[str, list[tuple[tuple[float, float], ...] | None]] = {}
        self.obstacle_items: dict[str, SurfaceObstacleGraphicsItem] = {}

    def reset_dynamic_items(self) -> None:
        self.scene.clear()
        self.actor_items.clear()
        self.route_items.clear()
        self.route_signatures.clear()
        self.obstacle_items.clear()

    def rebuild_scene(self, airport_code: str, gis_context, geo, mapper: WebMercatorMapper) -> None:
        self.reset_dynamic_items()
        self.scene.setSceneRect(QRectF(0, 0, mapper.pixel_width, mapper.pixel_height))
        self.radar_view.setSceneRect(QRectF(0, 0, mapper.pixel_width, mapper.pixel_height))
        map_item = self.scene.addPixmap(gis_context.map_pixmap)
        map_item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        self._render_sector_overlays(gis_context, geo, mapper)
        overlay_item = self.scene.addRect(QRectF(0, 0, mapper.pixel_width, mapper.pixel_height), QPen(Qt.PenStyle.NoPen), QColor(6, 12, 16, 110))
        overlay_item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        title = QGraphicsSimpleTextItem(f"{airport_code} GIS TRACK-UP SURFACE VIEW")
        title.setBrush(QColor("#7ce3ff"))
        title.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        title.setPos(18, 14)
        self.scene.addItem(title)
        subtitle = QGraphicsSimpleTextItem("Tower mode north-up | Click aircraft for AMMD cockpit mode")
        subtitle.setBrush(QColor("#d7f6ff"))
        subtitle.setFont(QFont("Consolas", 9))
        subtitle.setPos(18, 36)
        self.scene.addItem(subtitle)

    def _render_sector_overlays(self, gis_context, geo, mapper: WebMercatorMapper) -> None:
        sector_styles = {
            "runway": (QColor("#ff6e6e"), QColor("#ff8a80"), 1.8),
            "taxiway": (QColor("#78d2ff"), QColor("#7dd3fc"), 1.2),
            "apron": (QColor("#ffd760"), QColor("#ffd166"), 1.0),
        }
        for sector_name in ("apron", "taxiway", "runway"):
            geometry = gis_context.sector_areas.get(sector_name)
            if geometry is None or geometry.is_empty:
                continue
            fill_color, stroke_color, stroke_width = sector_styles[sector_name]
            zone_rule = gis_context.zone_tuning.get(sector_name)
            overlay_path = build_scene_path_from_local_geometry(geometry, geo, mapper)
            overlay_item = QGraphicsPathItem(overlay_path)
            overlay_item.setPen(QPen(stroke_color, stroke_width))
            overlay_item.setBrush(fill_color)
            overlay_item.setOpacity(zone_rule.overlay_opacity if zone_rule is not None else 0.12)
            overlay_item.setZValue(0.5)
            overlay_item.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
            self.scene.addItem(overlay_item)

        for label in gis_context.labels:
            label_item = QGraphicsSimpleTextItem(label.text)
            label_item.setBrush(QColor("#e9f7ff"))
            label_item.setFont(QFont("Consolas", 8, QFont.Weight.DemiBold))
            point = mapper.geo_to_pixel(*geo.to_geodetic(label.local_xy[0], label.local_xy[1]))
            label_item.setPos(point[0] - 10.0, point[1] - 8.0)
            label_item.setRotation(label.rotation_deg)
            label_item.setOpacity(0.92)
            label_item.setZValue(0.7)
            self.scene.addItem(label_item)

    def render_obstacles(
        self,
        obstacles: list[StaticObstacle],
        gis_context,
        mapper: WebMercatorMapper,
        viewport_state: ViewportFrameState,
        focus_state: RenderState | None,
        geo,
    ) -> None:
        incoming_ids = {obstacle.obstacle_id for obstacle in obstacles}
        for obstacle in obstacles:
            item = self.obstacle_items.get(obstacle.obstacle_id)
            if item is None:
                item = SurfaceObstacleGraphicsItem()
                item.tooltip_requested.connect(self.tooltip_requested.emit)
                item.tooltip_hidden.connect(self.tooltip_hidden.emit)
                self.obstacle_items[obstacle.obstacle_id] = item
                self.scene.addItem(item)
            point_x, point_y = mapper.geo_to_pixel(obstacle.latitude, obstacle.longitude)
            display_radius_scene = max(obstacle.hazard_radius_m * gis_context.pixels_per_meter_mean, 0.8 if obstacle.is_wildlife else 1.6)
            item.update_render(QPointF(point_x, point_y), obstacle, display_radius_scene, build_obstacle_tooltip(obstacle))
            item.setOpacity(self._obstacle_opacity(obstacle, viewport_state, focus_state, geo))
        stale_ids = [obstacle_id for obstacle_id in self.obstacle_items if obstacle_id not in incoming_ids]
        for obstacle_id in stale_ids:
            item = self.obstacle_items.pop(obstacle_id)
            self.scene.removeItem(item)

    def render_actors(
        self,
        geo,
        mapper: WebMercatorMapper,
        pixels_per_meter_mean: float,
        aircraft_states: dict[str, RenderState],
        aircraft_predictions: dict[str, tuple[object, ...]],
        aircraft_conflicts: set[str],
        vehicle_states: dict[str, RenderState],
        vehicle_predictions: dict[str, tuple[object, ...]],
        vehicle_conflicts: set[str],
        branch_conflicts: set[tuple[str, int]],
        sensor_envelopes: dict[str, SensorEnvelopeState],
        viewport_state: ViewportFrameState,
        vehicle_color_hex_by_id: dict[str, str],
    ) -> None:
        all_ids = set(self.actor_items)
        zoom_factor = self.radar_view.zoom_factor()
        focus_state = aircraft_states.get(viewport_state.focus_actor_id or "")

        for actor_id, state in aircraft_states.items():
            center_point, shape_path, hitbox_polygon = build_aircraft_geometry(state, geo, mapper)
            sprite_pixmap, sprite_width_m, sprite_length_m = resolve_actor_sprite(state)
            sprite_size_scene = fit_sprite_size_scene(sprite_pixmap, sprite_width_m * pixels_per_meter_mean, sprite_length_m * pixels_per_meter_mean)
            item = self.actor_items.get(actor_id)
            if item is None:
                item = SurfaceActorGraphicsItem(actor_id, "aircraft", aircraft_display_color(state))
                item.clicked.connect(self.aircraft_selected.emit)
                item.tooltip_requested.connect(self.tooltip_requested.emit)
                item.tooltip_hidden.connect(self.tooltip_hidden.emit)
                self.actor_items[actor_id] = item
                self.scene.addItem(item)
            envelope = sensor_envelopes.get(actor_id)
            bands = envelope.bands if envelope is not None else None
            sensor_band_tuple = (bands.green_m, bands.yellow_m, bands.red_m) if bands is not None else (0.0, 0.0, 0.0)
            sensor_level = envelope.level if envelope is not None else "clear"
            nearest_distance = envelope.nearest_distance_m if envelope is not None else None
            nearest_label = envelope.nearest_label if envelope is not None else "none"
            item.color = aircraft_display_color(state)
            highlight_radius = HALO_PIXEL_RADIUS / max(zoom_factor, 0.35)
            item.update_render(
                center_point,
                shape_path,
                hitbox_polygon,
                state.callsign,
                state.profile_label,
                state.heading_deg,
                selected_for_follow=(actor_id == viewport_state.focus_actor_id),
                in_conflict=(actor_id in aircraft_conflicts),
                highlight_radius_scene=highlight_radius,
                show_visibility_halo=zoom_factor < VISIBILITY_HALO_ZOOM_THRESHOLD,
                sensor_band_radii_scene=tuple(band * pixels_per_meter_mean for band in sensor_band_tuple),
                sensor_level=sensor_level,
                sprite_pixmap=sprite_pixmap,
                sprite_size_scene=sprite_size_scene,
                tooltip_text=append_sensor_summary(build_actor_tooltip(state, "aircraft"), sensor_level, nearest_distance, sensor_band_tuple, nearest_label),
            )
            item.setOpacity(self._actor_opacity(state, viewport_state, focus_state, geo))
            self._render_routes(
                actor_id,
                aircraft_predictions.get(actor_id, ()),
                branch_conflicts,
                QColor("#55d6ff"),
                QColor("#ff8a65"),
                12.0,
                geo,
                mapper,
                self._actor_opacity(state, viewport_state, focus_state, geo),
            )
            all_ids.discard(actor_id)

        for actor_id, state in vehicle_states.items():
            vehicle_outline = vehicle_outline_for_profile(state.profile_label)
            center_point, shape_path, hitbox_polygon = build_actor_geometry(state, vehicle_outline, geo, mapper)
            sprite_pixmap, sprite_width_m, sprite_length_m = resolve_actor_sprite(state)
            sprite_size_scene = fit_sprite_size_scene(sprite_pixmap, sprite_width_m * pixels_per_meter_mean, sprite_length_m * pixels_per_meter_mean)
            vehicle_color = vehicle_color_hex_by_id.get(actor_id, "#80cbc4")
            item = self.actor_items.get(actor_id)
            if item is None:
                item = SurfaceActorGraphicsItem(actor_id, "vehicle", QColor(vehicle_color))
                item.tooltip_requested.connect(self.tooltip_requested.emit)
                item.tooltip_hidden.connect(self.tooltip_hidden.emit)
                self.actor_items[actor_id] = item
                self.scene.addItem(item)
            item.update_render(
                center_point,
                shape_path,
                hitbox_polygon,
                state.callsign,
                state.profile_label,
                state.heading_deg,
                selected_for_follow=False,
                in_conflict=(actor_id in vehicle_conflicts),
                highlight_radius_scene=0.0,
                show_visibility_halo=False,
                sensor_band_radii_scene=(0.0, 0.0, 0.0),
                sensor_level="clear",
                sprite_pixmap=sprite_pixmap,
                sprite_size_scene=sprite_size_scene,
                tooltip_text=build_actor_tooltip(state, "vehicle"),
            )
            opacity = self._actor_opacity(state, viewport_state, focus_state, geo)
            item.setOpacity(opacity)
            self._render_routes(
                actor_id,
                vehicle_predictions.get(actor_id, ()),
                branch_conflicts,
                QColor(vehicle_color),
                QColor("#ffb36b"),
                11.0,
                geo,
                mapper,
                opacity,
            )
            all_ids.discard(actor_id)

        for stale_id in all_ids:
            self._remove_actor(stale_id)

    def frame_aircraft_cluster(self, aircraft: list[AircraftState], mapper: WebMercatorMapper) -> None:
        if not aircraft:
            return
        points = [QPointF(*mapper.geo_to_pixel(track.latitude, track.longitude)) for track in aircraft]
        min_x = min(point.x() for point in points)
        max_x = max(point.x() for point in points)
        min_y = min(point.y() for point in points)
        max_y = max(point.y() for point in points)
        margin = 180.0
        target_rect = QRectF(min_x - margin, min_y - margin, max((max_x - min_x) + (margin * 2.0), 320.0), max((max_y - min_y) + (margin * 2.0), 320.0))
        self.radar_view.fitInView(target_rect, Qt.AspectRatioMode.KeepAspectRatio)

    def frame_global_scene(self) -> None:
        self.radar_view.set_cockpit_navigation(False)
        self.radar_view.set_view_rotation(0.0)
        self.radar_view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def apply_viewport(self, viewport_state: ViewportFrameState, focus_state: RenderState | None, mapper: WebMercatorMapper | None) -> None:
        if viewport_state.mode == "tower" or focus_state is None or mapper is None:
            self.radar_view.set_cockpit_navigation(False)
            return
        pixel_x, pixel_y = mapper.geo_to_pixel(focus_state.latitude, focus_state.longitude)
        self.radar_view.set_cockpit_navigation(True, focus_state.heading_deg)
        self.radar_view.smooth_camera_to(QPointF(pixel_x, pixel_y), viewport_state.target_zoom, viewport_state.target_rotation_deg)

    def _render_routes(
        self,
        actor_id: str,
        predictions,
        branch_conflicts: set[tuple[str, int]],
        base_color: QColor,
        conflict_color: QColor,
        z_value: float,
        geo,
        mapper: WebMercatorMapper,
        visibility_opacity: float,
    ) -> None:
        route_items = self.route_items.get(actor_id, [])
        route_signatures = self.route_signatures.get(actor_id, [])
        while len(route_items) < len(predictions):
            route_item = QGraphicsPathItem()
            route_item.setZValue(z_value)
            route_items.append(route_item)
            route_signatures.append(None)
            self.scene.addItem(route_item)
        while len(route_items) > len(predictions):
            route_item = route_items.pop()
            self.scene.removeItem(route_item)
            route_signatures.pop()
        self.route_items[actor_id] = route_items
        self.route_signatures[actor_id] = route_signatures
        for branch_index, branch in enumerate(predictions):
            route_item = route_items[branch_index]
            route_signature = tuple(branch.local_polyline)
            route_item.setPen(
                QPen(
                    conflict_color if (actor_id, branch_index) in branch_conflicts else base_color,
                    1.6 if (actor_id, branch_index) in branch_conflicts else 1.0,
                    Qt.PenStyle.DashLine,
                )
            )
            route_item.setOpacity((0.95 if (actor_id, branch_index) in branch_conflicts else 0.42) * visibility_opacity)
            if route_signatures[branch_index] != route_signature:
                route_item.setPath(build_scene_path_from_local_polyline(branch.local_polyline, geo, mapper))
                route_signatures[branch_index] = route_signature

    def _actor_opacity(self, candidate_state: RenderState, viewport_state: ViewportFrameState, focus_state: RenderState | None, geo) -> float:
        if viewport_state.mode != "cockpit" or focus_state is None:
            return 1.0
        if candidate_state.actor_type != "aircraft":
            if is_ground_contact_relevant_to_focus(viewport_state, geo, focus_state, candidate_state):
                return 1.0
            return 0.34
        if is_relevant_to_focus(viewport_state, geo, focus_state, candidate_state):
            return 1.0
        return 0.16

    def _obstacle_opacity(self, obstacle: StaticObstacle, viewport_state: ViewportFrameState, focus_state: RenderState | None, geo) -> float:
        if viewport_state.mode != "cockpit" or focus_state is None:
            return 1.0
        return 1.0 if is_obstacle_relevant_to_focus(
            viewport_state,
            geo,
            focus_state,
            obstacle.latitude,
            obstacle.longitude,
            obstacle.hazard_radius_m,
        ) else 0.34

    def _remove_actor(self, actor_id: str) -> None:
        item = self.actor_items.pop(actor_id, None)
        if item is not None:
            self.scene.removeItem(item)
        routes = self.route_items.pop(actor_id, None)
        self.route_signatures.pop(actor_id, None)
        if routes is not None:
            for route in routes:
                self.scene.removeItem(route)


__all__ = ["RadarSceneController"]