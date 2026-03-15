from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import networkx as nx
from shapely.geometry import Polygon

from gis_manager import GISManager
from asmgcs.physics.zone_tuning import ZoneTuningRepository, ZoneTuningRule
from models import AIRPORTS, GeoReference, WebMercatorMapper


class GISBranchingTests(unittest.TestCase):
    def setUp(self) -> None:
        airport = AIRPORTS["ATL"]
        self.geo = GeoReference(center_lat=airport.center_lat, center_lon=airport.center_lon)
        self.manager = GISManager(airport, self.geo, WebMercatorMapper(airport))
        self.graph = nx.DiGraph()
        self.graph.add_node("N0", x=0.0, y=0.0)
        self.graph.add_node("N1", x=0.0, y=-60.0)
        self.graph.add_node("N2", x=32.0, y=-32.0)
        self.graph.add_node("N3", x=-32.0, y=-32.0)
        self.graph.add_edge("N0", "N1", length_m=60.0, heading_deg=0.0)
        self.graph.add_edge("N0", "N2", length_m=45.0, heading_deg=45.0)
        self.graph.add_edge("N0", "N3", length_m=45.0, heading_deg=315.0)

    def test_high_speed_aircraft_only_keeps_straight_branch(self) -> None:
        edges = self.manager._candidate_edges(self.graph, "N0", "", 0.0, 12.0, "aircraft")

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0][1], "N1")

    def test_low_speed_aircraft_can_keep_multiple_turn_options(self) -> None:
        edges = self.manager._candidate_edges(self.graph, "N0", "", 0.0, 2.0, "aircraft")

        self.assertGreaterEqual(len(edges), 3)

    def test_arc_prunes_taxiway_that_cannot_fit_wingspan(self) -> None:
        self.graph["N0"]["N1"]["usable_width_m"] = 26.0
        self.graph["N0"]["N1"]["aeroway"] = "taxiway"
        self.graph["N0"]["N2"]["usable_width_m"] = 42.0
        self.graph["N0"]["N2"]["aeroway"] = "taxiway"
        self.graph["N0"]["N3"]["usable_width_m"] = 42.0
        self.graph["N0"]["N3"]["aeroway"] = "taxiway"

        edges = self.manager._candidate_edges(self.graph, "N0", "", 45.0, 4.0, "aircraft", actor_width_m=35.8)

        self.assertTrue(all(edge[1] != "N1" for edge in edges))
        self.assertTrue(any(edge[1] == "N2" for edge in edges))

    def test_query_build_prefers_packaged_payload_when_available(self) -> None:
        payload = {
            "elements": [
                {
                    "geometry": [
                        {"lat": self.manager.airport.center_lat, "lon": self.manager.airport.center_lon - 0.0005},
                        {"lat": self.manager.airport.center_lat, "lon": self.manager.airport.center_lon + 0.0005},
                    ],
                    "tags": {"aeroway": "runway", "ref": "TEST 01/19"},
                },
                {
                    "geometry": [
                        {"lat": self.manager.airport.center_lat - 0.0002, "lon": self.manager.airport.center_lon - 0.0003},
                        {"lat": self.manager.airport.center_lat - 0.0002, "lon": self.manager.airport.center_lon + 0.0003},
                        {"lat": self.manager.airport.center_lat + 0.0002, "lon": self.manager.airport.center_lon + 0.0003},
                        {"lat": self.manager.airport.center_lat + 0.0002, "lon": self.manager.airport.center_lon - 0.0003},
                        {"lat": self.manager.airport.center_lat - 0.0002, "lon": self.manager.airport.center_lon - 0.0003},
                    ],
                    "tags": {"aeroway": "apron", "name": "Packaged Apron"},
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            packaged_path = Path(temp_dir) / "atl_overpass_aeroway.json"
            packaged_path.write_text(json.dumps(payload), encoding="utf-8")
            self.manager.packaged_overpass_path = packaged_path

            def _unexpected_fetch(_: str) -> tuple[dict, str]:
                raise AssertionError("live Overpass should not be queried when a packaged layout exists")

            self.manager._fetch_overpass_payload = _unexpected_fetch  # type: ignore[method-assign]

            drivable_area, sector_areas, graph, routing_segments, labels, source_status = self.manager._query_overpass_and_build()

        self.assertEqual(source_status, "packaged aeroway layout")
        self.assertFalse(drivable_area.is_empty)
        self.assertIn("runway", sector_areas)
        self.assertIn("apron", sector_areas)
        self.assertGreater(graph.number_of_edges(), 0)
        self.assertGreater(len(routing_segments), 0)
        self.assertEqual(labels[0].text, "TEST 01/19")

    def test_load_packaged_payload_ignores_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            packaged_path = Path(temp_dir) / "atl_overpass_aeroway.json"
            packaged_path.write_text("{not-json", encoding="utf-8")
            self.manager.packaged_overpass_path = packaged_path

            self.assertIsNone(self.manager._load_packaged_overpass_payload())

    def test_apply_zone_tuning_buffers_sector_and_drivable_area(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ZoneTuningRepository(Path(temp_dir) / "zone_tuning.json")
            repository.upsert_rule(ZoneTuningRule("ATL", "runway", 12.0, 0.4))
            manager = GISManager(AIRPORTS["ATL"], self.geo, WebMercatorMapper(AIRPORTS["ATL"]), repository)
            runway = Polygon([(0.0, 0.0), (0.0, 80.0), (12.0, 80.0), (12.0, 0.0)])

            drivable_area, sector_areas, zone_tuning = manager._apply_zone_tuning(runway, {"runway": runway})

        self.assertGreater(drivable_area.area, runway.area)
        self.assertGreater(sector_areas["runway"].area, runway.area)
        self.assertAlmostEqual(zone_tuning["runway"].buffer_m, 12.0)


if __name__ == "__main__":
    unittest.main()