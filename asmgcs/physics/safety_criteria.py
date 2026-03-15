from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable

from shapely.geometry import MultiPolygon, Point, Polygon

from asmgcs.domain.contracts import KinematicActorState, PredictedBranch, SensorBands, StaticHazardState
from models import GeoReference, heading_diff_deg, segment_heading_deg


SECTOR_PRIORITY = ("runway", "taxiway", "apron")
KNOWN_SECTORS = ("global", "runway", "taxiway", "apron")
KNOWN_PRIMARY_KINDS = ("aircraft", "vehicle")
KNOWN_OTHER_KINDS = ("aircraft", "vehicle", "hazard", "wildlife")
DEFAULT_DB_FILENAME = "safety_criteria.db"
SECTOR_DISTANCE_TOLERANCE_M = 6.0
TAKEOFF_ROLL_SUPPRESSION_SPEED_MPS = 35.0
TAKEOFF_ROLL_HEADING_TOLERANCE_DEG = 15.0
TAKEOFF_ROLL_MAX_BRANCH_TURN_DEG = 20.0


@dataclass(frozen=True, slots=True)
class SafetyCriteriaRule:
    rule_id: int | None
    airport_code: str
    sector_name: str
    primary_kind: str
    other_kind: str
    speed_min_mps: float
    speed_max_mps: float
    green_m: float
    yellow_m: float
    red_m: float


class SafetyCriteriaRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._cache: dict[str, tuple[SafetyCriteriaRule, ...]] = {}
        self._ensure_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def ensure_airport_defaults(self, airport_code: str) -> None:
        with self._lock:
            if self._airport_rule_count(airport_code) > 0:
                if airport_code not in self._cache:
                    self._cache[airport_code] = self._fetch_rules_locked(airport_code)
                return
            with closing(sqlite3.connect(self._db_path)) as connection:
                connection.executemany(
                    (
                        "INSERT INTO safety_distance_rules "
                        "(airport_code, sector_name, primary_kind, other_kind, speed_min_mps, speed_max_mps, green_m, yellow_m, red_m) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    [
                        (
                            airport_code,
                            rule.sector_name,
                            rule.primary_kind,
                            rule.other_kind,
                            rule.speed_min_mps,
                            rule.speed_max_mps,
                            rule.green_m,
                            rule.yellow_m,
                            rule.red_m,
                        )
                        for rule in _default_rules_for_airport(airport_code)
                    ],
                )
                connection.commit()
            self._cache[airport_code] = self._fetch_rules_locked(airport_code)

    def list_rules(
        self,
        airport_code: str,
        sector_name: str | None = None,
        primary_kind: str | None = None,
        other_kind: str | None = None,
    ) -> tuple[SafetyCriteriaRule, ...]:
        self.ensure_airport_defaults(airport_code)
        with self._lock:
            rules = self._cache.get(airport_code, ())
            filtered = [
                rule
                for rule in rules
                if (sector_name is None or rule.sector_name == sector_name)
                and (primary_kind is None or rule.primary_kind == primary_kind)
                and (other_kind is None or rule.other_kind == other_kind)
            ]
        return tuple(filtered)

    def upsert_rule(self, rule: SafetyCriteriaRule) -> SafetyCriteriaRule:
        normalized = _normalize_rule(rule)
        with self._lock:
            with closing(sqlite3.connect(self._db_path)) as connection:
                cursor = connection.cursor()
                if normalized.rule_id is None:
                    cursor.execute(
                        (
                            "INSERT INTO safety_distance_rules "
                            "(airport_code, sector_name, primary_kind, other_kind, speed_min_mps, speed_max_mps, green_m, yellow_m, red_m) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        ),
                        (
                            normalized.airport_code,
                            normalized.sector_name,
                            normalized.primary_kind,
                            normalized.other_kind,
                            normalized.speed_min_mps,
                            normalized.speed_max_mps,
                            normalized.green_m,
                            normalized.yellow_m,
                            normalized.red_m,
                        ),
                    )
                    rule_id = int(cursor.lastrowid)
                else:
                    cursor.execute(
                        (
                            "UPDATE safety_distance_rules SET sector_name = ?, primary_kind = ?, other_kind = ?, speed_min_mps = ?, speed_max_mps = ?, green_m = ?, yellow_m = ?, red_m = ? "
                            "WHERE id = ? AND airport_code = ?"
                        ),
                        (
                            normalized.sector_name,
                            normalized.primary_kind,
                            normalized.other_kind,
                            normalized.speed_min_mps,
                            normalized.speed_max_mps,
                            normalized.green_m,
                            normalized.yellow_m,
                            normalized.red_m,
                            normalized.rule_id,
                            normalized.airport_code,
                        ),
                    )
                    rule_id = normalized.rule_id
                connection.commit()
            self._cache[normalized.airport_code] = self._fetch_rules_locked(normalized.airport_code)
        return SafetyCriteriaRule(rule_id=rule_id, **{field: getattr(normalized, field) for field in normalized.__dataclass_fields__ if field != "rule_id"})

    def delete_rule(self, airport_code: str, rule_id: int) -> None:
        self.ensure_airport_defaults(airport_code)
        with self._lock:
            with closing(sqlite3.connect(self._db_path)) as connection:
                connection.execute("DELETE FROM safety_distance_rules WHERE id = ? AND airport_code = ?", (rule_id, airport_code))
                connection.commit()
            self._cache[airport_code] = self._fetch_rules_locked(airport_code)

    def reset_airport_defaults(self, airport_code: str) -> None:
        with self._lock:
            with closing(sqlite3.connect(self._db_path)) as connection:
                connection.execute("DELETE FROM safety_distance_rules WHERE airport_code = ?", (airport_code,))
                connection.executemany(
                    (
                        "INSERT INTO safety_distance_rules "
                        "(airport_code, sector_name, primary_kind, other_kind, speed_min_mps, speed_max_mps, green_m, yellow_m, red_m) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    [
                        (
                            airport_code,
                            rule.sector_name,
                            rule.primary_kind,
                            rule.other_kind,
                            rule.speed_min_mps,
                            rule.speed_max_mps,
                            rule.green_m,
                            rule.yellow_m,
                            rule.red_m,
                        )
                        for rule in _default_rules_for_airport(airport_code)
                    ],
                )
                connection.commit()
            self._cache[airport_code] = self._fetch_rules_locked(airport_code)

    def resolve_bands(self, airport_code: str, sector_name: str, primary_kind: str, other_kind: str, reference_speed_mps: float) -> SensorBands | None:
        self.ensure_airport_defaults(airport_code)
        rules = self.list_rules(airport_code, primary_kind=primary_kind, other_kind=other_kind)
        if not rules:
            return None
        for candidate_sector in (sector_name, "global"):
            sector_rules = [rule for rule in rules if rule.sector_name == candidate_sector]
            if not sector_rules:
                continue
            match = _match_rule_by_speed(sector_rules, reference_speed_mps)
            if match is not None:
                return SensorBands(match.green_m, match.yellow_m, match.red_m)
        return None

    def _ensure_schema(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS safety_distance_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    airport_code TEXT NOT NULL,
                    sector_name TEXT NOT NULL,
                    primary_kind TEXT NOT NULL,
                    other_kind TEXT NOT NULL,
                    speed_min_mps REAL NOT NULL,
                    speed_max_mps REAL NOT NULL,
                    green_m REAL NOT NULL,
                    yellow_m REAL NOT NULL,
                    red_m REAL NOT NULL
                )
                """
            )
            connection.commit()

    def _airport_rule_count(self, airport_code: str) -> int:
        with closing(sqlite3.connect(self._db_path)) as connection:
            row = connection.execute("SELECT COUNT(*) FROM safety_distance_rules WHERE airport_code = ?", (airport_code,)).fetchone()
        return int(row[0]) if row else 0

    def _fetch_rules_locked(self, airport_code: str) -> tuple[SafetyCriteriaRule, ...]:
        with closing(sqlite3.connect(self._db_path)) as connection:
            rows = connection.execute(
                (
                    "SELECT id, airport_code, sector_name, primary_kind, other_kind, speed_min_mps, speed_max_mps, green_m, yellow_m, red_m "
                    "FROM safety_distance_rules WHERE airport_code = ? "
                    "ORDER BY sector_name, primary_kind, other_kind, speed_min_mps, speed_max_mps"
                ),
                (airport_code,),
            ).fetchall()
        return tuple(SafetyCriteriaRule(*row) for row in rows)


class SectorAwareSafetyCriteria:
    def __init__(
        self,
        airport_code: str,
        repository: SafetyCriteriaRepository,
        geo: GeoReference,
        sector_areas: dict[str, Polygon | MultiPolygon],
    ) -> None:
        self._airport_code = airport_code
        self._repository = repository
        self._geo = geo
        self._sector_areas = {name: geometry for name, geometry in sector_areas.items() if name in KNOWN_SECTORS and not geometry.is_empty}
        self._repository.ensure_airport_defaults(airport_code)

    def sector_for_position(self, latitude: float, longitude: float) -> str:
        point = Point(self._geo.to_local_xy(latitude, longitude))
        for sector_name in SECTOR_PRIORITY:
            geometry = self._sector_areas.get(sector_name)
            if geometry is None:
                continue
            if geometry.contains(point) or geometry.distance(point) <= SECTOR_DISTANCE_TOLERANCE_M:
                return sector_name
        return "global"

    def sector_for_actor(self, actor: KinematicActorState) -> str:
        return self.sector_for_position(actor.latitude, actor.longitude)

    def sector_for_hazard(self, hazard: StaticHazardState) -> str:
        return self.sector_for_position(hazard.latitude, hazard.longitude)

    def resolve_for_actor(
        self,
        actor: KinematicActorState,
        other_kind: str,
        other_speed_mps: float,
        predicted_branch: PredictedBranch | None = None,
    ) -> SensorBands | None:
        sector_name = self.sector_for_actor(actor)
        if self._uses_takeoff_prediction_only(sector_name, actor, predicted_branch):
            return _prediction_only_bands(actor, other_kind)
        reference_speed = max(actor.speed_mps, other_speed_mps)
        return self._repository.resolve_bands(
            self._airport_code,
            sector_name,
            actor.actor_type,
            other_kind,
            reference_speed,
        )

    def _uses_takeoff_prediction_only(
        self,
        sector_name: str,
        actor: KinematicActorState,
        predicted_branch: PredictedBranch | None,
    ) -> bool:
        return (
            actor.actor_type == "aircraft"
            and sector_name == "runway"
            and actor.speed_mps >= TAKEOFF_ROLL_SUPPRESSION_SPEED_MPS
            and _branch_matches_takeoff_roll(actor, predicted_branch)
        )


def _branch_matches_takeoff_roll(actor: KinematicActorState, predicted_branch: PredictedBranch | None) -> bool:
    if predicted_branch is None:
        return True

    headings: list[float] = []
    for start_xy, end_xy in zip(predicted_branch.local_polyline, predicted_branch.local_polyline[1:]):
        if math.hypot(end_xy[0] - start_xy[0], end_xy[1] - start_xy[1]) <= 1e-6:
            continue
        headings.append(segment_heading_deg(start_xy, end_xy))
    if not headings:
        return True

    first_heading = headings[0]
    if heading_diff_deg(actor.heading_deg, first_heading) > TAKEOFF_ROLL_HEADING_TOLERANCE_DEG:
        return False
    if heading_diff_deg(first_heading, predicted_branch.final_heading_deg) > TAKEOFF_ROLL_HEADING_TOLERANCE_DEG:
        return False
    max_turn_deg = max(heading_diff_deg(first_heading, heading_deg) for heading_deg in headings)
    return max_turn_deg <= TAKEOFF_ROLL_MAX_BRANCH_TURN_DEG


def _normalize_rule(rule: SafetyCriteriaRule) -> SafetyCriteriaRule:
    sector_name = rule.sector_name if rule.sector_name in KNOWN_SECTORS else "global"
    primary_kind = rule.primary_kind if rule.primary_kind in KNOWN_PRIMARY_KINDS else "aircraft"
    other_kind = rule.other_kind if rule.other_kind in KNOWN_OTHER_KINDS else "hazard"
    speed_min = max(0.0, float(rule.speed_min_mps))
    speed_max = max(speed_min + 0.1, float(rule.speed_max_mps))
    green = max(0.0, float(rule.green_m))
    yellow = max(0.0, min(float(rule.yellow_m), green))
    red = max(0.0, min(float(rule.red_m), yellow))
    return SafetyCriteriaRule(
        rule_id=rule.rule_id,
        airport_code=rule.airport_code,
        sector_name=sector_name,
        primary_kind=primary_kind,
        other_kind=other_kind,
        speed_min_mps=speed_min,
        speed_max_mps=speed_max,
        green_m=green,
        yellow_m=yellow,
        red_m=red,
    )


def _match_rule_by_speed(rules: Iterable[SafetyCriteriaRule], reference_speed_mps: float) -> SafetyCriteriaRule | None:
    speed = max(0.0, reference_speed_mps)
    direct_matches = [rule for rule in rules if rule.speed_min_mps <= speed < rule.speed_max_mps or math.isclose(speed, rule.speed_max_mps)]
    if direct_matches:
        return min(direct_matches, key=lambda rule: (rule.speed_max_mps - rule.speed_min_mps, rule.speed_min_mps))
    sorted_rules = sorted(rules, key=lambda rule: rule.speed_min_mps)
    if not sorted_rules:
        return None
    return min(sorted_rules, key=lambda rule: min(abs(speed - rule.speed_min_mps), abs(speed - rule.speed_max_mps)))


def _default_rules_for_airport(airport_code: str) -> tuple[SafetyCriteriaRule, ...]:
    speed_bands = (
        (0.0, 2.0, 1.0),
        (2.0, 6.0, 4.0),
        (6.0, 10.0, 8.0),
        (10.0, 16.0, 13.0),
        (16.0, 60.0, 20.0),
    )
    sector_multipliers = {"global": 1.0, "runway": 1.2, "taxiway": 1.0, "apron": 0.86}
    representative_footprints = {"aircraft": 37.6, "vehicle": 8.0}
    rules: list[SafetyCriteriaRule] = []
    for sector_name in KNOWN_SECTORS:
        for primary_kind in KNOWN_PRIMARY_KINDS:
            footprint_m = representative_footprints[primary_kind]
            for other_kind in KNOWN_OTHER_KINDS:
                for speed_min, speed_max, sample_speed in speed_bands:
                    bands = _legacy_default_bands(primary_kind, other_kind, sample_speed, footprint_m)
                    multiplier = sector_multipliers[sector_name]
                    rules.append(
                        SafetyCriteriaRule(
                            rule_id=None,
                            airport_code=airport_code,
                            sector_name=sector_name,
                            primary_kind=primary_kind,
                            other_kind=other_kind,
                            speed_min_mps=speed_min,
                            speed_max_mps=speed_max,
                            green_m=round(bands.green_m * multiplier, 1),
                            yellow_m=round(bands.yellow_m * multiplier, 1),
                            red_m=round(bands.red_m * multiplier, 1),
                        )
                    )
    return tuple(rules)


def _legacy_default_bands(primary_kind: str, other_kind: str, reference_speed: float, footprint_m: float) -> SensorBands:
    if reference_speed < 0.25:
        return SensorBands(0.0, 0.0, 0.0)
    if other_kind == "aircraft":
        return SensorBands(
            green_m=max(24.0, footprint_m * 0.75) + (reference_speed * 8.0),
            yellow_m=max(14.0, footprint_m * 0.42) + (reference_speed * 5.0),
            red_m=max(7.0, footprint_m * 0.22) + (reference_speed * 3.0),
        )
    if other_kind == "vehicle":
        return SensorBands(
            green_m=max(18.0, footprint_m * 0.55) + (reference_speed * 6.0),
            yellow_m=max(10.0, footprint_m * 0.30) + (reference_speed * 3.8),
            red_m=max(5.0, footprint_m * 0.16) + (reference_speed * 2.2),
        )
    if other_kind == "wildlife":
        return SensorBands(
            green_m=max(120.0, footprint_m * 2.2) + (reference_speed * 12.0),
            yellow_m=max(80.0, footprint_m * 1.6) + (reference_speed * 8.0),
            red_m=max(40.0, footprint_m * 1.0) + (reference_speed * 4.8),
        )
    return SensorBands(
        green_m=max(16.0, footprint_m * 0.50) + (reference_speed * 5.0),
        yellow_m=max(9.0, footprint_m * 0.28) + (reference_speed * 3.1),
        red_m=max(4.5, footprint_m * 0.14) + (reference_speed * 1.8),
    )


def _prediction_only_bands(actor: KinematicActorState, other_kind: str) -> SensorBands:
    footprint_m = max(actor.length_m, actor.width_m)
    if other_kind == "aircraft":
        return SensorBands(
            green_m=max(14.0, footprint_m * 0.34),
            yellow_m=max(7.0, footprint_m * 0.18),
            red_m=max(3.5, footprint_m * 0.10),
        )
    if other_kind == "vehicle":
        return SensorBands(
            green_m=max(12.0, footprint_m * 0.28),
            yellow_m=max(6.0, footprint_m * 0.16),
            red_m=max(3.0, footprint_m * 0.08),
        )
    if other_kind == "wildlife":
        return SensorBands(
            green_m=max(18.0, footprint_m * 0.42),
            yellow_m=max(9.0, footprint_m * 0.22),
            red_m=max(4.0, footprint_m * 0.10),
        )
    return SensorBands(
        green_m=max(10.0, footprint_m * 0.24),
        yellow_m=max(5.0, footprint_m * 0.14),
        red_m=max(2.5, footprint_m * 0.07),
    )


__all__ = [
    "DEFAULT_DB_FILENAME",
    "KNOWN_OTHER_KINDS",
    "KNOWN_PRIMARY_KINDS",
    "KNOWN_SECTORS",
    "SafetyCriteriaRepository",
    "SafetyCriteriaRule",
    "SectorAwareSafetyCriteria",
]