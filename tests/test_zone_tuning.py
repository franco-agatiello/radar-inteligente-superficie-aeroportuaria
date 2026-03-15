from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from asmgcs.physics.zone_tuning import KNOWN_ZONE_SECTORS, ZoneTuningRepository, ZoneTuningRule


class ZoneTuningRepositoryTests(unittest.TestCase):
    def test_ensure_defaults_creates_one_rule_per_sector(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ZoneTuningRepository(Path(temp_dir) / "zone_tuning.json")

            repository.ensure_airport_defaults("SABE")
            rules = repository.list_rules("SABE")

        self.assertEqual({rule.sector_name for rule in rules}, set(KNOWN_ZONE_SECTORS))

    def test_upsert_persists_buffer_and_opacity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = ZoneTuningRepository(Path(temp_dir) / "zone_tuning.json")
            repository.ensure_airport_defaults("SABE")

            repository.upsert_rule(ZoneTuningRule("SABE", "taxiway", 8.5, 0.31))
            rules = {rule.sector_name: rule for rule in repository.list_rules("SABE")}

        self.assertAlmostEqual(rules["taxiway"].buffer_m, 8.5)
        self.assertAlmostEqual(rules["taxiway"].overlay_opacity, 0.31)


if __name__ == "__main__":
    unittest.main()