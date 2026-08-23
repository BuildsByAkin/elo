"""Tests for the selection engine. No network, no database, no API key.

Run:  python -m unittest discover tests -v

stdlib unittest on purpose — this repo has two dependencies and adding pytest
to run twelve assertions is not a trade worth making.
"""
import math
import unittest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine as E


def card(i, v, e, artist="A", themes=("breakup",), stance="devastated",
         genre="Pop", seconds=200):
    return {"id": i, "title": "t%d" % i, "artist": artist, "genre": genre,
            "seconds": seconds, "themes": list(themes), "stance": stance,
            "valence": v, "energy": e, "confidence": "known", "basis": "lyrics"}


def ladder(n=11, artist_cycle=("A", "B", "C")):
    """A clean synthetic library: energy climbs 0..10 in even rungs."""
    return [card(i, v=float(i), e=float(i),
                 artist=artist_cycle[i % len(artist_cycle)])
            for i in range(n)]


class TestInterpolate(unittest.TestCase):
    def test_includes_both_endpoints(self):
        pts = E.interpolate((2.0, 2.0), (8.0, 9.0), 5)
        self.assertEqual(len(pts), 5)
        self.assertEqual(pts[0], (2.0, 2.0))
        self.assertEqual(pts[-1], (8.0, 9.0))

    def test_midpoint_is_halfway(self):
        pts = E.interpolate((0.0, 0.0), (10.0, 10.0), 3)
        self.assertAlmostEqual(pts[1][0], 5.0)
        self.assertAlmostEqual(pts[1][1], 5.0)

    def test_evenly_spaced(self):
        pts = E.interpolate((0.0, 0.0), (10.0, 0.0), 6)
        gaps = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
        for g in gaps:
            self.assertAlmostEqual(g, gaps[0])

    def test_degenerate(self):
        self.assertEqual(E.interpolate((1.0, 1.0), (2.0, 2.0), 0), [])
        self.assertEqual(E.interpolate((1.0, 1.0), (2.0, 2.0), 1), [(1.0, 1.0)])


class TestDuration(unittest.TestCase):
    def test_missing_seconds_falls_back(self):
        # An export with no Time column must not make a 45-minute set 0 seconds.
        cards = [card(1, 1, 1, seconds=0), card(2, 2, 2, seconds=0)]
        self.assertEqual(E.duration(cards, avg=210), 420)

    def test_mixed(self):
        cards = [card(1, 1, 1, seconds=100), card(2, 2, 2, seconds=0)]
        self.assertEqual(E.duration(cards, avg=210), 310)

    def test_steps_for_never_below_two(self):
        self.assertEqual(E.steps_for(0), 2)
        self.assertEqual(E.steps_for(1), 2)
        self.assertEqual(E.steps_for(35, avg=210), 10)


class TestSustain(unittest.TestCase):
    def test_theme_overlap_is_jaccard(self):
        a = card(1, 5, 5, themes=("breakup", "grief"))
        b = card(2, 5, 5, themes=("breakup", "hope"))
        self.assertAlmostEqual(E.theme_overlap(a, b), 1 / 3)
        self.assertEqual(E.theme_overlap(a, a), 1.0)
        self.assertEqual(E.theme_overlap(a, card(3, 5, 5, themes=("party",))), 0.0)

    def test_shared_subject_beats_mood_proximity(self):
        """The whole thesis: a breakup song across the room outranks a party
        song standing next to you."""
        seed = card(1, 2.0, 2.0, themes=("breakup",))
        same_theme_far = card(2, 5.0, 5.0, themes=("breakup",))
        other_theme_near = card(3, 2.1, 2.1, themes=("party",))
        self.assertGreater(E.sustain_score(seed, same_theme_far),
                           E.sustain_score(seed, other_theme_near))

    def test_stance_separates_identical_situations(self):
        """Someone Like You vs I Will Survive: same theme, opposite posture.
        This is the field the brief's schema did not have."""
        seed = card(1, 2.0, 3.0, themes=("breakup",), stance="devastated")
        wallow = card(2, 2.2, 3.2, themes=("breakup",), stance="devastated")
        anthem = card(3, 2.2, 3.2, themes=("breakup",), stance="defiant")
        self.assertGreater(E.sustain_score(seed, wallow),
                           E.sustain_score(seed, anthem))

    def test_excludes_the_seed_itself(self):
        pool = ladder(5)
        out = E.sustain(pool[2], pool, n=5)
        self.assertNotIn(pool[2]["id"], [c["id"] for c in out])

    def test_respects_n(self):
        pool = ladder(9)
        self.assertEqual(len(E.sustain(pool[0], pool, n=3)), 3)

    def test_weights_are_tunable(self):
        seed = card(1, 2.0, 2.0, themes=("breakup",), stance="devastated")
        other = card(2, 2.0, 2.0, themes=("party",), stance="euphoric")
        # Nothing in common and no distance: with every weight live, the score
        # is 0, and raising the theme weight cannot rescue a disjoint theme set.
        self.assertAlmostEqual(E.sustain_score(seed, other), 0.0)
        self.assertAlmostEqual(E.sustain_score(seed, other, w_theme=99.0), 0.0)
        # A stance match is worth exactly its weight when all else is equal.
        same_stance = card(3, 2.0, 2.0, themes=("party",), stance="devastated")
        self.assertAlmostEqual(
            E.sustain_score(seed, same_stance, w_stance=2.5), 2.5)


