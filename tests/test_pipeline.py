"""The parts that are pure logic, tested against a temporary cache.

Everything network-facing is left to the live probes in the README. What is
worth pinning down here is the arithmetic the design leans on: that fusion
rewards agreement and respects its weights, that library affinity reorders a
shortlist rather than filtering it, and that a block lands on its minutes.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

os.environ["ELO_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blend            # noqa: E402
import common           # noqa: E402
import feedback as F    # noqa: E402
import library          # noqa: E402
import push             # noqa: E402
import sources          # noqa: E402
import taste as T       # noqa: E402
import ytauth           # noqa: E402


def cand(title, artist, rank, source, **kw):
    return sources._cand(title, artist, rank, source, **kw)


class TestNormalisation(unittest.TestCase):

    def test_folds_accents_but_keeps_non_latin(self):
        self.assertEqual(common.norm("Café"), common.norm("Cafe"))
        self.assertEqual(common.norm("東京"), "東京")
        self.assertNotEqual(common.norm("東京"), common.norm("大阪"))

    def test_strips_the_noise_that_breaks_cross_source_matching(self):
        self.assertEqual(common.norm("Often (feat. Drake) [Remastered]"),
                         common.norm("Often"))

    def test_clean_removes_video_furniture_only(self):
        self.assertEqual(common.clean("logical (Official Lyric Video)"),
                         "logical")
        self.assertEqual(common.clean("Say So (feat. Nicki Minaj)"),
                         "Say So (feat. Nicki Minaj)")

    def test_seconds(self):
        self.assertEqual(common.seconds("5:35"), 335)
        self.assertEqual(common.seconds("1:02:03"), 3723)
        self.assertEqual(common.seconds(""), 0)
        self.assertEqual(common.seconds("garbage"), 0)


class TestFusion(unittest.TestCase):

    def test_agreement_beats_a_single_high_rank(self):
        a = [cand("Shared", "X", 20, "radio"), cand("Solo", "Y", 1, "radio")]
        b = [cand("Shared", "X", 20, "mood:Sad")]
        out = sources.fuse([(1.0, a), (1.0, b)])
        self.assertEqual(out[0]["title"], "Shared")

    def test_weight_demotes_a_source(self):
        a = [cand("FromSeed", "X", 1, "radio")]
        b = [cand("FromMood", "Y", 1, "mood:Sad")]
        opening = sources.fuse([(1.0, a), (0.7, b)])
        later = sources.fuse([(0.35, a), (1.0, b)])
        self.assertEqual(opening[0]["title"], "FromSeed")
        self.assertEqual(later[0]["title"], "FromMood")

    def test_merges_across_title_variants_and_keeps_the_richer_row(self):
        a = [cand("Often", "The Weeknd", 1, "similar")]           # no videoId
        b = [cand("Often (Kygo Remix)", "The Weeknd", 3, "radio",
                  video_id="abc", secs=250)]
        out = sources.fuse([(1.0, a), (1.0, b)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["video_id"], "abc")
        self.assertEqual(out[0]["secs"], 250)
        self.assertEqual(out[0]["sources"], {"similar", "radio"})


RAP = {"mood": "Hip-hop", "genres": ["hip-hop"], "tags": ["rap", "trap"]}
SLEEP = {"mood": "Sleep", "genres": [], "tags": ["ambient", "calm"]}
RNB = {"mood": "R&B & soul", "genres": ["r&b"], "tags": ["alternative r&b"]}
MOODONLY = {"mood": "Sad", "genres": [], "tags": []}


def seed_library():
    """A small, lopsided library: a lot of one rapper, a little folk."""
    library.upsert_tracks([
        {"title": "Big Rap Song", "artist": "Rap Guy", "genre": "Hip-Hop/Rap",
         "plays": 140, "liked": 1, "source": "apple", "video_id": "vid123"},
        {"title": "Other Rap Song", "artist": "Rap Guy", "genre": "Hip-Hop/Rap",
         "plays": 40, "source": "apple"},
        {"title": "Third Rap Song", "artist": "Rap Guy", "genre": "Hip-Hop/Rap",
         "plays": 20, "source": "apple"},
        {"title": "Quiet Song", "artist": "Folk Person", "genre": "Folk",
         "plays": 3, "source": "apple"},
        {"title": "Skipped Song", "artist": "Rap Guy", "genre": "Hip-Hop/Rap",
         "plays": 1, "skips": 30, "source": "apple"},
    ])
    library.tags_from_track_genres("apple")


class TestTasteVocabulary(unittest.TestCase):

    def test_collapses_the_four_ways_services_spell_a_genre(self):
        for text in ("R&B/Soul", "r&b", "rnb", "R and B"):
            self.assertIn("rnb", T.tokens(text), text)
        for text in ("Hip-Hop/Rap", "hip hop", "hiphop", "atlanta rap"):
            self.assertIn("hiphop", T.tokens(text), text)

    def test_drops_lastfm_junk_tags(self):
        self.assertEqual(T.tokens("seen live, favourites, awesome"), set())

    def test_a_modifier_alone_is_not_a_genre_match(self):
        """`alternative r&b` must not make an alternative-rock act r&b."""
        strong = T.fit(["r&b", "soul"], T.segment_tokens(RNB))
        weak = T.fit(["alternative", "indie folk"], T.segment_tokens(RNB))
        self.assertGreaterEqual(strong, 0.9)
        self.assertLessEqual(weak, 0.3)
        self.assertGreater(strong, weak * 3)

    def test_a_mood_only_segment_yields_no_genre_tokens_anyone_matches(self):
        seg = T.segment_tokens(MOODONLY)
        self.assertEqual(T.fit(["hip-hop"], seg), 0.0)


class TestSegmentAwareBoost(unittest.TestCase):

    def setUp(self):
        con = common.connect()
        for t in ("library_tracks", "library_artists", "artist_tags",
                  "library_playlists", "library_playlist_tracks"):
            con.execute("DELETE FROM %s" % t)
        con.commit()
        seed_library()
        self.taste = T.load()

    def _boost(self, segment):
        cands = sources.fuse([(1.0, [
            cand("Big Rap Song", "Rap Guy", 1, "mood"),
            cand("Quiet Song", "Folk Person", 2, "mood"),
            cand("Unknown Song", "A Stranger", 3, "mood")])])
        T.boost(cands, self.taste, segment)
        return {c["title"]: c["weight"] for c in cands}

    def test_loads(self):
        self.assertTrue(self.taste)
        self.assertEqual(self.taste.artist("Rap Guy")["tracks"], 4)
        self.assertIn("hip-hop/rap", self.taste.artist("Rap Guy")["tags"])

    def test_matches_inside_a_multi_artist_credit(self):
        self.assertEqual(self.taste.artist("Someone, Rap Guy")["name"],
                         "Rap Guy")

    def test_the_same_owned_track_is_worth_less_in_the_wrong_block(self):
        """The whole point: 140 plays of a rapper should not carry a sleep
        block the way it carries a hip-hop block."""
        rap = self._boost(RAP)["Big Rap Song"]
        sleep = self._boost(SLEEP)["Big Rap Song"]
        self.assertGreater(rap, 3.0)
        self.assertLess(sleep, 2.5)
        self.assertGreater(rap, sleep * 2)

    def test_fit_reaches_the_track_terms_not_only_the_artist_term(self):
        """Gating only the artist multiplier leaves liked+plays segment-blind,
        and those are the big ones."""
        strangers = self._boost(SLEEP)["Unknown Song"]
        self.assertEqual(strangers, 1.0)
        # An off-genre owned track keeps *some* edge, but a small one.
        self.assertLess(self._boost(SLEEP)["Big Rap Song"] / strangers, 2.5)

    def test_boost_reorders_without_ever_filtering(self):
        cands = sources.fuse([(1.0, [
            cand("Unknown Song", "A Stranger", 1, "radio"),
            cand("Quiet Song", "Folk Person", 2, "radio"),
            cand("Big Rap Song", "Rap Guy", 3, "radio")])])
        out = T.boost(cands, self.taste, RAP)
        self.assertEqual(len(out), 3, "affinity must reorder, never drop")
        self.assertEqual(out[0]["title"], "Big Rap Song")

    def test_boost_cannot_rescue_a_track_the_sources_barely_returned(self):
        cands = sources.fuse([
            (1.0, [cand("Strong Stranger", "Nobody", 1, "radio")]),
            (1.0, [cand("Strong Stranger", "Nobody", 1, "mood")]),
            (1.0, [cand("Big Rap Song", "Rap Guy", 400, "radio")])])
        out = T.boost(cands, self.taste, RAP)
        self.assertEqual(out[0]["title"], "Strong Stranger")

    def test_a_skipped_track_is_demoted_in_every_block(self):
        cands = sources.fuse([(1.0, [
            cand("Skipped Song", "Rap Guy", 1, "mood"),
            cand("Other Rap Song", "Rap Guy", 1, "mood")])])
        T.boost(cands, self.taste, RAP)
        w = {c["title"]: c["weight"] for c in cands}
        self.assertLess(w["Skipped Song"], w["Other Rap Song"])

    def test_boost_backfills_a_video_id_from_the_library(self):
        cands = sources.fuse([(1.0, [
            cand("Big Rap Song", "Rap Guy", 1, "similar")])])
        self.assertEqual(cands[0]["video_id"], "")
        T.boost(cands, self.taste, RAP)
        self.assertEqual(cands[0]["video_id"], "vid123")

    def test_an_empty_library_is_a_no_op_not_an_error(self):
        empty = T.Taste({}, {}, {}, {}, {})
        self.assertFalse(empty)
        cands = sources.fuse([(1.0, [cand("A", "X", 1, "radio"),
                                     cand("B", "Y", 2, "radio")])])
        out = T.boost(cands, empty, RAP)
        self.assertEqual([c["title"] for c in out], ["A", "B"])
        self.assertEqual({c["weight"] for c in out}, {1.0})


class TestLibraryAsASource(unittest.TestCase):
    """A multiplier only reorders what the sources returned. If the library is
    not also a source, music you own that nobody's radio surfaces can never
    appear at all."""

    def setUp(self):
        con = common.connect()
        for t in ("library_tracks", "library_artists", "artist_tags",
                  "library_playlists", "library_playlist_tracks"):
            con.execute("DELETE FROM %s" % t)
        con.commit()
        seed_library()
        library.upsert_playlists("apple", [
            ("drives", [common.key("Big Rap Song", "Rap Guy"),
                        common.key("Quiet Song", "Folk Person")])])
        self.taste = T.load()

    def test_injects_owned_tracks_that_fit_the_block(self):
        pool = T.library_pool(self.taste, RAP)
        self.assertTrue(pool)
        self.assertTrue(all(c["source"] == "library" for c in pool))
        self.assertIn("Big Rap Song", [c["title"] for c in pool])

    def test_keeps_the_wrong_genre_out_of_a_block(self):
        """Folk survives a sleep block — it is in the same soft family as
        ambient, which is the right answer. Rap does not."""
        titles = [c["title"] for c in T.library_pool(self.taste, SLEEP)]
        self.assertNotIn("Big Rap Song", titles)
        self.assertNotIn("Other Rap Song", titles)
        self.assertIn("Quiet Song", titles)

    def test_liking_a_track_does_not_buy_it_into_the_wrong_block(self):
        liked = [t for t in self.taste.tracks.values() if t["liked"]]
        self.assertTrue(liked, "fixture should have a liked track")
        self.assertEqual(liked[0]["title"], "Big Rap Song")
        self.assertNotIn("Big Rap Song",
                         [c["title"] for c in T.library_pool(self.taste, SLEEP)])

    def test_injects_nothing_when_the_block_wants_a_genre_you_lack(self):
        classical = {"mood": "Focus", "genres": ["classical"],
                     "tags": ["opera", "orchestral"]}
        self.assertEqual(T.library_pool(self.taste, classical), [])

    def test_a_mood_only_block_still_gets_the_library(self):
        """`Sad` names no genre, so filtering on fit would drop everything and
        silently switch personalisation off for that block."""
        pool = T.library_pool(self.taste, MOODONLY)
        self.assertTrue(pool, "a genre-less segment must not filter to nothing")

    def test_your_own_playlists_are_a_source(self):
        pool = T.playlist_pool(self.taste,
                               {common.key("Big Rap Song", "Rap Guy")})
        self.assertEqual([c["title"] for c in pool], ["Quiet Song"])
        self.assertEqual(pool[0]["source"], "yours")

    def test_the_playlist_pool_widens_as_the_playlist_fills(self):
        """Block one asks what goes with one song; block three asks what goes
        with everything picked so far."""
        library.upsert_playlists("apple", [
            ("a", [common.key("Big Rap Song", "Rap Guy"),
                   common.key("Quiet Song", "Folk Person")]),
            ("b", [common.key("Other Rap Song", "Rap Guy"),
                   common.key("Third Rap Song", "Rap Guy")])])
        t = T.load()
        one = T.playlist_pool(t, {common.key("Big Rap Song", "Rap Guy")})
        two = T.playlist_pool(t, {common.key("Big Rap Song", "Rap Guy"),
                                  common.key("Other Rap Song", "Rap Guy")})
        self.assertEqual([c["title"] for c in one], ["Quiet Song"])
        self.assertEqual(sorted(c["title"] for c in two),
                         ["Quiet Song", "Third Rap Song"])

    def test_a_dumping_ground_playlist_is_not_a_statement(self):
        big = [common.key("t%d" % i, "someone") for i in range(400)]
        big[0] = common.key("Big Rap Song", "Rap Guy")
        big[1] = common.key("Quiet Song", "Folk Person")
        library.upsert_playlists("apple", [("everything I like", big)])
        t = T.load()
        self.assertEqual(
            T.playlist_pool(t, {common.key("Big Rap Song", "Rap Guy")}), [],
            "a 400-track list co-occurs everything with everything")


class TestRecency(unittest.TestCase):
    """`Date Added` as a percentile on your own timeline, not an absolute age —
    a library bulk-imported in 2015 must not read as uniformly stale."""

    def setUp(self):
        con = common.connect()
        for t in ("library_tracks", "library_artists", "artist_tags"):
            con.execute("DELETE FROM %s" % t)
        con.commit()
        library.upsert_tracks([
            {"title": "Ancient", "artist": "X", "genre": "Rock",
             "added": "2014-01-01", "source": "apple"},
            {"title": "Middling", "artist": "X", "genre": "Rock",
             "added": "2019-01-01", "source": "apple"},
            {"title": "Newest", "artist": "X", "genre": "Rock",
             "added": "2026-01-01", "source": "apple"},
        ])
        library.tags_from_track_genres("apple")
        self.taste = T.load()

    def test_ranks_oldest_to_newest_across_zero_to_one(self):
        self.assertEqual(self.taste.fresh(common.key("Ancient", "X")), 0.0)
        self.assertEqual(self.taste.fresh(common.key("Middling", "X")), 0.5)
        self.assertEqual(self.taste.fresh(common.key("Newest", "X")), 1.0)

    def test_an_undated_track_is_neutral_not_old(self):
        self.assertEqual(self.taste.fresh("no|date"), 0.5)

    def test_ties_share_a_percentile(self):
        con = common.connect()
        con.execute("DELETE FROM library_tracks")
        con.commit()
        library.upsert_tracks([
            {"title": "Bulk %d" % i, "artist": "X", "added": "2015-06-01",
             "source": "apple"} for i in range(5)])
        t = T.load()
        got = {t.fresh(common.key("Bulk %d" % i, "X")) for i in range(5)}
        self.assertEqual(got, {0.5}, "a bulk import must not fake an ordering")

    def test_recency_moves_the_weight_in_both_directions(self):
        seg = {"mood": "Rock", "genres": ["rock"], "tags": []}
        cands = sources.fuse([(1.0, [
            cand("Ancient", "X", 1, "mood"), cand("Newest", "X", 1, "mood")])])
        T.boost(cands, self.taste, seg)
        w = {c["title"]: c["weight"] for c in cands}
        self.assertGreater(w["Newest"], w["Ancient"] * 1.5)
        self.assertIn("added recently",
                      [c["aff"] for c in cands if c["title"] == "Newest"][0])

    def test_a_library_with_no_dates_is_a_no_op(self):
        con = common.connect()
        con.execute("DELETE FROM library_tracks")
        con.commit()
        library.upsert_tracks([
            {"title": "A", "artist": "X", "source": "ytmusic"},
            {"title": "B", "artist": "X", "source": "ytmusic"}])
        t = T.load()
        self.assertEqual(t.recency, {})
        self.assertEqual(t.fresh(common.key("A", "X")), 0.5)


class TestCohesion(unittest.TestCase):
    """Co-occurrence with everything picked so far, not just the seed."""

    def setUp(self):
        con = common.connect()
        for t in ("library_tracks", "library_artists", "artist_tags",
                  "library_playlists", "library_playlist_tracks"):
            con.execute("DELETE FROM %s" % t)
        con.commit()
        library.upsert_tracks([
            {"title": t, "artist": "X", "genre": "Hip-Hop/Rap",
             "source": "apple"}
            for t in ("A", "B", "C", "D", "E", "Far")])
        k = lambda t: common.key(t, "X")
        library.upsert_playlists("apple", [
            ("gym", [k("A"), k("B"), k("C"), k("D"), k("E")]),
            ("other", [k("Far")])])
        self.taste = T.load()
        self.k = k

    def test_evidence_accumulates_rather_than_saturating_at_once(self):
        """Dividing by len(chosen) pinned this at 1.0 from the first block,
        which defeated the point of generalising past the seed."""
        got = [self.taste.cohesion(self.k("A"), {self.k(x) for x in picks})
               for picks in ("B", "BC", "BCD", "BCDE")]
        self.assertEqual(got, sorted(got), "must be monotonic")
        self.assertLess(got[0], 0.5)
        self.assertEqual(got[-1], 1.0)
        self.assertGreater(got[-1], got[0] * 2)

    def test_is_zero_for_a_track_you_never_file_with_these(self):
        self.assertEqual(
            self.taste.cohesion(self.k("Far"),
                                {self.k("A"), self.k("B")}), 0.0)

    def test_is_zero_before_anything_is_picked(self):
        self.assertEqual(self.taste.cohesion(self.k("A"), set()), 0.0)

    def test_it_reorders_two_otherwise_equal_candidates(self):
        seg = {"mood": "Hip-hop", "genres": ["hip-hop"], "tags": []}
        chosen = {self.k("B"), self.k("C"), self.k("D"), self.k("E")}
        cands = sources.fuse([(1.0, [cand("Far", "X", 1, "mood"),
                                     cand("A", "X", 1, "mood")])])
        T.boost(cands, self.taste, seg, chosen)
        self.assertEqual(cands[0]["title"], "A")
        self.assertIn("you file it with these", cands[0]["aff"])

    def test_survives_a_library_with_no_playlists(self):
        con = common.connect()
        con.execute("DELETE FROM library_playlist_tracks")
        con.execute("DELETE FROM library_playlists")
        con.commit()
        t = T.load()
        self.assertEqual(t.cohesion(self.k("A"), {self.k("B")}), 0.0)
        self.assertEqual(T.playlist_pool(t, {self.k("A")}), [])


class TestEra(unittest.TestCase):
    """The only signal that reaches music you do not own — a release year is a
    fact about the record, not about your library."""

    def setUp(self):
        con = common.connect()
        for t in ("library_tracks", "library_artists", "artist_tags"):
            con.execute("DELETE FROM %s" % t)
        con.commit()
        library.upsert_tracks(
            [{"title": "t%d" % i, "artist": "X", "genre": "Hip-Hop/Rap",
              "year": "2015", "source": "apple"} for i in range(14)] +
            [{"title": "n%d" % i, "artist": "X", "genre": "Hip-Hop/Rap",
              "year": "2022", "source": "apple"} for i in range(6)])
        library.tags_from_track_genres("apple")
        self.taste = T.load()

    def test_distribution(self):
        self.assertEqual(sorted(self.taste.eras), ["2010s", "2020s"])
        self.assertAlmostEqual(self.taste.eras["2010s"], 14 / 20.0)

    def test_your_decade_scores_full_and_a_foreign_one_scores_nothing(self):
        self.assertEqual(self.taste.era_fit("2015"), 1.0)
        self.assertEqual(self.taste.era_fit("1974"), 0.0)

    def test_adjacent_decades_count_for_half(self):
        """A 2009 record is not a different world from a 2010 one."""
        self.assertAlmostEqual(self.taste.era_fit("2009"), 0.5)
        self.assertGreater(self.taste.era_fit("2022"), 0.8)

    def test_an_unknown_year_has_no_opinion_rather_than_a_bad_one(self):
        for bad in ("", None, "nope", "20", "n/a"):
            self.assertIsNone(self.taste.era_fit(bad), repr(bad))

    def test_a_full_release_date_reads_as_its_year(self):
        """Spotify ships `release_date`, not a bare year."""
        self.assertEqual(self.taste.era_fit("2015-06-01"), 1.0)
        self.assertEqual(self.taste.era_fit(2015), 1.0)

    def test_too_few_dated_tracks_switches_the_signal_off(self):
        con = common.connect()
        con.execute("DELETE FROM library_tracks")
        con.commit()
        library.upsert_tracks([{"title": "a", "artist": "X", "year": "2020",
                                "source": "apple"}])
        t = T.load()
        self.assertEqual(t.eras, {})
        self.assertIsNone(t.era_fit("2020"))
        self.assertEqual(T.era_hint(t), "")

    def test_it_reaches_a_track_you_do_not_own(self):
        seg = {"mood": "Hip-hop", "genres": ["hip-hop"], "tags": []}
        cands = sources.fuse([(1.0, [
            sources._cand("Old", "Stranger", 1, "radio", year="1978"),
            sources._cand("New", "Stranger", 1, "radio", year="2016"),
            sources._cand("Undated", "Stranger", 1, "mood")])])
        T.boost(cands, self.taste, seg)
        w = {c["title"]: c["weight"] for c in cands}
        self.assertEqual(w["Undated"], 1.0, "unknown year must be neutral")
        self.assertGreater(w["New"], 1.0)
        self.assertLess(w["Old"], 1.0)
        self.assertGreater(w["New"], w["Old"] * 1.5)

    def test_the_hint_names_only_decades_worth_mentioning(self):
        self.assertEqual(T.era_hint(self.taste), "2010s 70%, 2020s 30%")

    def test_fusion_carries_a_year_from_whichever_source_knew_it(self):
        """Only the radio reports one; the merged row must keep it."""
        merged = sources.fuse([
            (1.0, [sources._cand("Song", "X", 1, "similar")]),
            (1.0, [sources._cand("Song", "X", 2, "radio", year="2015")])])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["year"], "2015")


class TestFeedback(unittest.TestCase):
    """The one channel where the listener says it outright, so the only one
    allowed to remove a candidate instead of demoting it."""

    def setUp(self):
        con = common.connect()
        con.execute("DELETE FROM feedback")
        con.execute("DELETE FROM last_playlist")
        con.commit()
        self.blocks = [
            {"segment": {"mood": "Hip-hop", "tags": ["trap"],
                         "label": "bangers", "minutes": 10},
             "tracks": [{"title": "Alpha", "artist": "Rap Guy",
                         "key": common.key("Alpha", "Rap Guy")},
                        {"title": "Beta", "artist": "Rap Guy",
                         "key": common.key("Beta", "Rap Guy")}],
             "seconds": 400},
            {"segment": {"mood": "Sleep", "tags": [], "label": "down",
                         "minutes": 10},
             "tracks": [{"title": "Gamma", "artist": "Quiet One",
                         "key": common.key("Gamma", "Quiet One")}],
             "seconds": 300}]
        F.remember(self.blocks, "bangers then sleep")

    def test_remember_numbers_across_blocks(self):
        rows = F.last()
        self.assertEqual([r["pos"] for r in rows], [1, 2, 3])
        self.assertEqual(rows[2]["title"], "Gamma")
        self.assertEqual(rows[2]["mood"], "Sleep",
                         "a verdict must know which block it happened in")
        self.assertEqual(rows[0]["request"], "bangers then sleep")

    def test_resolve_accepts_positions_ranges_and_names(self):
        rows = F.last()
        self.assertEqual([r["pos"] for r in F._resolve(rows, ["2"])], [2])
        self.assertEqual([r["pos"] for r in F._resolve(rows, ["1-3"])],
                         [1, 2, 3])
        self.assertEqual([r["pos"] for r in F._resolve(rows, ["all"])],
                         [1, 2, 3])
        self.assertEqual([r["pos"] for r in F._resolve(rows, ["gamma"])], [3])
        self.assertEqual([r["pos"] for r in F._resolve(rows, ["Rap Guy"])],
                         [1, 2])
        self.assertEqual([r["pos"] for r in F._resolve(rows, ["2", "2"])], [2])

    def test_resolve_refuses_rather_than_guessing(self):
        with self.assertRaises(SystemExit):
            F._resolve(F.last(), ["99"])
        with self.assertRaises(SystemExit):
            F._resolve(F.last(), ["nothing like this"])

    def test_a_rejection_is_scoped_to_the_block_it_happened_in(self):
        """Dropping a rap track from a sleep block must not delete it from
        your life."""
        F.record(["1"], -1, quiet=True)
        L = F.load()
        k = common.key("Alpha", "Rap Guy")
        self.assertTrue(L.vetoed(k, "Hip-hop"))
        self.assertIsNone(L.vetoed(k, "Sleep"))
        self.assertIsNone(L.vetoed(k, "Party"))

    def test_but_it_still_counts_against_it_elsewhere(self):
        F.record(["1"], -1, quiet=True)
        w, why = F.load().weight(common.key("Alpha", "Rap Guy"), "Rap Guy",
                                 "Sleep")
        self.assertLess(w, 1.0)
        self.assertIn("elsewhere", why)

    def test_rejecting_in_two_unrelated_blocks_widens_the_veto(self):
        F.record(["1"], -1, quiet=True)
        F.remember([{"segment": {"mood": "Sleep", "tags": []},
                     "tracks": [{"title": "Alpha", "artist": "Rap Guy",
                                 "key": common.key("Alpha", "Rap Guy")}],
                     "seconds": 200}], "quiet please")
        F.record(["1"], -1, quiet=True)
        L = F.load()
        k = common.key("Alpha", "Rap Guy")
        for mood in ("Hip-hop", "Sleep", "Party", "Workout"):
            self.assertTrue(L.vetoed(k, mood), mood)

    def test_keeping_is_the_mirror_of_rejecting(self):
        F.record(["1"], +1, quiet=True)
        L = F.load()
        k = common.key("Alpha", "Rap Guy")
        self.assertIsNone(L.vetoed(k, "Hip-hop"))
        here, _ = L.weight(k, "Rap Guy", "Hip-hop")
        away, _ = L.weight(k, "Rap Guy", "Sleep")
        self.assertGreater(here, away)
        self.assertGreater(away, 1.0)

    def test_one_rejection_does_not_condemn_an_artist(self):
        """The system must not flinch at noise and narrow itself to nothing."""
        F.record(["1"], -1, quiet=True)
        w, why = F.load().weight("some|other", "Rap Guy", "Hip-hop")
        self.assertEqual(w, 1.0)
        self.assertEqual(why, "")

    def test_a_pattern_does(self):
        L = F.Learned({}, {common.norm("Rap Guy"): {"Hip-hop": (3, 1)}})
        w, why = L.weight("some|other", "Rap Guy", "Hip-hop")
        self.assertLess(w, 0.7)
        self.assertIn("3 of 4", why)
        # ...and only in the block the pattern was in.
        self.assertEqual(L.weight("some|other", "Rap Guy", "Party")[0], 1.0)

    def test_apply_removes_vetoed_and_reweighs_the_rest(self):
        F.record(["1"], -1, quiet=True)
        F.record(["2"], +1, quiet=True)
        cands = sources.fuse([(1.0, [
            cand("Alpha", "Rap Guy", 1, "mood"),
            cand("Beta", "Rap Guy", 2, "mood"),
            cand("Delta", "Nobody", 3, "mood")])])
        for c in cands:
            c["rank_score"] = c["rrf"]
        dropped = F.apply(cands, F.load(), {"mood": "Hip-hop"})
        self.assertEqual([c["title"] for c in dropped], ["Alpha"])
        self.assertEqual(sorted(c["title"] for c in cands),
                         ["Beta", "Delta"])
        self.assertEqual(cands[0]["title"], "Beta",
                         "the kept track should now outrank the stranger")
        self.assertIn("kept", cands[0]["aff"])

    def test_apply_is_a_no_op_with_nothing_learned(self):
        cands = sources.fuse([(1.0, [cand("A", "X", 1, "mood"),
                                     cand("B", "Y", 2, "mood")])])
        before = [c["title"] for c in cands]
        self.assertEqual(F.apply(cands, F.load(), {"mood": "Hip-hop"}), [])
        self.assertEqual([c["title"] for c in cands], before)

    def test_forget_one_and_forget_all(self):
        F.record(["1", "2"], -1, quiet=True)
        self.assertEqual(F.forget("Alpha"), 1)
        self.assertIsNone(F.load().vetoed(common.key("Alpha", "Rap Guy"),
                                          "Hip-hop"))
        self.assertTrue(F.load().vetoed(common.key("Beta", "Rap Guy"),
                                        "Hip-hop"))
        self.assertEqual(F.forget("all"), 1)
        self.assertFalse(F.load())

    def test_summary_reports_nothing_when_there_is_nothing(self):
        self.assertIsNone(F.summary())
        F.record(["1"], -1, quiet=True)
        s = F.summary()
        self.assertEqual(len(s["drops"]), 1)
        self.assertEqual(len(s["keeps"]), 0)


class FakeYT(object):
    """Enough of YTMusic to exercise the push paths without an account."""

    def __init__(self, playlists=None, missing=()):
        self.playlists = playlists or {}     # pid -> [(videoId, setVideoId)]
        self.missing = set(missing)          # pids that 404
        self.calls = []
        self.next_id = 100

    def get_playlist(self, pid, limit=100, **kw):
        if pid in self.missing or pid not in self.playlists:
            raise RuntimeError("404")
        return {"tracks": [{"videoId": v, "setVideoId": s}
                           for v, s in self.playlists[pid]]}

    def create_playlist(self, title, desc, privacy="PRIVATE", video_ids=None,
                        **kw):
        pid = "PL%d" % self.next_id
        self.next_id += 1
        self.playlists[pid] = [(v, "set-" + v) for v in (video_ids or [])]
        self.calls.append(("create", title, list(video_ids or [])))
        return pid

    def add_playlist_items(self, pid, videoIds=None, duplicates=False, **kw):
        self.calls.append(("add", pid, list(videoIds or [])))
        self.playlists[pid] += [(v, "set-" + v) for v in (videoIds or [])]
        return "STATUS_SUCCEEDED"

    def remove_playlist_items(self, pid, videos):
        gone = {v["videoId"] for v in videos}
        self.calls.append(("remove", pid, sorted(gone)))
        self.playlists[pid] = [(v, s) for v, s in self.playlists[pid]
                               if v not in gone]
        return "STATUS_SUCCEEDED"

    def search(self, *a, **kw):
        return []


def track(title, artist, vid):
    return {"title": title, "artist": artist, "video_id": vid}


class TestPushDedupe(unittest.TestCase):
    """A tool that only ever calls create_playlist turns an account into a
    landfill: four evenings of "hip hop bangers" is four playlists."""

    def setUp(self):
        con = common.connect()
        con.execute("DELETE FROM pushed")
        con.commit()
        self.yt = FakeYT()
        self._client, self._require = push.client, None
        push.client = lambda need_auth=True: self.yt
        import ytauth
        self._require = ytauth.require
        ytauth.require = lambda action="": {"ok": True}

    def tearDown(self):
        push.client = self._client
        import ytauth
        ytauth.require = self._require

    def _push(self, tracks, title="Bangers", **kw):
        # push narrates to stderr, which is right in use and noise in a test.
        with contextlib.redirect_stderr(io.StringIO()):
            return push.create(tracks, title, request="r", quiet=True, **kw)

    # -- the pure parts ---------------------------------------------------

    def test_fingerprint_ignores_order_but_not_content(self):
        self.assertEqual(push.fingerprint(["a", "b"]),
                         push.fingerprint(["b", "a"]))
        self.assertNotEqual(push.fingerprint(["a", "b"]),
                            push.fingerprint(["a", "c"]))
        self.assertEqual(push.fingerprint([]), "")

    def test_dedupe_catches_two_titles_for_one_recording(self):
        """Fusion merges on title+artist, so `Often` and `Often (Kygo Remix)`
        survive as two rows pointing at one video."""
        pairs = [(track("Often", "The Weeknd", "v1"), "v1"),
                 (track("Often (Kygo Remix)", "The Weeknd", "v1"), "v1"),
                 (track("Other", "X", "v2"), "v2")]
        kept, dropped = push.dedupe(pairs)
        self.assertEqual([v for _, v in kept], ["v1", "v2"])
        self.assertEqual(len(dropped), 1)

    def test_decide(self):
        hist = [{"title": "Bangers", "fingerprint": push.fingerprint(["a"]),
                 "playlist_id": "PL1"}]
        self.assertEqual(push.decide("Bangers", ["a"], hist)[0], "unchanged")
        self.assertEqual(push.decide("Bangers", ["a", "b"], hist)[0], "update")
        self.assertEqual(push.decide("Other", ["a"], hist)[0], "create")
        self.assertEqual(push.decide("Bangers", ["a"], hist, new=True)[0],
                         "create")

    def test_decide_never_touches_a_playlist_we_did_not_make(self):
        """Title collision with a hand-made playlist must not update it."""
        self.assertEqual(push.decide("My Own Mix", ["a"], [])[0], "create")

    # -- end to end against the fake -------------------------------------

    def test_first_push_creates(self):
        pid = self._push([track("A", "X", "v1"), track("B", "Y", "v2")])
        self.assertEqual([c[0] for c in self.yt.calls], ["create"])
        self.assertEqual(len(push.pushed()), 1)
        self.assertEqual(push.pushed()[0]["n"], 2)
        self.assertIn(pid, self.yt.playlists)

    def test_pushing_the_same_thing_twice_does_nothing(self):
        first = self._push([track("A", "X", "v1")])
        self.yt.calls = []
        again = self._push([track("A", "X", "v1")])
        self.assertEqual(again, first)
        self.assertEqual(self.yt.calls, [], "must not write anything")
        self.assertEqual(len(push.pushed()), 1)

    def test_a_changed_playlist_updates_in_place_and_keeps_its_url(self):
        first = self._push([track("A", "X", "v1"), track("B", "Y", "v2")])
        self.yt.calls = []
        again = self._push([track("A", "X", "v1"), track("C", "Z", "v3")])
        self.assertEqual(again, first, "the URL must survive an update")
        kinds = {c[0]: c for c in self.yt.calls}
        self.assertEqual(kinds["add"][2], ["v3"])
        self.assertEqual(kinds["remove"][2], ["v2"])
        self.assertNotIn("create", kinds)
        self.assertEqual(sorted(v for v, _ in self.yt.playlists[first]),
                         ["v1", "v3"])
        self.assertEqual(len(push.pushed()), 1)

    def test_new_forces_a_second_playlist(self):
        first = self._push([track("A", "X", "v1")])
        second = self._push([track("A", "X", "v1")], new=True)
        self.assertNotEqual(first, second)
        self.assertEqual(len(push.pushed()), 2)

    def test_a_deleted_playlist_does_not_turn_a_push_into_a_no_op(self):
        """The local record can be stale — you deleted it from the app."""
        first = self._push([track("A", "X", "v1")])
        self.yt.missing.add(first)
        self.yt.calls = []
        again = self._push([track("A", "X", "v1")])
        self.assertNotEqual(again, first)
        self.assertEqual([c[0] for c in self.yt.calls], ["create"])

    def test_a_deleted_playlist_falls_back_to_create_on_update_too(self):
        first = self._push([track("A", "X", "v1")])
        self.yt.missing.add(first)
        again = self._push([track("B", "Y", "v2")])
        self.assertNotEqual(again, first)
        self.assertEqual([c[0] for c in self.yt.calls
                          if c[0] == "create"], ["create", "create"])

    def test_duplicates_never_reach_the_playlist(self):
        pid = self._push([track("Often", "The Weeknd", "v1"),
                          track("Often (Remix)", "The Weeknd", "v1"),
                          track("Other", "X", "v2")])
        self.assertEqual(sorted(v for v, _ in self.yt.playlists[pid]),
                         ["v1", "v2"])

    def test_nothing_resolvable_writes_nothing(self):
        self.assertIsNone(self._push([{"title": "Ghost", "artist": "Nobody"}]))
        self.assertEqual(self.yt.calls, [])
        self.assertEqual(push.pushed(), [])


class TestYouTubeMusicAuth(unittest.TestCase):
    """Expired credentials return a valid, empty library rather than an error,
    so every check here is about refusing to confuse that with an empty
    account."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._real_path = ytauth.path

    def tearDown(self):
        ytauth.path = self._real_path

    def _at(self, contents):
        p = os.path.join(self.dir, "browser.json")
        with open(p, "w") as fh:
            fh.write(contents)
        ytauth.path = lambda: p
        return p

    def test_no_credentials_is_reported_not_guessed(self):
        ytauth.path = lambda: None
        out = ytauth.health()
        self.assertFalse(out["ok"])
        self.assertIn("no credentials", out["why"])

    def test_unparseable_file(self):
        self._at("not json at all")
        out = ytauth.health()
        self.assertFalse(out["ok"])
        self.assertIn("not valid JSON", out["why"])

    def test_headers_copied_from_an_unauthenticated_request(self):
        self._at(json.dumps({"accept": "*/*", "x-origin": "https://x"}))
        out = ytauth.health()
        self.assertFalse(out["ok"])
        self.assertIn("no Cookie header", out["why"])

    def test_a_logged_out_cookie_is_caught_before_any_request(self):
        self._at(json.dumps({"cookie": "VISITOR_INFO1_LIVE=abc; NID=1",
                             "authorization": "SAPISIDHASH 1_x"}))
        out = ytauth.health()
        self.assertFalse(out["ok"])
        self.assertIn("SAPISID", out["why"])

    def test_reads_chromes_copy_as_fetch_blob(self):
        blob = ('await fetch("https://music.youtube.com/youtubei/v1/browse", {'
                '"headers": {"accept": "*/*", "cookie": "SAPISID=x",'
                ' "authorization": "SAPISIDHASH 1_a"},'
                ' "body": "{\\"a\\": {\\"b\\": 1}}", "method": "POST"});')
        got = ytauth._from_fetch(blob)
        self.assertIn("cookie: SAPISID=x", got)
        self.assertIn("authorization: SAPISIDHASH 1_a", got)
        self.assertNotIn("body", got)

    def test_a_plain_header_list_is_left_alone(self):
        self.assertIsNone(ytauth._from_fetch("cookie: a=b\nauthorization: c"))

    def test_a_fetch_blob_with_broken_json_does_not_explode(self):
        self.assertIsNone(ytauth._from_fetch('{"headers": {oops}}'))


