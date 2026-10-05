import unittest

from polar_station_foundation import chemical as rules
from polar_station_foundation.errors import *  # noqa: F401,F403


class ChemicalRulesTest(unittest.TestCase):
    def sheet(self, **overrides):
        value = {"batch_id": "b1", "version": 1, "hazard_class": "acid",
                 "temp_min": -5.0, "temp_max": 4.0, "container_materials": ["glass", "PE"],
                 "incompatible_classes": [], "required_barrier_level": 2}
        value.update(overrides)
        return value

    def location(self, **overrides):
        value = {"location_id": "l1", "temp_min": -10.0, "temp_max": 5.0,
                 "barrier_level": 2, "capacity_total": 100.0}
        value.update(overrides)
        return value

    def unit(self, **overrides):
        value = {"unit_id": "u1", "barrier_level": 2}
        value.update(overrides)
        return value

    def test_temperature_range_must_cover_sheet(self):
        self.assertEqual([], rules.temperature_violations(self.sheet(), self.location()))
        self.assertTrue(rules.temperature_violations(
            self.sheet(), self.location(temp_min=0.0, temp_max=5.0)))
        self.assertTrue(rules.temperature_violations(
            self.sheet(), self.location(temp_min=-10.0, temp_max=2.0)))

    def test_container_material_must_be_on_sheet_version(self):
        self.assertEqual([], rules.container_violations("glass", self.sheet()))
        violations = rules.container_violations("steel", self.sheet())
        self.assertEqual(1, len(violations))
        self.assertIn("v1", violations[0])

    def test_barrier_levels_of_location_and_unit(self):
        self.assertEqual([], rules.barrier_violations(self.sheet(), self.location(), self.unit()))
        self.assertTrue(rules.barrier_violations(
            self.sheet(), self.location(barrier_level=1), self.unit()))
        self.assertTrue(rules.barrier_violations(
            self.sheet(), self.location(), self.unit(barrier_level=1)))

    def test_rule_book_pair_and_sds_self_declaration(self):
        pairs = rules.normalize_pairs([["acid", "base"]])
        occupants = [{"batch_id": "b2", "hazard_class": "base",
                      "incompatible_classes": []}]
        violations = rules.coexistence_violations(self.sheet(), occupants, pairs)
        self.assertEqual(1, len(violations))
        self.assertIn("规则册禁忌对", violations[0])

    def test_self_declared_incompatibility_even_without_rule_pair(self):
        sheet = self.sheet(incompatible_classes=["oxidizer"])
        occupants = [{"batch_id": "b2", "hazard_class": "oxidizer",
                      "incompatible_classes": []}]
        violations = rules.coexistence_violations(sheet, occupants, set())
        self.assertEqual(1, len(violations))
        self.assertIn("SDS", violations[0])

    def test_same_batch_always_compatible(self):
        occupants = [{"batch_id": "b1", "hazard_class": "base",
                      "incompatible_classes": []}]
        self.assertEqual([], rules.coexistence_violations(
            self.sheet(), occupants, rules.normalize_pairs([["acid", "base"]])))

    def test_normalize_pairs_rejects_malformed(self):
        with self.assertRaises(ValueError):
            rules.normalize_pairs([["acid"]])

    def test_certification_validity_window(self):
        held = [{"certification_type": "chem-handle", "active": True,
                 "valid_from": "2026-01-01", "valid_until": "2026-12-31"}]
        self.assertEqual([], rules.certification_violations(["chem-handle"], held, "2026-06-01"))
        missing = rules.certification_violations(["chem-handle"], [], "2026-06-01")
        self.assertEqual(1, len(missing))
        expired = rules.certification_violations(["chem-handle"], held, "2027-01-01")
        self.assertEqual(1, len(expired))
        held[0]["active"] = False
        self.assertTrue(rules.certification_violations(["chem-handle"], held, "2026-06-01"))


if __name__ == "__main__":
    unittest.main()
