from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


KNOWN_ZONE_SECTORS = ("runway", "taxiway", "apron")


@dataclass(frozen=True, slots=True)
class ZoneTuningRule:
    airport_code: str
    sector_name: str
    buffer_m: float
    overlay_opacity: float


DEFAULT_ZONE_TUNING_BY_SECTOR: dict[str, ZoneTuningRule] = {
    "runway": ZoneTuningRule("", "runway", 0.0, 0.22),
    "taxiway": ZoneTuningRule("", "taxiway", 0.0, 0.13),
    "apron": ZoneTuningRule("", "apron", 0.0, 0.09),
}


class ZoneTuningRepository:
    def __init__(self, json_path: Path) -> None:
        self.json_path = json_path

    def ensure_airport_defaults(self, airport_code: str) -> None:
        payload = self._read_payload()
        airport_rules = payload.setdefault(airport_code, {})
        changed = False
        for sector_name in KNOWN_ZONE_SECTORS:
            if sector_name in airport_rules:
                continue
            default_rule = DEFAULT_ZONE_TUNING_BY_SECTOR[sector_name]
            airport_rules[sector_name] = asdict(
                ZoneTuningRule(airport_code, sector_name, default_rule.buffer_m, default_rule.overlay_opacity)
            )
            changed = True
        if changed:
            self._write_payload(payload)

    def list_rules(self, airport_code: str) -> tuple[ZoneTuningRule, ...]:
        self.ensure_airport_defaults(airport_code)
        payload = self._read_payload()
        airport_rules = payload.get(airport_code, {})
        rules: list[ZoneTuningRule] = []
        for sector_name in KNOWN_ZONE_SECTORS:
            raw_rule = airport_rules.get(sector_name)
            if raw_rule is None:
                default_rule = DEFAULT_ZONE_TUNING_BY_SECTOR[sector_name]
                rules.append(ZoneTuningRule(airport_code, sector_name, default_rule.buffer_m, default_rule.overlay_opacity))
                continue
            rules.append(self._normalize_rule(airport_code, raw_rule))
        return tuple(rules)

    def upsert_rule(self, rule: ZoneTuningRule) -> ZoneTuningRule:
        normalized = self._normalize_rule(rule.airport_code, asdict(rule))
        payload = self._read_payload()
        airport_rules = payload.setdefault(normalized.airport_code, {})
        airport_rules[normalized.sector_name] = asdict(normalized)
        self._write_payload(payload)
        return normalized

    def reset_airport_defaults(self, airport_code: str) -> None:
        payload = self._read_payload()
        payload[airport_code] = {
            sector_name: asdict(
                ZoneTuningRule(
                    airport_code,
                    sector_name,
                    DEFAULT_ZONE_TUNING_BY_SECTOR[sector_name].buffer_m,
                    DEFAULT_ZONE_TUNING_BY_SECTOR[sector_name].overlay_opacity,
                )
            )
            for sector_name in KNOWN_ZONE_SECTORS
        }
        self._write_payload(payload)

    def _normalize_rule(self, airport_code: str, raw_rule: dict) -> ZoneTuningRule:
        sector_name = str(raw_rule.get("sector_name", "taxiway"))
        if sector_name not in KNOWN_ZONE_SECTORS:
            sector_name = "taxiway"
        buffer_m = max(-120.0, min(120.0, float(raw_rule.get("buffer_m", 0.0))))
        overlay_opacity = max(0.0, min(1.0, float(raw_rule.get("overlay_opacity", 0.12))))
        return ZoneTuningRule(airport_code, sector_name, buffer_m, overlay_opacity)

    def _read_payload(self) -> dict:
        if not self.json_path.exists():
            return {}
        try:
            return json.loads(self.json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_payload(self, payload: dict) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


__all__ = ["KNOWN_ZONE_SECTORS", "ZoneTuningRepository", "ZoneTuningRule"]