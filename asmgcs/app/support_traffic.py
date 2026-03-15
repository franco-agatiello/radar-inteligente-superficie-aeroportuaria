from __future__ import annotations

import math
import random

import networkx as nx
from shapely.geometry import MultiPolygon, Point, Polygon

from gis_manager import GISContext, GISManager
from models import ANIMATION_FPS, GROUND_VEHICLE_SPECS, GeoReference, GroundVehicleState, StaticObstacle


FOD_COUNT = 10
WILDLIFE_COUNT = 1
GROUND_VEHICLE_COUNT = 10
APRON_SERVICE_LABELS = {"Fuel Truck", "Pushback Tug", "Baggage Cart"}
TAXIWAY_SERVICE_LABELS = {"Follow-Me"}
APRON_SERVICE_SPEED_RANGE_MPS = (1.5, 5.5)
TAXIWAY_SERVICE_SPEED_RANGE_MPS = (4.0, 12.0)


class SupportTrafficService:
    def __init__(self, seed: int = 21) -> None:
        self._rng = random.Random(seed)
        self._geo: GeoReference | None = None
        self._gis_manager: GISManager | None = None
        self._gis_context: GISContext | None = None
        self.obstacles: list[StaticObstacle] = []
        self.vehicles: list[GroundVehicleState] = []
        self._vehicle_modes_by_id: dict[str, str] = {}
        self._vehicle_route_graphs: dict[str, nx.DiGraph] = {}
        self._vehicle_route_segments: dict[str, list] = {}
        self._apron_area: Polygon | MultiPolygon | None = None
        self._taxiway_graph = nx.DiGraph()
        self._taxiway_segments: list = []

    def activate_airport(self, geo: GeoReference, gis_manager: GISManager, gis_context: GISContext) -> None:
        self._geo = geo
        self._gis_manager = gis_manager
        self._gis_context = gis_context
        self._apron_area = gis_context.sector_areas.get("apron")
        self._taxiway_graph, self._taxiway_segments = self._build_filtered_network({"taxiway"})
        self.obstacles = self._spawn_obstacles()
        self.vehicles = self._spawn_ground_vehicles()

    def clear(self) -> None:
        self._geo = None
        self._gis_manager = None
        self._gis_context = None
        self.obstacles = []
        self.vehicles = []
        self._vehicle_modes_by_id = {}
        self._vehicle_route_graphs = {}
        self._vehicle_route_segments = {}
        self._apron_area = None
        self._taxiway_graph = nx.DiGraph()
        self._taxiway_segments = []

    def update(self, dt_s: float) -> None:
        if self._geo is None or self._gis_manager is None or self._gis_context is None:
            return
        self._update_wildlife(dt_s)
        self._update_ground_vehicles(dt_s)

    def _spawn_obstacles(self) -> list[StaticObstacle]:
        assert self._gis_manager is not None and self._gis_context is not None
        obstacles: list[StaticObstacle] = []
        fod_pool = [
            ("FOD", "Loose Panel Fragment", False, 1.1),
            ("FOD", "Metal Tooling", False, 1.0),
            ("FOD", "Tyre Debris", False, 1.3),
        ]
        wildlife_pool = [
            ("Wildlife", "Fox", True, 0.78),
            ("Wildlife", "Dog", True, 0.82),
            ("Wildlife", "Coyote", True, 0.88),
            ("Wildlife", "Hare", True, 0.66),
        ]
        total = FOD_COUNT + WILDLIFE_COUNT
        for index in range(total):
            latitude, longitude, _, _ = self._gis_manager.sample_point_within_drivable_area(self._gis_context.drivable_area, self._rng)
            if index < FOD_COUNT:
                category, description, is_wildlife, hazard_radius_m = self._rng.choice(fod_pool)
            else:
                category, description, is_wildlife, hazard_radius_m = self._rng.choice(wildlife_pool)
            obstacles.append(
                StaticObstacle(
                    obstacle_id=f"ANM-{index + 1 - FOD_COUNT}" if is_wildlife else f"OBS-{index + 1}",
                    latitude=latitude,
                    longitude=longitude,
                    category=category,
                    description=description,
                    is_wildlife=is_wildlife,
                    hazard_radius_m=hazard_radius_m,
                )
            )
        return obstacles

    def _spawn_ground_vehicles(self) -> list[GroundVehicleState]:
        assert self._gis_manager is not None and self._gis_context is not None
        vehicles: list[GroundVehicleState] = []
        for index in range(GROUND_VEHICLE_COUNT):
            spec = self._rng.choice(GROUND_VEHICLE_SPECS)
            operating_mode = self._vehicle_mode_for_spec(spec.label)
            latitude, longitude, heading_deg = self._spawn_vehicle_pose(operating_mode)
            speed_range = APRON_SERVICE_SPEED_RANGE_MPS if operating_mode == "apron" else TAXIWAY_SERVICE_SPEED_RANGE_MPS
            actor_id = f"GSE-{index + 1}"
            vehicles.append(
                GroundVehicleState(
                    actor_id=actor_id,
                    callsign=f"{spec.label[:3].upper()}-{index + 1}",
                    latitude=latitude,
                    longitude=longitude,
                    heading_deg=heading_deg,
                    speed_mps=self._rng.uniform(*speed_range),
                    length_m=spec.length_m,
                    width_m=spec.width_m,
                    color_hex=spec.color_hex,
                    vehicle_label=spec.label,
                )
            )
            self._vehicle_modes_by_id[actor_id] = operating_mode
            if operating_mode == "taxiway":
                self._vehicle_route_graphs[actor_id] = self._taxiway_graph if self._taxiway_graph.number_of_edges() > 0 else self._gis_context.graph
                self._vehicle_route_segments[actor_id] = self._taxiway_segments if self._taxiway_segments else list(self._gis_context.routing_segments)
        return vehicles

    def _update_ground_vehicles(self, dt_s: float) -> None:
        assert self._gis_context is not None and self._gis_manager is not None
        for vehicle in self.vehicles:
            operating_mode = self._vehicle_modes_by_id.get(vehicle.actor_id, "taxiway")
            if operating_mode == "apron":
                self._update_apron_vehicle(vehicle, dt_s)
                continue
            route = self._gis_manager.project_along_graph(
                self._vehicle_route_graphs.get(vehicle.actor_id, self._gis_context.graph),
                self._vehicle_route_segments.get(vehicle.actor_id, list(self._gis_context.routing_segments)),
                vehicle.latitude,
                vehicle.longitude,
                vehicle.heading_deg,
                vehicle.speed_mps * dt_s,
                rng=self._rng,
            )
            vehicle.latitude = route.final_latitude
            vehicle.longitude = route.final_longitude
            vehicle.heading_deg = route.final_heading_deg

    def _vehicle_mode_for_spec(self, label: str) -> str:
        if label in APRON_SERVICE_LABELS and self._apron_area is not None and not self._apron_area.is_empty:
            return "apron"
        if label in TAXIWAY_SERVICE_LABELS and self._taxiway_segments:
            return "taxiway"
        if self._apron_area is not None and not self._apron_area.is_empty:
            return "apron"
        return "taxiway"

    def _spawn_vehicle_pose(self, operating_mode: str) -> tuple[float, float, float]:
        assert self._gis_manager is not None and self._gis_context is not None
        if operating_mode == "apron" and self._apron_area is not None and not self._apron_area.is_empty:
            latitude, longitude, _, _ = self._gis_manager.sample_point_within_drivable_area(self._apron_area, self._rng)
            return latitude, longitude, self._rng.uniform(0.0, 360.0)
        route_segments = self._taxiway_segments if self._taxiway_segments else list(self._gis_context.routing_segments)
        latitude, longitude, _, _, heading_deg = self._gis_manager.sample_graph_position(route_segments, self._rng)
        return latitude, longitude, heading_deg

    def _update_apron_vehicle(self, vehicle: GroundVehicleState, dt_s: float) -> None:
        assert self._geo is not None and self._apron_area is not None
        current_x, current_y = self._geo.to_local_xy(vehicle.latitude, vehicle.longitude)
        turn_delta_deg = self._rng.uniform(-28.0, 28.0)
        candidate_heading_deg = (vehicle.heading_deg + turn_delta_deg) % 360.0
        heading_rad = math.radians(candidate_heading_deg)
        step_distance_m = max(vehicle.speed_mps * dt_s, 0.4)
        candidate_x = current_x + math.sin(heading_rad) * step_distance_m
        candidate_y = current_y - math.cos(heading_rad) * step_distance_m
        if not self._apron_area.contains(Point(candidate_x, candidate_y)):
            candidate_heading_deg = (vehicle.heading_deg + 180.0 + self._rng.uniform(-35.0, 35.0)) % 360.0
            heading_rad = math.radians(candidate_heading_deg)
            candidate_x = current_x + math.sin(heading_rad) * step_distance_m
            candidate_y = current_y - math.cos(heading_rad) * step_distance_m
            if not self._apron_area.contains(Point(candidate_x, candidate_y)):
                return
        vehicle.latitude, vehicle.longitude = self._geo.to_geodetic(candidate_x, candidate_y)
        vehicle.heading_deg = candidate_heading_deg

    def _build_filtered_network(self, allowed_aeroways: set[str]) -> tuple[nx.DiGraph, list]:
        assert self._gis_context is not None
        filtered_graph = nx.DiGraph()
        filtered_segments: list = []
        for segment in self._gis_context.routing_segments:
            if segment.aeroway not in allowed_aeroways:
                continue
            filtered_segments.append(segment)
            start_node_data = self._gis_context.graph.nodes.get(segment.start_node)
            end_node_data = self._gis_context.graph.nodes.get(segment.end_node)
            if start_node_data is not None:
                filtered_graph.add_node(segment.start_node, **start_node_data)
            if end_node_data is not None:
                filtered_graph.add_node(segment.end_node, **end_node_data)
            edge_data = self._gis_context.graph.get_edge_data(segment.start_node, segment.end_node)
            if edge_data is not None:
                filtered_graph.add_edge(segment.start_node, segment.end_node, **edge_data)
            reverse_edge_data = self._gis_context.graph.get_edge_data(segment.end_node, segment.start_node)
            if reverse_edge_data is not None:
                filtered_graph.add_edge(segment.end_node, segment.start_node, **reverse_edge_data)
        return filtered_graph, filtered_segments

    def _update_wildlife(self, dt_s: float) -> None:
        assert self._geo is not None and self._gis_context is not None
        for obstacle in self.obstacles:
            if not obstacle.is_wildlife:
                continue
            current_x, current_y = self._geo.to_local_xy(obstacle.latitude, obstacle.longitude)
            step_distance_m = self._rng.uniform(0.2, 1.0) * max(dt_s * ANIMATION_FPS, 0.5)
            for _ in range(8):
                heading_rad = self._rng.uniform(0.0, math.tau)
                candidate_x = current_x + math.cos(heading_rad) * step_distance_m
                candidate_y = current_y + math.sin(heading_rad) * step_distance_m
                if not self._gis_context.drivable_area.contains(Point(candidate_x, candidate_y)):
                    continue
                obstacle.latitude, obstacle.longitude = self._geo.to_geodetic(candidate_x, candidate_y)
                break


__all__ = ["SupportTrafficService"]