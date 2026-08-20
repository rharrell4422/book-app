"""Regression coverage for services/metadata_provenance.py's bind-time
provenance rules (see project design chat's consolidated Add Book
specification, restored architecture items A1/A2/R1-R3)."""
import unittest

from services.metadata_provenance import (
    is_verified,
    provenance_for_declined_or_manual_entry,
    provenance_for_find_bind,
)


class ProvenanceForFindBindTest(unittest.TestCase):
    def test_high_confidence_is_fully_verified_and_not_flagged(self):
        self.assertEqual(
            provenance_for_find_bind("high"),
            {"metadata_source": "provider", "needs_reresolution": False},
        )

    def test_medium_confidence_is_fully_verified_and_not_flagged(self):
        # Explicit per the restored spec: medium-confidence binds are NOT
        # queued for re-resolution, same as high.
        self.assertEqual(
            provenance_for_find_bind("medium"),
            {"metadata_source": "provider", "needs_reresolution": False},
        )

    def test_low_confidence_is_verified_but_flagged_for_reresolution(self):
        self.assertEqual(
            provenance_for_find_bind("low"),
            {"metadata_source": "provider", "needs_reresolution": True},
        )

    def test_unknown_confidence_tier_raises(self):
        with self.assertRaises(ValueError):
            provenance_for_find_bind("zero")

    def test_every_provider_bind_is_verified_regardless_of_tier(self):
        for tier in ("high", "medium", "low"):
            self.assertTrue(is_verified(provenance_for_find_bind(tier)["metadata_source"]))


class ProvenanceForDeclinedOrManualEntryTest(unittest.TestCase):
    def test_stamps_user_not_null(self):
        result = provenance_for_declined_or_manual_entry()
        self.assertEqual(result["metadata_source"], "user")
        self.assertIsNone(result["needs_reresolution"])

    def test_is_not_verified(self):
        result = provenance_for_declined_or_manual_entry()
        self.assertFalse(is_verified(result["metadata_source"]))


class IsVerifiedTest(unittest.TestCase):
    def test_provider_and_discovery_are_verified(self):
        self.assertTrue(is_verified("provider"))
        self.assertTrue(is_verified("discovery"))

    def test_user_import_and_none_are_not_verified(self):
        self.assertFalse(is_verified("user"))
        self.assertFalse(is_verified("import"))
        self.assertFalse(is_verified(None))


if __name__ == "__main__":
    unittest.main()