class TestDiscoveryCap(unittest.TestCase):

    def setUp(self):
        con = common.connect()
        con.execute("DELETE FROM library_tracks")
        con.execute("DELETE FROM library_artists")
        con.commit()
        seed_library()
        self.taste = T.load()

    def test_holds_the_ratio_while_preserving_order(self):
        picked = [{"key": common.key("Big Rap Song", "Rap Guy"), "t": 1},
                  {"key": "not|owned", "t": 2},
                  {"key": common.key("Other Rap Song", "Rap Guy"), "t": 3},
                  {"key": common.key("Third Rap Song", "Rap Guy"), "t": 4},
                  {"key": "also|new", "t": 5},
                  {"key": "third|new", "t": 6}]
        out = blend._cap_owned(picked, self.taste, want=4, max_owned=0.5)
        owned = sum(1 for c in out if c["key"] in self.taste.tracks)
        self.assertEqual(owned, 2)
        self.assertEqual([c["t"] for c in out], sorted(c["t"] for c in out))

    def test_would_rather_break_the_ratio_than_hand_back_a_short_block(self):
        """A shortlist that is nearly all yours must still fill the block."""
        picked = [{"key": common.key(t, "Rap Guy"), "t": i} for i, t in
                  enumerate(("Big Rap Song", "Other Rap Song",
                             "Third Rap Song", "Skipped Song"))]
        out = blend._cap_owned(picked, self.taste, want=4, max_owned=0.5)
        self.assertEqual(len(out), 4)
        self.assertEqual([c["t"] for c in out], [0, 1, 2, 3])

    def test_still_prefers_discovery_when_there_is_enough_of_it(self):
        picked = [{"key": common.key("Big Rap Song", "Rap Guy"), "t": 0},
                  {"key": common.key("Other Rap Song", "Rap Guy"), "t": 1},
                  {"key": "a|new", "t": 2}, {"key": "b|new", "t": 3},
                  {"key": "c|new", "t": 4}]
        out = blend._cap_owned(picked, self.taste, want=4, max_owned=0.25)
        self.assertEqual(sum(1 for c in out
                             if c["key"] in self.taste.tracks), 1)

    def test_is_a_no_op_with_no_library(self):
        picked = [{"key": "a|b"}, {"key": "c|d"}]
        self.assertEqual(
            blend._cap_owned(picked, T.Taste({}, {}, {}, {}, {}), 2, 0.5),
            picked)