class TestShift(unittest.TestCase):
    def test_empty_pool(self):
        self.assertEqual(E.shift([], (2.0, 2.0), (8.0, 8.0), 30), [])

    def test_never_repeats_a_track(self):
        steps = E.shift(ladder(11), (0.0, 0.0), (10.0, 10.0), 40)
        ids = [s.card["id"] for s in steps]
        self.assertEqual(len(ids), len(set(ids)))

    def test_travels_from_start_toward_end(self):
        steps = E.shift(ladder(11), (0.0, 0.0), (10.0, 10.0), 40)
        self.assertGreaterEqual(len(steps), 3)
        self.assertLess(steps[0].card["energy"], steps[-1].card["energy"])

    def test_ramp_is_monotonic_on_a_clean_pool(self):
        """If the library can supply a smooth ramp, the engine must find it."""
        steps = E.shift(ladder(11), (0.0, 0.0), (10.0, 10.0), 40)
        r = E.ramp_report(steps)
        self.assertEqual(r["monotonic_frac"], 1.0)

    def test_descending_arc_also_works(self):
        steps = E.shift(ladder(11), (10.0, 10.0), (0.0, 0.0), 40)
        self.assertGreater(steps[0].card["energy"], steps[-1].card["energy"])
        self.assertEqual(E.ramp_report(steps)["monotonic_frac"], 1.0)

    def test_respects_the_duration_budget(self):
        # 10 minutes at 200s/track is 3 tracks, not 11.
        steps = E.shift(ladder(11), (0.0, 0.0), (10.0, 10.0), 10)
        self.assertLessEqual(E.duration([s.card for s in steps]), 10 * 60 + 200)

    def test_avoids_back_to_back_same_artist_when_it_can(self):
        """Two artists sit at every rung; the engine should alternate rather
        than serve four of the same artist in a row."""
        pool = []
        i = 0
        for rung in range(0, 11):
            for who in ("A", "B"):
                pool.append(card(i, float(rung), float(rung), artist=who))
                i += 1
        steps = E.shift(pool, (0.0, 0.0), (10.0, 10.0), 40,
                        E.Constraints(artist_window=2, w_artist=50.0))
        artists = [s.card["artist"] for s in steps]
        repeats = sum(1 for a, b in zip(artists, artists[1:]) if a == b)
        self.assertEqual(repeats, 0, "got %s" % artists)

    def test_artist_penalty_is_soft_not_a_filter(self):
        """A one-artist library must still produce a playlist, flagged."""
        pool = [card(i, float(i), float(i), artist="Solo") for i in range(11)]
        steps = E.shift(pool, (0.0, 0.0), (10.0, 10.0), 40)
        self.assertGreater(len(steps), 3)
        self.assertTrue(any(b.startswith("artist repeat")
                            for s in steps[1:] for b in s.broke))

    def test_big_energy_jumps_are_flagged(self):
        """A library with a hole in the middle must report the jump, not hide
        it — that is the substitute for the tempo constraint."""
        pool = [card(0, 0.0, 0.0), card(1, 0.5, 0.5),
                card(2, 9.5, 9.5, artist="B"), card(3, 10.0, 10.0, artist="C")]
        steps = E.shift(pool, (0.0, 0.0), (10.0, 10.0), 40,
                        E.Constraints(max_energy_step=1.5))
        self.assertTrue(any(any(b.startswith("energy jump") for b in s.broke)
                            for s in steps))

    def test_genre_lock_prefers_the_anchor_early(self):
        """Do not change genre and mood at the same time — the early path
        should stay in the seed's genre when an equivalent track exists."""
        pool = []
        i = 0
        for rung in range(0, 11):
            for g in ("Soul", "Metal"):
                pool.append(card(i, float(rung), float(rung),
                                 artist="A%d" % i, genre=g))
                i += 1
        steps = E.shift(pool, (0.0, 0.0), (10.0, 10.0), 40,
                        E.Constraints(genre_lock_frac=0.5, w_genre=50.0))
        anchor = steps[0].card["genre"]
        early = [s.card["genre"] for s in steps[:len(steps) // 2]]
        self.assertTrue(all(g == anchor for g in early), "got %s" % early)


class TestDedupe(unittest.TestCase):
    """The bug that reached a real YouTube Music playlist: the same Chezile
    song at positions 2 and 8, as `Beanie (Slowed)` and `Beanie`."""

    @staticmethod
    def key(s):
        from common import norm
        return norm(s)

    def test_collapses_versions_of_one_recording(self):
        cards = [card(1, 2, 2, artist="Chezile"), card(2, 2, 2, artist="Chezile")]
        cards[0]["title"], cards[1]["title"] = "Beanie (Slowed)", "Beanie"
        out = E.dedupe(cards, self.key)
        self.assertEqual(len(out), 1)

    def test_prefers_the_shorter_original_title(self):
        cards = [card(1, 2, 2, artist="Sia"), card(2, 2, 2, artist="Sia")]
        cards[0]["title"] = "Elastic Heart (Piano Version)"
        cards[1]["title"] = "Elastic Heart"
        self.assertEqual(E.dedupe(cards, self.key)[0]["title"], "Elastic Heart")

    def test_keeps_genuinely_different_songs(self):
        cards = [card(1, 2, 2, artist="A"), card(2, 2, 2, artist="A")]
        cards[0]["title"], cards[1]["title"] = "One", "Two"
        self.assertEqual(len(E.dedupe(cards, self.key)), 2)

    def test_same_title_different_artist_survives(self):
        cards = [card(1, 2, 2, artist="Black Sabbath"),
                 card(2, 2, 2, artist="Tupac")]
        cards[0]["title"] = cards[1]["title"] = "Changes"
        self.assertEqual(len(E.dedupe(cards, self.key)), 2)

    def test_non_latin_titles_are_not_collapsed_together(self):
        cards = [card(1, 2, 2, artist="Гио ПиКа"), card(2, 2, 2, artist="Гио ПиКа")]
        cards[0]["title"], cards[1]["title"] = "Буйно голова", "Оправдан"
        self.assertEqual(len(E.dedupe(cards, self.key)), 2)

    def test_preserves_pool_order(self):
        cards = [card(i, i, i, artist="A%d" % i) for i in range(5)]
        self.assertEqual([c["id"] for c in E.dedupe(cards, self.key)],
                         [0, 1, 2, 3, 4])

    def test_shift_never_serves_the_same_recording_twice(self):
        pool = []
        for i in range(8):
            a = card(i, float(i), float(i), artist="X%d" % i)
            a["title"] = "Song%d" % i
            pool.append(a)
        twin = card(99, 3.0, 3.0, artist="X3")
        twin["title"] = "Song3 (Slowed)"
        pool.append(twin)
        steps = E.shift(E.dedupe(pool, self.key), (0.0, 0.0), (7.0, 7.0), 40)
        keys = [(self.key(s.card["title"]), self.key(s.card["artist"]))
                for s in steps]
        self.assertEqual(len(keys), len(set(keys)))


class TestRampReport(unittest.TestCase):
    def test_detects_a_broken_ramp(self):
        steps = [E.Step(i, (0.0, 0.0), card(i, 0, e), 0.0)
                 for i, e in enumerate([1.0, 5.0, 2.0, 8.0])]
        r = E.ramp_report(steps)
        self.assertLess(r["monotonic_frac"], 1.0)
        self.assertAlmostEqual(r["max_jump"], 6.0)

    def test_single_track(self):
        r = E.ramp_report([E.Step(0, (0.0, 0.0), card(1, 5, 5), 0.0)])
        self.assertEqual(r["tracks"], 1)
        self.assertEqual(r["max_jump"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
