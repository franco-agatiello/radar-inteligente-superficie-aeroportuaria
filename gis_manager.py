from __future__ import annotations

import io
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import requests
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from asmgcs.physics.zone_tuning import ZoneTuningRepository, ZoneTuningRule
from models import (
    AirportConfig,
    GeoReference,
    ProjectedPath,
    REQUEST_TIMEOUT_SECONDS,
    TILE_SIZE,
    WebMercatorMapper,
    clamp,
    heading_diff_deg,
    project_linear,
    segment_heading_deg,
)


HTTP_USER_AGENT = "A-SMGCS-HMI/4.0"
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PACKAGED_AEROWAY_DIR = Path(__file__).resolve().parent / "data" / "airport_layouts"
DEFAULT_AEROWAY_WIDTH_M = {"runway": 46.0, "taxiway": 24.0, "apron": 70.0}
DEFAULT_EDGE_CLEARANCE_MARGIN_M = {"runway": 7.5, "taxiway": 4.5, "apron": 6.0}


@dataclass(frozen=True, slots=True)
class RoutingSegment:
    start_node: str
    end_node: str
    start_xy: tuple[float, float]
    end_xy: tuple[float, float]
    length_m: float
    heading_deg: float
    aeroway: str
    usable_width_m: float


@dataclass(frozen=True, slots=True)
class GISLabel:
    text: str
    local_xy: tuple[float, float]
    rotation_deg: float
    aeroway: str


@dataclass(slots=True)
class GISContext:
    drivable_area: Polygon | MultiPolygon
    sector_areas: dict[str, Polygon | MultiPolygon]
    zone_tuning: dict[str, ZoneTuningRule]
    graph: nx.DiGraph
    routing_segments: list[RoutingSegment]
    labels: list[GISLabel]
    map_pixmap: QPixmap
    map_status: str
    gis_status: str
    pixels_per_meter_x: float
    pixels_per_meter_y: float
    pixels_per_meter_mean: float
    local_bounds: tuple[float, float, float, float]