class TestSchemaRelaxation(unittest.TestCase):
    """The API enforces a subset of JSON Schema. Measured against the live
    endpoint: enum, minLength, minItems, nested objects and optional
    properties are accepted; minimum, maximum and maxItems return a 400."""

    def test_strips_only_the_three_rejected_keywords(self):
        out, notes = common._relax({
            "type": "object", "additionalProperties": False,
            "required": ["n"],
            "properties": {
                "n": {"type": "integer", "minimum": 5, "maximum": 240},
                "tags": {"type": "array", "maxItems": 4, "minItems": 1,
                         "items": {"type": "string"}},
                "mode": {"type": "string", "enum": ["a", "b"]}}})
        props = out["properties"]
        self.assertEqual(props["n"], {"type": "integer"})
        self.assertEqual(props["tags"]["minItems"], 1)
        self.assertNotIn("maxItems", props["tags"])
        self.assertEqual(props["mode"]["enum"], ["a", "b"])
        self.assertEqual(out["required"], ["n"])
        self.assertIs(out["additionalProperties"], False)
        self.assertEqual(len(notes), 3)

    def test_every_dropped_bound_is_restated_with_its_path(self):
        _, notes = common._relax({
            "type": "object",
            "properties": {"seg": {
                "type": "array", "maxItems": 5,
                "items": {"type": "object", "properties": {
                    "minutes": {"type": "integer", "minimum": 3}}}}}})
        self.assertIn("seg: at most 5 items", notes)
        self.assertIn("seg.minutes: at least 3", notes)

    def test_the_real_schemas_come_back_clean(self):
        import plan
        for schema in (plan.SCHEMA, blend.PICK_SCHEMA):
            relaxed, notes = common._relax(schema)
            text = str(relaxed)
            for bad in ("minimum", "maximum", "maxItems"):
                self.assertNotIn(bad, text)
            self.assertTrue(notes, "bounds should be restated, not lost")

    def test_leaves_an_already_clean_schema_untouched(self):
        clean = {"type": "object", "additionalProperties": False,
                 "required": ["a"], "properties": {"a": {"type": "string"}}}
        out, notes = common._relax(clean)
        self.assertEqual(out, clean)
        self.assertEqual(notes, [])


