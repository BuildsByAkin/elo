"""Tests for the cross-source matcher.

`norm` decides whether two rows are the same recording. It is used by the
fuser, by pool.ensure_tracks (which SKIPS anything normalising to empty, so a
bug here silently deletes catalogue) and by the version deduper. It is worth
more tests than its six lines suggest.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import norm


class TestNorm(unittest.TestCase):
    def test_folds_accents(self):
        self.assertEqual(norm("Café del Mar"), norm("Cafe del Mar"))

    def test_strips_version_suffixes(self):
        self.assertEqual(norm("Beanie (Slowed)"), norm("Beanie"))
        self.assertEqual(norm("Elastic Heart (Piano Version)"),
                         norm("Elastic Heart"))
        self.assertEqual(norm("Something in the Orange (Z&E's Version)"),
                         norm("Something in the Orange"))

    def test_strips_features(self):
        self.assertEqual(norm("Yeah! (feat. Lil Jon & Ludacris)"), norm("Yeah!"))
        self.assertEqual(norm("Rush feat. Somebody"), norm("Rush"))

    def test_ampersand_is_and(self):
        self.assertEqual(norm("Salt & Pepper"), norm("Salt and Pepper"))

    # The regression that mattered: everything non-Latin used to normalise to
    # "", so Cyrillic/Arabic/CJK titles collided with each other AND were
    # skipped outright by pool.ensure_tracks.
    def test_keeps_cyrillic(self):
        self.assertTrue(norm("Буйно голова"))
        self.assertNotEqual(norm("Буйно голова"), norm("Оправдан"))

    def test_keeps_cjk(self):
        self.assertEqual(norm("東京"), "東京")
        self.assertNotEqual(norm("東京"), norm("大阪"))

    def test_keeps_yoruba(self):
        # Was mangled to "i pi n" by the old accent handling.
        self.assertEqual(norm("Ìpín"), "ipin")

    def test_distinct_non_latin_titles_do_not_collide(self):
        titles = ["Буйно голова", "Оправдан", "東京", "Ìpín", "نبي"]
        self.assertEqual(len({norm(t) for t in titles}), len(titles))

    def test_empty_and_junk(self):
        self.assertEqual(norm(""), "")
        self.assertEqual(norm(None), "")
        self.assertEqual(norm("!!! ??? ---"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