class GISManager:
    """Loads map imagery and OSM aeroway geometry for spatially aware simulation."""

    def __init__(
        self,
        airport: AirportConfig,
        geo: GeoReference,
        mapper: WebMercatorMapper,
        zone_tuning_repository: ZoneTuningRepository | None = None,
    ) -> None:
        self.airport = airport
        self.geo = geo
        self.mapper = mapper
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": HTTP_USER_AGENT})
        self.cache_root = Path(__file__).resolve().parent / ".cache" / self.airport.code.lower()
        self.tile_cache_dir = self.cache_root / "tiles"
        self.overpass_cache_path = self.cache_root / "overpass_aeroway.json"
        self.packaged_overpass_path = PACKAGED_AEROWAY_DIR / f"{self.airport.code.lower()}_overpass_aeroway.json"
        self.zone_tuning_repository = zone_tuning_repository or ZoneTuningRepository(Path(__file__).resolve().parent / "data" / "zone_tuning.json")
        self.tile_cache_dir.mkdir(parents=True, exist_ok=True)
        self._tile_cache_hits = 0
        self._tile_network_fetches = 0

    def load_context(self) -> GISContext:
        map_pixmap, map_status = self._fetch_map_pixmap()
        try:
            drivable_area, sector_areas, graph, routing_segments, labels, gis_source = self._query_overpass_and_build()
            gis_status = f"{self.airport.code} aeroway graph loaded ({gis_source})"
        except requests.RequestException as exc:
            drivable_area, sector_areas, graph, routing_segments = self._build_fallback_context()
            labels = []
            gis_status = f"{self.airport.code} Overpass degraded: {exc} | using fallback geometry"
        drivable_area, sector_areas, zone_tuning = self._apply_zone_tuning(drivable_area, sector_areas)
        if any(abs(rule.buffer_m) > 0.01 for rule in zone_tuning.values()):
            gis_status += " | zone polish active"

        min_x, max_x, min_y, max_y = self._local_bounds_from_airport()
        pixels_per_meter_x = self.mapper.pixel_width / max(max_x - min_x, 1.0)
        pixels_per_meter_y = self.mapper.pixel_height / max(max_y - min_y, 1.0)
        return GISContext(
            drivable_area=drivable_area,
            sector_areas=sector_areas,
            zone_tuning=zone_tuning,
            graph=graph,
            routing_segments=routing_segments,
            labels=labels,
            map_pixmap=map_pixmap,
            map_status=map_status,
            gis_status=gis_status,
            pixels_per_meter_x=pixels_per_meter_x,
            pixels_per_meter_y=pixels_per_meter_y,
            pixels_per_meter_mean=(pixels_per_meter_x + pixels_per_meter_y) / 2.0,
            local_bounds=(min_x, max_x, min_y, max_y),
        )

    def sample_point_within_drivable_area(
        self,
        drivable_area: Polygon | MultiPolygon,
        rng: random.Random,
    ) -> tuple[float, float, float, float]:
        # Shapely containment is the hard spatial gate here. Rejection sampling draws
        # points across the airport bounds until Point(x, y).contains() is true, which
        # guarantees obstacles spawn only inside runway/taxiway/apron geometry.
        min_x, min_y, max_x, max_y = drivable_area.bounds
        for _ in range(500):
            x_m = rng.uniform(min_x, max_x)
            y_m = rng.uniform(min_y, max_y)
            if drivable_area.contains(Point(x_m, y_m)):
                latitude, longitude = self.geo.to_geodetic(x_m, y_m)
                return latitude, longitude, x_m, y_m

        representative = drivable_area.representative_point()
        latitude, longitude = self.geo.to_geodetic(representative.x, representative.y)
        return latitude, longitude, representative.x, representative.y

    def sample_graph_position(
        self,
        routing_segments: list[RoutingSegment],
        rng: random.Random,
    ) -> tuple[float, float, float, float, float]:
        if not routing_segments:
            latitude, longitude, x_m, y_m = self.sample_point_within_drivable_area(self._build_bbox_polygon(), rng)
            return latitude, longitude, x_m, y_m, rng.uniform(0.0, 360.0)

        segment = rng.choice(routing_segments)
        t_value = rng.uniform(0.0, 1.0)
        x_m = segment.start_xy[0] + ((segment.end_xy[0] - segment.start_xy[0]) * t_value)
        y_m = segment.start_xy[1] + ((segment.end_xy[1] - segment.start_xy[1]) * t_value)
        latitude, longitude = self.geo.to_geodetic(x_m, y_m)
        if rng.random() < 0.5:
            heading_deg = segment.heading_deg
        else:
            heading_deg = (segment.heading_deg + 180.0) % 360.0
        return latitude, longitude, x_m, y_m, heading_deg

    def project_along_graph(
        self,
        graph: nx.DiGraph,
        routing_segments: list[RoutingSegment],
        latitude: float,
        longitude: float,
        heading_deg: float,
        distance_m: float,
        rng: random.Random | None = None,
    ) -> ProjectedPath:
        if graph.number_of_edges() == 0 or not routing_segments:
            final_latitude, final_longitude = project_linear(latitude, longitude, heading_deg, distance_m, 1.0)
            start_xy = self.geo.to_local_xy(latitude, longitude)
            end_xy = self.geo.to_local_xy(final_latitude, final_longitude)
            return ProjectedPath(final_latitude, final_longitude, heading_deg, [start_xy, end_xy])

        point_xy = self.geo.to_local_xy(latitude, longitude)
        segment, projection_xy, t_value, travel_forward = self._nearest_directed_segment(point_xy, heading_deg, routing_segments)
        if travel_forward:
            current_node = segment.end_node
            next_xy = segment.end_xy
            previous_node = segment.start_node
            current_heading = segment.heading_deg
            remaining_on_segment = segment.length_m * (1.0 - t_value)
        else:
            current_node = segment.start_node
            next_xy = segment.start_xy
            previous_node = segment.end_node
            current_heading = (segment.heading_deg + 180.0) % 360.0
            remaining_on_segment = segment.length_m * t_value

        polyline: list[tuple[float, float]] = [projection_xy]
        if distance_m <= remaining_on_segment:
            final_xy = self._interpolate_xy(projection_xy, next_xy, distance_m / max(remaining_on_segment, 1e-6))
            polyline.append(final_xy)
            final_latitude, final_longitude = self.geo.to_geodetic(final_xy[0], final_xy[1])
            return ProjectedPath(final_latitude, final_longitude, current_heading, polyline)

        distance_left = distance_m - remaining_on_segment
        polyline.append(next_xy)
        current_xy = next_xy

        # The graph projection walks edge-by-edge along the aeroway network. At each
        # node it chooses the outgoing edge whose heading is closest to the current
        # travel heading, which makes the T+15 prediction bend along taxiway curves
        # instead of cutting straight across non-drivable terrain.
        for _ in range(128):
            outgoing_edges = list(graph.out_edges(current_node, data=True))
            if not outgoing_edges:
                break

            def edge_score(edge: tuple[str, str, dict]) -> float:
                _, next_node, data = edge
                reverse_penalty = 40.0 if next_node == previous_node and len(outgoing_edges) > 1 else 0.0
                random_bias = rng.uniform(0.0, 3.0) if rng is not None else 0.0
                return heading_diff_deg(current_heading, data["heading_deg"]) + reverse_penalty + random_bias

            _, next_node, data = min(outgoing_edges, key=edge_score)
            start_xy = (graph.nodes[current_node]["x"], graph.nodes[current_node]["y"])
            end_xy = (graph.nodes[next_node]["x"], graph.nodes[next_node]["y"])
            edge_length = float(data["length_m"])

            if distance_left <= edge_length:
                final_xy = self._interpolate_xy(start_xy, end_xy, distance_left / max(edge_length, 1e-6))
                polyline.append(final_xy)
                final_latitude, final_longitude = self.geo.to_geodetic(final_xy[0], final_xy[1])
                return ProjectedPath(final_latitude, final_longitude, float(data["heading_deg"]), polyline)

            distance_left -= edge_length
            polyline.append(end_xy)
            previous_node = current_node
            current_node = next_node
            current_xy = end_xy
            current_heading = float(data["heading_deg"])

        final_latitude, final_longitude = self.geo.to_geodetic(current_xy[0], current_xy[1])
        return ProjectedPath(final_latitude, final_longitude, current_heading, polyline)

    def project_branches_along_graph(
        self,
        graph: nx.DiGraph,
        routing_segments: list[RoutingSegment],
        latitude: float,
        longitude: float,
        heading_deg: float,
        distance_m: float,
        speed_mps: float = 0.0,
        actor_type: str = "aircraft",
        actor_width_m: float = 0.0,
        max_branch_count: int = 12,
    ) -> list[ProjectedPath]:
        if graph.number_of_edges() == 0 or not routing_segments:
            final_latitude, final_longitude = project_linear(latitude, longitude, heading_deg, distance_m, 1.0)
            start_xy = self.geo.to_local_xy(latitude, longitude)
            end_xy = self.geo.to_local_xy(final_latitude, final_longitude)
            return [ProjectedPath(final_latitude, final_longitude, heading_deg, [start_xy, end_xy], branch_id="B1")]

        point_xy = self.geo.to_local_xy(latitude, longitude)
        segment, projection_xy, t_value, travel_forward = self._nearest_directed_segment(point_xy, heading_deg, routing_segments)
        if travel_forward:
            current_node = segment.end_node
            next_xy = segment.end_xy
            previous_node = segment.start_node
            current_heading = segment.heading_deg
            remaining_on_segment = segment.length_m * (1.0 - t_value)
        else:
            current_node = segment.start_node
            next_xy = segment.start_xy
            previous_node = segment.end_node
            current_heading = (segment.heading_deg + 180.0) % 360.0
            remaining_on_segment = segment.length_m * t_value

        if distance_m <= remaining_on_segment:
            final_xy = self._interpolate_xy(projection_xy, next_xy, distance_m / max(remaining_on_segment, 1e-6))
            final_latitude, final_longitude = self.geo.to_geodetic(final_xy[0], final_xy[1])
            return [ProjectedPath(final_latitude, final_longitude, current_heading, [projection_xy, final_xy], branch_id="B1")]

        distance_left = distance_m - remaining_on_segment
        base_polyline = [projection_xy, next_xy]
        branches = self._walk_branch_paths(
            graph,
            current_node,
            previous_node,
            current_heading,
            distance_left,
            base_polyline,
            speed_mps=speed_mps,
            actor_type=actor_type,
            actor_width_m=actor_width_m,
            max_branch_count=max_branch_count,
            depth_remaining=24,
        )
        if not branches:
            final_latitude, final_longitude = self.geo.to_geodetic(next_xy[0], next_xy[1])
            return [ProjectedPath(final_latitude, final_longitude, current_heading, base_polyline, branch_id="B1")]
        return branches

    def _fetch_map_pixmap(self) -> tuple[QPixmap, str]:
        start_tile_x = int(math.floor(self.mapper.origin_global_x / TILE_SIZE))
        end_tile_x = int(math.floor((self.mapper.end_global_x - 1.0) / TILE_SIZE))
        start_tile_y = int(math.floor(self.mapper.origin_global_y / TILE_SIZE))
        end_tile_y = int(math.floor((self.mapper.end_global_y - 1.0) / TILE_SIZE))

        stitched_width = (end_tile_x - start_tile_x + 1) * TILE_SIZE
        stitched_height = (end_tile_y - start_tile_y + 1) * TILE_SIZE
        stitched = QImage(stitched_width, stitched_height, QImage.Format.Format_ARGB32)
        stitched.fill(QColor("#0f1418"))

        failed_tiles = 0
        painter = QPainter(stitched)
        try:
            for tile_y in range(start_tile_y, end_tile_y + 1):
                for tile_x in range(start_tile_x, end_tile_x + 1):
                    target_x = (tile_x - start_tile_x) * TILE_SIZE
                    target_y = (tile_y - start_tile_y) * TILE_SIZE
                    try:
                        image = self._fetch_tile(self.airport.map_zoom, tile_x, tile_y)
                        painter.drawImage(target_x, target_y, image)
                    except requests.RequestException:
                        failed_tiles += 1
                        painter.fillRect(target_x, target_y, TILE_SIZE, TILE_SIZE, QColor("#20262b"))
        finally:
            painter.end()

        crop_x = int(round(self.mapper.origin_global_x - (start_tile_x * TILE_SIZE)))
        crop_y = int(round(self.mapper.origin_global_y - (start_tile_y * TILE_SIZE)))
        cropped = stitched.copy(crop_x, crop_y, self.mapper.pixel_width, self.mapper.pixel_height)
        cache_summary = f"cache {self._tile_cache_hits} | live {self._tile_network_fetches}"
        if failed_tiles > 0:
            return QPixmap.fromImage(cropped), f"{self.airport.code} map loaded with {failed_tiles} fallback tile(s) | {cache_summary}"
        return QPixmap.fromImage(cropped), f"{self.airport.code} OSM map loaded | {cache_summary}"

    def _fetch_tile(self, zoom: int, tile_x: int, tile_y: int) -> QImage:
        cache_path = self.tile_cache_dir / str(zoom) / str(tile_x) / f"{tile_y}.png"
        if cache_path.exists():
            image = QImage(str(cache_path))
            if not image.isNull():
                self._tile_cache_hits += 1
                return image
        response = self.session.get(
            OSM_TILE_URL.format(z=zoom, x=tile_x, y=tile_y),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(response.content)
        self._tile_network_fetches += 1
        image = QImage()
        if not image.loadFromData(io.BytesIO(response.content).getvalue()):
            raise requests.RequestException("Tile decode failure")
        return image

    def _query_overpass_and_build(self) -> tuple[Polygon | MultiPolygon, dict[str, Polygon | MultiPolygon], nx.DiGraph, list[RoutingSegment], list[GISLabel], str]:
        query = f"""
        [out:json][timeout:25];
        (
          way[\"aeroway\"~\"runway|taxiway|apron\"]({self.airport.lat_min},{self.airport.lon_min},{self.airport.lat_max},{self.airport.lon_max});
        );
        out geom tags;
        """.strip()
        packaged_payload = self._load_packaged_overpass_payload()
        if packaged_payload is not None:
            payload, source_status = packaged_payload, "packaged aeroway layout"
        else:
            payload, source_status = self._fetch_overpass_payload(query)

        graph = nx.DiGraph()
        routing_segments: list[RoutingSegment] = []
        polygons: list[Polygon] = []
        sector_polygons: dict[str, list[Polygon]] = {"runway": [], "taxiway": [], "apron": []}
        labels: list[GISLabel] = []
        for element in payload.get("elements", []):
            geometry = element.get("geometry") or []
            tags = element.get("tags") or {}
            aeroway = tags.get("aeroway", "")
            if len(geometry) < 2 or aeroway not in DEFAULT_AEROWAY_WIDTH_M:
                continue

            local_coords = [self.geo.to_local_xy(point["lat"], point["lon"]) for point in geometry]
            if aeroway == "apron" and self._is_closed(local_coords) and len(local_coords) >= 4:
                polygon = Polygon(local_coords)
            else:
                width_m = self._width_from_tags(tags, aeroway)
                polygon = LineString(local_coords).buffer(width_m / 2.0, cap_style=2, join_style=2)
            if polygon.is_valid and not polygon.is_empty:
                normalized_polygon = polygon.buffer(0)
                polygons.append(normalized_polygon)
                sector_polygons[aeroway].append(normalized_polygon)

            label = self._build_label(tags, aeroway, local_coords)
            if label is not None:
                labels.append(label)

            if aeroway in {"runway", "taxiway"}:
                self._add_way_to_graph(graph, routing_segments, geometry, local_coords, aeroway, tags)

        drivable_area = unary_union(polygons).buffer(0)
        if drivable_area.is_empty:
            raise requests.RequestException("No drivable OSM aeroway geometry returned")
        sector_areas = {
            name: unary_union(items).buffer(0)
            for name, items in sector_polygons.items()
            if items
        }
        return drivable_area, sector_areas, graph, routing_segments, labels, source_status

    def _apply_zone_tuning(
        self,
        drivable_area: Polygon | MultiPolygon,
        sector_areas: dict[str, Polygon | MultiPolygon],
    ) -> tuple[Polygon | MultiPolygon, dict[str, Polygon | MultiPolygon], dict[str, ZoneTuningRule]]:
        self.zone_tuning_repository.ensure_airport_defaults(self.airport.code)
        zone_tuning = {rule.sector_name: rule for rule in self.zone_tuning_repository.list_rules(self.airport.code)}
        tuned_sector_areas: dict[str, Polygon | MultiPolygon] = {}
        for sector_name, geometry in sector_areas.items():
            tuned_geometry = geometry
            rule = zone_tuning.get(sector_name)
            if rule is not None and abs(rule.buffer_m) > 0.01:
                buffered_geometry = geometry.buffer(rule.buffer_m).buffer(0)
                if not buffered_geometry.is_empty:
                    tuned_geometry = buffered_geometry
            tuned_sector_areas[sector_name] = tuned_geometry
        if tuned_sector_areas:
            tuned_drivable_area = unary_union(list(tuned_sector_areas.values())).buffer(0)
            if not tuned_drivable_area.is_empty:
                drivable_area = tuned_drivable_area
                sector_areas = tuned_sector_areas
        return drivable_area, sector_areas, zone_tuning

    def _load_packaged_overpass_payload(self) -> dict | None:
        if not self.packaged_overpass_path.exists():
            return None
        try:
            return json.loads(self.packaged_overpass_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _fetch_overpass_payload(self, query: str) -> tuple[dict, str]:
        try:
            response = self.session.post(OVERPASS_URL, data={"data": query}, timeout=REQUEST_TIMEOUT_SECONDS + 12.0)
            response.raise_for_status()
            payload = response.json()
            self.cache_root.mkdir(parents=True, exist_ok=True)
            self.overpass_cache_path.write_text(json.dumps(payload), encoding="utf-8")
            return payload, "live Overpass"
        except (requests.RequestException, ValueError) as exc:
            if self.overpass_cache_path.exists():
                try:
                    cached_payload = json.loads(self.overpass_cache_path.read_text(encoding="utf-8"))
                    return cached_payload, "cached Overpass"
                except json.JSONDecodeError:
                    pass
            raise requests.RequestException(str(exc)) from exc

    def _build_fallback_context(self) -> tuple[Polygon, dict[str, Polygon | MultiPolygon], nx.DiGraph, list[RoutingSegment]]:
        bbox_polygon = self._build_bbox_polygon()
        graph = nx.DiGraph()
        routing_segments: list[RoutingSegment] = []
        min_x, min_y, max_x, max_y = bbox_polygon.bounds
        center_x = (min_x + max_x) / 2.0
        center_y = (min_y + max_y) / 2.0
        fallback_lines = [
            [(min_x, center_y), (max_x, center_y)],
            [(center_x, min_y), (center_x, max_y)],
            [(min_x, min_y), (max_x, max_y)],
            [(min_x, max_y), (max_x, min_y)],
        ]
        for index, line in enumerate(fallback_lines):
            way_geometry = [
                {"lat": self.geo.to_geodetic(point[0], point[1])[0], "lon": self.geo.to_geodetic(point[0], point[1])[1]}
                for point in line
            ]
            self._add_way_to_graph(graph, routing_segments, way_geometry, line, "taxiway", {})
        return bbox_polygon, {"taxiway": bbox_polygon}, graph, routing_segments

    def _add_way_to_graph(
        self,
        graph: nx.DiGraph,
        routing_segments: list[RoutingSegment],
        geometry: list[dict],
        local_coords: list[tuple[float, float]],
        aeroway: str,
        tags: dict,
    ) -> None:
        one_way = str(tags.get("oneway", "")).lower() in {"yes", "1", "true"}
        usable_width_m = self._usable_edge_width_m(tags, aeroway)
        for index in range(len(local_coords) - 1):
            start_xy = local_coords[index]
            end_xy = local_coords[index + 1]
            if start_xy == end_xy:
                continue
            start_geo = geometry[index]
            end_geo = geometry[index + 1]
            start_node = f"{start_geo['lat']:.6f},{start_geo['lon']:.6f}"
            end_node = f"{end_geo['lat']:.6f},{end_geo['lon']:.6f}"
            graph.add_node(start_node, x=start_xy[0], y=start_xy[1])
            graph.add_node(end_node, x=end_xy[0], y=end_xy[1])
            length_m = math.hypot(end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
            heading_deg = segment_heading_deg(start_xy, end_xy)
            graph.add_edge(start_node, end_node, length_m=length_m, heading_deg=heading_deg, aeroway=aeroway, usable_width_m=usable_width_m)
            routing_segments.append(RoutingSegment(start_node, end_node, start_xy, end_xy, length_m, heading_deg, aeroway, usable_width_m))
            if not one_way:
                graph.add_edge(end_node, start_node, length_m=length_m, heading_deg=(heading_deg + 180.0) % 360.0, aeroway=aeroway, usable_width_m=usable_width_m)

    def _nearest_directed_segment(
        self,
        point_xy: tuple[float, float],
        heading_deg: float,
        routing_segments: list[RoutingSegment],
    ) -> tuple[RoutingSegment, tuple[float, float], float, bool]:
        best_candidate: tuple[float, float, RoutingSegment, tuple[float, float], float, bool] | None = None
        for segment in routing_segments:
            projection_xy, t_value, distance_to_segment = self._project_point_to_segment(point_xy, segment.start_xy, segment.end_xy)
            forward_diff = heading_diff_deg(heading_deg, segment.heading_deg)
            reverse_diff = heading_diff_deg(heading_deg, (segment.heading_deg + 180.0) % 360.0)
            travel_forward = forward_diff <= reverse_diff
            heading_score = forward_diff if travel_forward else reverse_diff
            candidate = (distance_to_segment, heading_score, segment, projection_xy, t_value, travel_forward)
            if best_candidate is None or candidate < best_candidate:
                best_candidate = candidate

        assert best_candidate is not None
        _, _, segment, projection_xy, t_value, travel_forward = best_candidate
        return segment, projection_xy, t_value, travel_forward

    def _project_point_to_segment(
        self,
        point_xy: tuple[float, float],
        start_xy: tuple[float, float],
        end_xy: tuple[float, float],
    ) -> tuple[tuple[float, float], float, float]:
        seg_dx = end_xy[0] - start_xy[0]
        seg_dy = end_xy[1] - start_xy[1]
        seg_len_sq = (seg_dx * seg_dx) + (seg_dy * seg_dy)
        if seg_len_sq == 0.0:
            return start_xy, 0.0, math.hypot(point_xy[0] - start_xy[0], point_xy[1] - start_xy[1])
        rel_x = point_xy[0] - start_xy[0]
        rel_y = point_xy[1] - start_xy[1]
        t_value = clamp(((rel_x * seg_dx) + (rel_y * seg_dy)) / seg_len_sq, 0.0, 1.0)
        projection_xy = (
            start_xy[0] + (seg_dx * t_value),
            start_xy[1] + (seg_dy * t_value),
        )
        distance_to_segment = math.hypot(point_xy[0] - projection_xy[0], point_xy[1] - projection_xy[1])
        return projection_xy, t_value, distance_to_segment

    def _interpolate_xy(
        self,
        start_xy: tuple[float, float],
        end_xy: tuple[float, float],
        ratio: float,
    ) -> tuple[float, float]:
        return (
            start_xy[0] + ((end_xy[0] - start_xy[0]) * ratio),
            start_xy[1] + ((end_xy[1] - start_xy[1]) * ratio),
        )

    def _walk_branch_paths(
        self,
        graph: nx.DiGraph,
        current_node: str,
        previous_node: str,
        current_heading: float,
        distance_left: float,
        polyline: list[tuple[float, float]],
        speed_mps: float,
        actor_type: str,
        actor_width_m: float,
        max_branch_count: int,
        depth_remaining: int,
    ) -> list[ProjectedPath]:
        if max_branch_count <= 0:
            return []
        if distance_left <= 0.0 or depth_remaining <= 0:
            final_xy = polyline[-1]
            final_latitude, final_longitude = self.geo.to_geodetic(final_xy[0], final_xy[1])
            return [ProjectedPath(final_latitude, final_longitude, current_heading, polyline.copy())]

        outgoing_edges = self._candidate_edges(graph, current_node, previous_node, current_heading, speed_mps, actor_type, actor_width_m)
        if not outgoing_edges:
            final_xy = polyline[-1]
            final_latitude, final_longitude = self.geo.to_geodetic(final_xy[0], final_xy[1])
            return [ProjectedPath(final_latitude, final_longitude, current_heading, polyline.copy())]

        branches: list[ProjectedPath] = []
        for _, next_node, data in outgoing_edges:
            if len(branches) >= max_branch_count:
                break
            start_xy = (graph.nodes[current_node]["x"], graph.nodes[current_node]["y"])
            end_xy = (graph.nodes[next_node]["x"], graph.nodes[next_node]["y"])
            edge_length = float(data["length_m"])
            edge_heading = float(data["heading_deg"])
            if distance_left <= edge_length:
                final_xy = self._interpolate_xy(start_xy, end_xy, distance_left / max(edge_length, 1e-6))
                final_polyline = polyline + [final_xy]
                final_latitude, final_longitude = self.geo.to_geodetic(final_xy[0], final_xy[1])
                branches.append(ProjectedPath(final_latitude, final_longitude, edge_heading, final_polyline))
                continue

            next_polyline = polyline + [end_xy]
            child_paths = self._walk_branch_paths(
                graph,
                next_node,
                current_node,
                edge_heading,
                distance_left - edge_length,
                next_polyline,
                speed_mps=speed_mps,
                actor_type=actor_type,
                actor_width_m=actor_width_m,
                max_branch_count=max_branch_count - len(branches),
                depth_remaining=depth_remaining - 1,
            )
            branches.extend(child_paths)

        for index, branch in enumerate(branches, start=1):
            branch.branch_id = f"B{index}"
        return branches

    def _candidate_edges(
        self,
        graph: nx.DiGraph,
        current_node: str,
        previous_node: str,
        current_heading: float,
        speed_mps: float,
        actor_type: str,
        actor_width_m: float = 0.0,
    ) -> list[tuple[str, str, dict]]:
        outgoing_edges = list(graph.out_edges(current_node, data=True))
        if not outgoing_edges:
            return []
        if actor_type == "aircraft":
            arc_feasible_edges = [edge for edge in outgoing_edges if self._edge_supports_actor_width(edge[2], actor_width_m, actor_type)]
            if arc_feasible_edges:
                outgoing_edges = arc_feasible_edges
        if len(outgoing_edges) > 1:
            filtered_edges = [edge for edge in outgoing_edges if edge[1] != previous_node]
            if filtered_edges:
                outgoing_edges = filtered_edges
        outgoing_edges.sort(key=lambda edge: heading_diff_deg(current_heading, float(edge[2]["heading_deg"])))
        max_turn_angle = self._max_turn_angle_deg(speed_mps, actor_type)
        turn_feasible_edges = [edge for edge in outgoing_edges if heading_diff_deg(current_heading, float(edge[2]["heading_deg"])) <= max_turn_angle]
        if turn_feasible_edges:
            outgoing_edges = turn_feasible_edges
        branch_limit = self._max_branch_options(speed_mps, actor_type)
        if branch_limit > 0:
            outgoing_edges = outgoing_edges[:branch_limit]
        return outgoing_edges

    def _edge_supports_actor_width(self, edge_data: dict, actor_width_m: float, actor_type: str) -> bool:
        if actor_type != "aircraft" or actor_width_m <= 0.0:
            return True
        aeroway = str(edge_data.get("aeroway", "taxiway"))
        usable_width_m = float(edge_data.get("usable_width_m", 0.0))
        if usable_width_m <= 0.0:
            usable_width_m = max(DEFAULT_AEROWAY_WIDTH_M.get(aeroway, DEFAULT_AEROWAY_WIDTH_M["taxiway"]) - DEFAULT_EDGE_CLEARANCE_MARGIN_M.get(aeroway, 4.5), 6.0)
        return usable_width_m >= (actor_width_m + self._arc_clearance_margin_m(actor_width_m, aeroway))

    def _arc_clearance_margin_m(self, actor_width_m: float, aeroway: str) -> float:
        return max(DEFAULT_EDGE_CLEARANCE_MARGIN_M.get(aeroway, 4.5), actor_width_m * 0.12)

    def _max_branch_options(self, speed_mps: float, actor_type: str) -> int:
        if actor_type == "vehicle":
            return 3
        if speed_mps >= 10.0:
            return 1
        if speed_mps >= 6.0:
            return 2
        return 4

    def _max_turn_angle_deg(self, speed_mps: float, actor_type: str) -> float:
        if actor_type == "vehicle":
            if speed_mps >= 8.0:
                return 95.0
            if speed_mps >= 4.0:
                return 125.0
            return 170.0
        if speed_mps >= 18.0:
            return 8.0
        if speed_mps >= 14.0:
            return 12.0
        if speed_mps >= 10.0:
            return 18.0
        if speed_mps >= 7.0:
            return 32.0
        if speed_mps >= 4.0:
            return 55.0
        return 120.0

    def _width_from_tags(self, tags: dict, aeroway: str) -> float:
        raw_width = tags.get("width")
        if raw_width is None:
            return DEFAULT_AEROWAY_WIDTH_M[aeroway]
        try:
            return max(float(str(raw_width).split()[0]), 6.0)
        except ValueError:
            return DEFAULT_AEROWAY_WIDTH_M[aeroway]

    def _usable_edge_width_m(self, tags: dict, aeroway: str) -> float:
        return max(self._width_from_tags(tags, aeroway) - DEFAULT_EDGE_CLEARANCE_MARGIN_M.get(aeroway, 4.5), 6.0)

    def _build_label(self, tags: dict, aeroway: str, local_coords: list[tuple[float, float]]) -> GISLabel | None:
        label_text = str(tags.get("ref") or tags.get("name") or "").strip()
        if not label_text:
            return None
        if aeroway in {"runway", "taxiway"}:
            midpoint_xy = self._midpoint_on_polyline(local_coords)
            rotation_deg = segment_heading_deg(local_coords[0], local_coords[-1])
        else:
            polygon = Polygon(local_coords) if self._is_closed(local_coords) and len(local_coords) >= 4 else LineString(local_coords).buffer(8.0)
            point = polygon.representative_point()
            midpoint_xy = (point.x, point.y)
            rotation_deg = 0.0
        return GISLabel(label_text, midpoint_xy, rotation_deg, aeroway)

    def _midpoint_on_polyline(self, local_coords: list[tuple[float, float]]) -> tuple[float, float]:
        if len(local_coords) == 1:
            return local_coords[0]
        total_length = 0.0
        segments: list[tuple[tuple[float, float], tuple[float, float], float]] = []
        for index in range(len(local_coords) - 1):
            start_xy = local_coords[index]
            end_xy = local_coords[index + 1]
            length_m = math.hypot(end_xy[0] - start_xy[0], end_xy[1] - start_xy[1])
            segments.append((start_xy, end_xy, length_m))
            total_length += length_m
        if total_length == 0.0:
            return local_coords[0]
        midpoint_length = total_length / 2.0
        traversed = 0.0
        for start_xy, end_xy, length_m in segments:
            if traversed + length_m >= midpoint_length:
                ratio = (midpoint_length - traversed) / max(length_m, 1e-6)
                return self._interpolate_xy(start_xy, end_xy, ratio)
            traversed += length_m
        return local_coords[-1]

    def _is_closed(self, local_coords: list[tuple[float, float]]) -> bool:
        return len(local_coords) >= 4 and local_coords[0] == local_coords[-1]

    def _build_bbox_polygon(self) -> Polygon:
        corners = [
            self.geo.to_local_xy(self.airport.lat_min, self.airport.lon_min),
            self.geo.to_local_xy(self.airport.lat_min, self.airport.lon_max),
            self.geo.to_local_xy(self.airport.lat_max, self.airport.lon_max),
            self.geo.to_local_xy(self.airport.lat_max, self.airport.lon_min),
        ]
        return Polygon(corners)

    def _local_bounds_from_airport(self) -> tuple[float, float, float, float]:
        bbox_polygon = self._build_bbox_polygon()
        min_x, min_y, max_x, max_y = bbox_polygon.bounds
        return min_x, max_x, min_y, max_y