class TestTrim(unittest.TestCase):

    def make(self, *secs):
        return [{"title": "t%d" % i, "secs": s} for i, s in enumerate(secs)]

    def test_fills_the_budget(self):
        out, total, _ = blend.trim(self.make(200, 200, 200, 200, 200), 10)
        self.assertEqual(len(out), 3)
        self.assertEqual(total, 600)

    def test_skips_an_overlong_track_instead_of_ending_the_block(self):
        out, total, _ = blend.trim(self.make(200, 900, 200, 200), 10)
        self.assertEqual([c["title"] for c in out], ["t0", "t2", "t3"])
        self.assertEqual(total, 600)

    def test_overshoot_is_charged_to_the_next_block(self):
        _, total, carry = blend.trim(self.make(400, 400), 10)
        self.assertEqual(total, 800)
        self.assertEqual(carry, 200)
        out2, total2, _ = blend.trim(self.make(300, 300, 300), 10, carry)
        self.assertLessEqual(total2, 600)

    def test_unknown_durations_use_the_average(self):
        out, total, _ = blend.trim([{"title": "a", "secs": 0}] * 5, 10)
        self.assertEqual(len(out), 3)
        self.assertEqual(total, 3 * blend.AVG_SECS)

    def test_always_yields_at_least_one_track(self):
        out, _, _ = blend.trim(self.make(1200), 3)
        self.assertEqual(len(out), 1)


