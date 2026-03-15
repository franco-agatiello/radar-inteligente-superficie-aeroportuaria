from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform, QWheelEvent
from PySide6.QtWidgets import QFrame, QGraphicsItem, QGraphicsObject, QGraphicsScene, QGraphicsView

from models import StaticObstacle, clamp


SENSOR_RING_COMPACT_FACTOR = 0.22
SENSOR_RING_MIN_PADDING = 3.0
SENSOR_RING_SIZE_OFFSET_FACTOR = 0.18
COCKPIT_ZOOM_MIN_FACTOR = 0.55
COCKPIT_ZOOM_MAX_FACTOR = 2.35

PARKING_SENSOR_RING_STYLES = {
    "green": {
        "inactive": (QColor(76, 217, 100, 52), 1.0),
        "active": (QColor(76, 217, 100, 120), 1.8),
    },
    "yellow": {
        "inactive": (QColor(255, 159, 10, 74), 1.2),
        "active": (QColor(255, 159, 10, 168), 2.4),
    },
    "red": {
        "inactive": (QColor(255, 59, 48, 98), 1.5),
        "active": (QColor(255, 59, 48, 228), 3.0),
    },
}


class SurfaceActorGraphicsItem(QGraphicsObject):
    clicked = Signal(str)
    tooltip_requested = Signal(object, str)
    tooltip_hidden = Signal()

    def __init__(self, actor_id: str, actor_type: str, color: QColor) -> None:
        super().__init__()
        self.actor_id = actor_id
        self.actor_type = actor_type
        self.color = color
        self.callsign = actor_id
        self.profile_label = ""
        self.relative_shape = QPainterPath()
        self.relative_hitbox = QPolygonF()
        self.in_conflict = False
        self.selected_for_follow = False
        self.highlight_radius_scene = 0.0
        self.show_visibility_halo = False
        self.sensor_band_radii_scene = (0.0, 0.0, 0.0)
        self.sensor_level = "clear"
        self.heading_deg = 0.0
        self.sprite_pixmap: QPixmap | None = None
        self.sprite_size_scene = (0.0, 0.0)
        self._actor_extent_radius_value = 12.0
        self._bounding_rect = QRectF(-40, -40, 140, 100)
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton if actor_type == "aircraft" else Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(True)
        self.setZValue(30.0 if actor_type == "aircraft" else 24.0)

    def update_render(
        self,
        center_point: QPointF,
        shape_path: QPainterPath,
        hitbox_polygon: QPolygonF,
        callsign: str,
        profile_label: str,
        heading_deg: float,
        selected_for_follow: bool,
        in_conflict: bool,
        highlight_radius_scene: float,
        show_visibility_halo: bool,
        sensor_band_radii_scene: tuple[float, float, float],
        sensor_level: str,
        sprite_pixmap: QPixmap | None,
        sprite_size_scene: tuple[float, float],
        tooltip_text: str,
    ) -> None:
        self.prepareGeometryChange()
        self.setPos(center_point)
        self.relative_shape = shape_path
        self.relative_hitbox = hitbox_polygon
        self.callsign = callsign
        self.profile_label = profile_label
        self.heading_deg = heading_deg
        self.selected_for_follow = selected_for_follow
        self.in_conflict = in_conflict
        self.highlight_radius_scene = highlight_radius_scene
        self.show_visibility_halo = show_visibility_halo
        self.sensor_band_radii_scene = sensor_band_radii_scene
        self.sensor_level = sensor_level
        self.sprite_pixmap = sprite_pixmap
        self.sprite_size_scene = sprite_size_scene
        sensor_padding = max(sensor_band_radii_scene) if sensor_band_radii_scene else 0.0
        halo_padding = max(highlight_radius_scene, sensor_padding) + 8.0 if show_visibility_halo or sensor_padding > 0.0 else 12.0
        bounds = shape_path.boundingRect().united(hitbox_polygon.boundingRect())
        sprite_width_scene, sprite_length_scene = sprite_size_scene
        self._actor_extent_radius_value = max(
            bounds.width(),
            bounds.height(),
            sprite_width_scene,
            sprite_length_scene,
            12.0,
        ) / 2.0
        if sprite_width_scene > 0.0 and sprite_length_scene > 0.0:
            sprite_rect = QTransform().rotate(heading_deg).mapRect(
                QRectF(-sprite_width_scene / 2.0, -sprite_length_scene / 2.0, sprite_width_scene, sprite_length_scene)
            )
            bounds = bounds.united(sprite_rect)
        new_bounding_rect = bounds.adjusted(-halo_padding, -halo_padding, halo_padding, halo_padding)
        if new_bounding_rect != self._bounding_rect:
            self.prepareGeometryChange()
            self._bounding_rect = new_bounding_rect
        if tooltip_text != self.toolTip():
            self.setToolTip(tooltip_text)
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounding_rect

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[override]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        green_radius, yellow_radius, red_radius = self.sensor_band_radii_scene
        if green_radius > 0.0:
            self._paint_sensor_ring(painter, green_radius, "green")
        if yellow_radius > 0.0:
            self._paint_sensor_ring(painter, yellow_radius, "yellow")
        if red_radius > 0.0:
            self._paint_sensor_ring(painter, red_radius, "red")
        if self.show_visibility_halo and self.highlight_radius_scene > 0.0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(124, 227, 255, 36))
            painter.drawEllipse(QPointF(0.0, 0.0), self.highlight_radius_scene * 1.55, self.highlight_radius_scene * 1.55)
            painter.setBrush(QColor(124, 227, 255, 72))
            painter.drawEllipse(QPointF(0.0, 0.0), self.highlight_radius_scene, self.highlight_radius_scene)
        if self.in_conflict or self.selected_for_follow:
            painter.setPen(QPen(QColor("#ff3b30") if self.in_conflict else QColor("#7ce3ff"), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            focus_radius = self._focus_outline_radius()
            painter.drawEllipse(QPointF(0.0, 0.0), focus_radius, focus_radius)
        if self.sprite_pixmap is not None and not self.sprite_pixmap.isNull() and self.sprite_size_scene[0] > 0.0 and self.sprite_size_scene[1] > 0.0:
            painter.save()
            painter.rotate(self.heading_deg)
            sprite_width_scene = max(1, int(round(self.sprite_size_scene[0])))
            sprite_length_scene = max(1, int(round(self.sprite_size_scene[1])))
            painter.drawPixmap(-sprite_width_scene // 2, -sprite_length_scene // 2, sprite_width_scene, sprite_length_scene, self.sprite_pixmap)
            painter.restore()
        else:
            painter.setPen(QPen(QColor("#eafcff"), 1.0))
            painter.setBrush(QColor("#ff6f61") if self.in_conflict else self.color)
            painter.drawPath(self.relative_shape)
        if self.selected_for_follow:
            painter.setPen(QPen(QColor("#7ce3ff"), 1.8))
            painter.drawEllipse(QRectF(-self.highlight_radius_scene, -self.highlight_radius_scene, self.highlight_radius_scene * 2.0, self.highlight_radius_scene * 2.0))

    def _paint_sensor_ring(self, painter: QPainter, radius_scene: float, level: str) -> None:
        if self.actor_type != "aircraft" or radius_scene <= 0.0:
            return
        compact_radius = self._compact_sensor_radius(radius_scene)
        ring_palette = PARKING_SENSOR_RING_STYLES.get(level, PARKING_SENSOR_RING_STYLES["green"])
        color, width = ring_palette["active" if self.sensor_level == level else "inactive"]
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, width))
        painter.drawEllipse(QPointF(0.0, 0.0), compact_radius, compact_radius)
        if self.sensor_level == level:
            glow_color = QColor(color)
            glow_color.setAlpha(max(36, color.alpha() // 2))
            painter.setPen(QPen(glow_color, width + 2.2))
            painter.drawEllipse(QPointF(0.0, 0.0), compact_radius, compact_radius)
        painter.restore()

    def _compact_sensor_radius(self, radius_scene: float) -> float:
        actor_extent = self._actor_extent_radius()
        size_padding = max(SENSOR_RING_MIN_PADDING, actor_extent * SENSOR_RING_SIZE_OFFSET_FACTOR)
        return actor_extent + size_padding + (radius_scene * SENSOR_RING_COMPACT_FACTOR)

    def _focus_outline_radius(self) -> float:
        return self._actor_extent_radius() + 5.0

    def _actor_extent_radius(self) -> float:
        return self._actor_extent_radius_value

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self.actor_type == "aircraft":
            self.clicked.emit(self.actor_id)
        event.accept()

    def hoverMoveEvent(self, event) -> None:  # type: ignore[override]
        self.tooltip_requested.emit(event.scenePos(), self.toolTip())
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.tooltip_hidden.emit()
        super().hoverLeaveEvent(event)


class SurfaceObstacleGraphicsItem(QGraphicsObject):
    tooltip_requested = Signal(object, str)
    tooltip_hidden = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.obstacle: StaticObstacle | None = None
        self.display_radius_scene = 2.0
        self._bounding_rect = QRectF(-28, -34, 84, 72)
        self.setZValue(18.0)
        self.setAcceptHoverEvents(True)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def update_render(self, center_point: QPointF, obstacle: StaticObstacle, display_radius_scene: float, tooltip_text: str) -> None:
        self.setPos(center_point)
        self.obstacle = obstacle
        self.display_radius_scene = display_radius_scene
        new_bounding_rect = QRectF(-display_radius_scene * 3.8, -display_radius_scene * 4.6, display_radius_scene * 7.6 + 52.0, display_radius_scene * 8.2 + 24.0)
        if new_bounding_rect != self._bounding_rect:
            self.prepareGeometryChange()
            self._bounding_rect = new_bounding_rect
        if tooltip_text != self.toolTip():
            self.setToolTip(tooltip_text)
        self.update()

    def boundingRect(self) -> QRectF:
        return self._bounding_rect

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[override]
        if self.obstacle is None:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base_radius = self.display_radius_scene
        if self.obstacle.is_wildlife:
            halo_color = QColor(255, 183, 77, 76) if not self.obstacle.in_conflict else QColor(255, 59, 48, 96)
            fill_color = QColor("#8d6e63") if not self.obstacle.in_conflict else QColor("#ff3b30")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo_color)
            painter.drawEllipse(QRectF(-base_radius * 2.9, -base_radius * 2.9, base_radius * 5.8, base_radius * 5.8))
            painter.setPen(QPen(QColor("#ffe0b2"), 1.2))
            painter.setBrush(fill_color)
            painter.drawEllipse(QRectF(-base_radius, -base_radius * 0.7, base_radius * 2.0, base_radius * 1.4))
            painter.drawEllipse(QRectF(-base_radius * 2.1, -base_radius * 1.9, base_radius * 1.05, base_radius * 1.5))
            painter.drawEllipse(QRectF(-base_radius * 0.7, -base_radius * 2.25, base_radius * 1.05, base_radius * 1.5))
            painter.drawEllipse(QRectF(base_radius * 0.55, -base_radius * 1.9, base_radius * 1.05, base_radius * 1.5))
            painter.drawEllipse(QRectF(base_radius * 1.7, -base_radius * 0.7, base_radius * 1.05, base_radius * 1.5))
            painter.setBrush(QColor("#2e7d32") if not self.obstacle.in_conflict else QColor("#b71c1c"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QRectF(-base_radius * 0.28, -base_radius * 0.28, base_radius * 0.56, base_radius * 0.56))
        else:
            halo_color = QColor(79, 195, 247, 70) if not self.obstacle.in_conflict else QColor(255, 59, 48, 96)
            fill_color = QColor("#cfd8dc") if not self.obstacle.in_conflict else QColor("#ff3b30")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo_color)
            painter.drawEllipse(QRectF(-base_radius * 2.6, -base_radius * 2.6, base_radius * 5.2, base_radius * 5.2))
            painter.setPen(QPen(QColor("#e8f6ff"), 1.2))
            painter.setBrush(fill_color)
            diamond = QPolygonF([QPointF(0.0, -base_radius * 1.7), QPointF(base_radius * 1.35, 0.0), QPointF(0.0, base_radius * 1.7), QPointF(-base_radius * 1.35, 0.0)])
            painter.drawPolygon(diamond)
            painter.setBrush(QColor("#1565c0") if not self.obstacle.in_conflict else QColor("#ff8a80"))
            painter.drawEllipse(QRectF(-base_radius * 0.35, -base_radius * 0.35, base_radius * 0.7, base_radius * 0.7))

    def hoverMoveEvent(self, event) -> None:  # type: ignore[override]
        self.tooltip_requested.emit(event.scenePos(), self.toolTip())
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # type: ignore[override]
        self.tooltip_hidden.emit()
        super().hoverLeaveEvent(event)


class SurfaceRadarView(QGraphicsView):
    def __init__(self, scene: QGraphicsScene, parent=None) -> None:
        super().__init__(scene, parent)
        self._zoom_factor = 1.0
        self._rotation_deg = 0.0
        self._cockpit_mode = False
        self._cockpit_zoom_factor = 1.0
        self._compass_heading_deg = 0.0
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setOptimizationFlags(QGraphicsView.OptimizationFlag.DontSavePainterState | QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("background-color: #121212; border: 1px solid #2a2a2a;")
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._apply_transform()

    def wheelEvent(self, event: QWheelEvent) -> None:
        zoom_step = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
        if self._cockpit_mode:
            self._cockpit_zoom_factor = clamp(self._cockpit_zoom_factor * zoom_step, COCKPIT_ZOOM_MIN_FACTOR, COCKPIT_ZOOM_MAX_FACTOR)
            self.viewport().update()
            event.accept()
            return
        old_scene_pos = self.mapToScene(event.position().toPoint())
        self._zoom_factor = clamp(self._zoom_factor * zoom_step, 0.35, 9.0)
        self._apply_transform()
        new_scene_pos = self.mapToScene(event.position().toPoint())
        delta = old_scene_pos - new_scene_pos
        self.centerOn(self.mapToScene(self.viewport().rect().center()) + delta)
        event.accept()

    def _apply_transform(self) -> None:
        transform = QTransform()
        transform.scale(self._zoom_factor, self._zoom_factor)
        transform.rotate(self._rotation_deg)
        self.setTransform(transform)

    def set_zoom_factor(self, zoom_factor: float) -> None:
        self._zoom_factor = clamp(zoom_factor, 0.35, 9.0)
        self._apply_transform()

    def zoom_factor(self) -> float:
        return self._zoom_factor

    def set_view_rotation(self, rotation_deg: float) -> None:
        self._rotation_deg = rotation_deg
        self._apply_transform()

    def rotation_deg(self) -> float:
        return self._rotation_deg

    def smooth_camera_to(self, center_point: QPointF, zoom_factor: float, rotation_deg: float, easing: float = 0.18) -> None:
        target_zoom = clamp(zoom_factor * (self._cockpit_zoom_factor if self._cockpit_mode else 1.0), 0.35, 9.0)
        zoom_delta = target_zoom - self._zoom_factor
        self._zoom_factor += zoom_delta * easing
        rotation_delta = ((rotation_deg - self._rotation_deg + 180.0) % 360.0) - 180.0
        self._rotation_deg += rotation_delta * easing
        self._apply_transform()
        scene_center = self.mapToScene(self.viewport().rect().center())
        delta = center_point - scene_center
        self.centerOn(scene_center + (delta * easing))

    def reset_camera(self) -> None:
        self._rotation_deg = 0.0
        self._zoom_factor = 1.0
        self._cockpit_zoom_factor = 1.0
        self._cockpit_mode = False
        self._compass_heading_deg = 0.0
        self._apply_transform()

    def set_cockpit_navigation(self, enabled: bool, heading_deg: float = 0.0) -> None:
        self._cockpit_mode = enabled
        self._compass_heading_deg = heading_deg % 360.0
        if not enabled:
            self._cockpit_zoom_factor = 1.0
        self.viewport().update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if not self._cockpit_mode:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_compass_overlay(painter)
        painter.end()

    def _paint_compass_overlay(self, painter: QPainter) -> None:
        margin = 18.0
        diameter = 112.0
        x_pos = self.viewport().width() - diameter - margin
        y_pos = margin
        compass_rect = QRectF(x_pos, y_pos, diameter, diameter)
        center = compass_rect.center()
        radius = diameter / 2.0

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(8, 14, 18, 172))
        painter.drawEllipse(compass_rect)
        painter.setPen(QPen(QColor("#7ce3ff"), 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(compass_rect.adjusted(2.0, 2.0, -2.0, -2.0))

        heading_rad = math.radians(self._compass_heading_deg)
        for angle_deg in range(0, 360, 30):
            relative_rad = math.radians(angle_deg) - heading_rad - (math.pi / 2.0)
            outer_point = QPointF(center.x() + math.cos(relative_rad) * (radius - 8.0), center.y() + math.sin(relative_rad) * (radius - 8.0))
            inner_length = 12.0 if angle_deg % 90 == 0 else 7.0
            inner_point = QPointF(center.x() + math.cos(relative_rad) * (radius - 8.0 - inner_length), center.y() + math.sin(relative_rad) * (radius - 8.0 - inner_length))
            painter.drawLine(inner_point, outer_point)

        painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        for label, base_angle_deg in (("N", 0.0), ("E", 90.0), ("S", 180.0), ("W", 270.0)):
            relative_rad = math.radians(base_angle_deg) - heading_rad - (math.pi / 2.0)
            label_center = QPointF(center.x() + math.cos(relative_rad) * (radius - 22.0), center.y() + math.sin(relative_rad) * (radius - 22.0))
            label_rect = QRectF(label_center.x() - 10.0, label_center.y() - 9.0, 20.0, 18.0)
            painter.setPen(QColor("#f4fbff") if label == "N" else QColor("#9cc7d4"))
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffd54a"))
        heading_bug = QPolygonF(
            [
                QPointF(center.x(), y_pos + 8.0),
                QPointF(center.x() - 7.0, y_pos + 20.0),
                QPointF(center.x() + 7.0, y_pos + 20.0),
            ]
        )
        painter.drawPolygon(heading_bug)

        painter.setPen(QColor("#d7f6ff"))
        painter.setFont(QFont("Consolas", 11, QFont.Weight.DemiBold))
        painter.drawText(QRectF(x_pos + 18.0, y_pos + diameter - 34.0, diameter - 36.0, 20.0), Qt.AlignmentFlag.AlignCenter, f"HDG {self._compass_heading_deg:03.0f}")
        painter.setFont(QFont("Consolas", 9))
        painter.drawText(QRectF(x_pos + 18.0, y_pos + diameter - 18.0, diameter - 36.0, 16.0), Qt.AlignmentFlag.AlignCenter, f"ZOOM x{self._cockpit_zoom_factor:.2f}")