class TestPlanRepair(unittest.TestCase):

    def setUp(self):
        import plan
        self.plan = plan

    def test_minutes_are_rescaled_to_the_total(self):
        out = self.plan._tidy(
            {"minutes": 30, "mode": "shift", "segments": [
                {"label": "a", "mood": "Sad", "minutes": 20, "note": ""},
                {"label": "b", "mood": "Chill", "minutes": 20, "note": ""},
                {"label": "c", "mood": "Feel good", "minutes": 20, "note": ""}]},
            ["Sad", "Chill", "Feel good"])
        self.assertEqual(sum(s["minutes"] for s in out["segments"]), 30)

    def test_a_near_miss_mood_snaps_to_a_real_pool(self):
        out = self.plan._tidy(
            {"minutes": 20, "mode": "sustain", "segments": [
                {"label": "a", "mood": "sad", "minutes": 20, "note": ""}]},
            ["Sad", "Chill"])
        self.assertEqual(out["segments"][0]["mood"], "Sad")

    def test_an_invented_mood_falls_back_rather_than_selecting_nothing(self):
        out = self.plan._tidy(
            {"minutes": 20, "mode": "sustain", "segments": [
                {"label": "a", "mood": "Melancholic", "minutes": 20,
                 "note": ""}]},
            ["Sad", "Chill"])
        self.assertIn(out["segments"][0]["mood"], ("Sad", "Chill"))

    def test_multiple_segments_are_never_labelled_sustain(self):
        out = self.plan._tidy(
            {"minutes": 20, "mode": "sustain", "segments": [
                {"label": "a", "mood": "Sad", "minutes": 10, "note": ""},
                {"label": "b", "mood": "Chill", "minutes": 10, "note": ""}]},
            ["Sad", "Chill"])
        self.assertEqual(out["mode"], "shift")


if __name__ == "__main__":
    unittest.main(verbosity=2)
