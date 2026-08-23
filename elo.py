#!/usr/bin/env python3
"""elo — pick music by what it is ABOUT and where it leaves you.

    python elo.py sustain "Tell Your Friends" "The Weeknd"
    python elo.py shift sad hyped 30
    python elo.py similar "Tell Your Friends" "The Weeknd"
    python elo.py moods

Three commands over two halves of the same pipeline (see DESIGN.md §4):

  CANDIDATE GENERATION            SCORING                 SELECTION
  `similar` — co-listening,       the mood card —         `sustain` holds a
  no model anywhere. What         what the song is        posture; `shift`
  exists near this song,          about, from its         walks between two.
  including music you do          lyrics.
  not own.

`sustain` and `shift` select over the `moods` table — run `python lyrics.py`
then `python tag.py` first. `similar` needs neither and works today.
"""
import argparse
import json
import math
import sys

import common
import engine as E
import lyrics as L
import tag as T

# Where the named moods sit in (valence, energy), both 0-10. These are the
# anchors `shift` interpolates between; they are judgement calls, not
# measurements, and are meant to be argued with.
MOODS = {
    "heartbroken": (1.0, 3.0), "sad": (2.0, 2.5), "angry": (2.5, 8.5),
    "numb": (3.0, 1.5), "reflective": (4.0, 2.5), "calm": (5.5, 1.5),
    "focus": (5.0, 4.0), "romantic": (6.5, 3.5), "chill": (6.0, 3.0),
    "content": (7.0, 4.0), "happy": (8.0, 6.0), "hyped": (8.5, 9.0),
}
# SPAN and AVG_SECONDS now live in engine.py, with the rest of the math.

CARD_COLS = ("id", "title", "artist", "genre", "seconds", "themes", "stance",
             "valence", "energy", "summary", "confidence", "basis")


def cards(con, external=False):
    q = ("SELECT t.id, t.title, t.artist, t.genre, t.seconds, m.themes,"
         " m.stance, m.valence, m.energy, m.summary, m.confidence, m.basis"
         " FROM moods m JOIN tracks t ON t.id = m.track_id")
    if not external:
        q += " WHERE t.external = 0"
    out = []
    for r in con.execute(q):
        c = dict(zip(CARD_COLS, r))
        c["themes"] = json.loads(c["themes"])
        out.append(c)
    return out


def require_cards(con, known_only):
    pool = [c for c in cards(con)
            if not known_only or c["confidence"] == "known"]
    if not pool:
        sys.exit("no tagged tracks — run: python lyrics.py && python tag.py")
    return pool


def show(c, prefix=""):
    print("%s%s — %s" % (prefix, c["title"], c["artist"] or "(unknown artist)"))
    print("%s   %s | %s | valence %.1f  energy %.1f  [%s]"
          % (" " * len(prefix), "/".join(c["themes"]), c["stance"],
             c["valence"], c["energy"], c["confidence"]))
    if c["summary"]:
        print("%s   %s" % (" " * len(prefix), c["summary"]))


# ------------------------------------------------------------------ seeding

def find_seed(con, title, artist):
    """Prefer a track you own; otherwise look the seed up and tag it once."""
    want_t, want_a = common.norm(title), common.norm(artist)
    for c in cards(con, external=True):
        if common.norm(c["title"]) == want_t and (
                not want_a or common.norm(c["artist"]) == want_a):
            return c

    row = con.execute("SELECT id FROM tracks WHERE title=? AND artist=?"
                      " AND album=''", (title, artist)).fetchone()
    if not row:
        con.execute("INSERT INTO tracks (title, artist, album, external)"
                    " VALUES (?,?,'',1)", (title, artist))
        con.commit()
        row = con.execute("SELECT id FROM tracks WHERE title=? AND artist=?"
                          " AND album=''", (title, artist)).fetchone()
    tid = row[0]
    print("seed is not in your library — looking it up", file=sys.stderr)
    L.fetch(con, [{"id": tid, "title": title, "artist": artist}])
    _tag_one(con, tid)
    for c in cards(con, external=True):
        if c["id"] == tid:
            return c
    sys.exit("could not build a mood card for that seed")


def _tag_one(con, tid):
    r = con.execute("SELECT id, title, artist, album, genre, year FROM tracks"
                    " WHERE id=?", (tid,)).fetchone()
    t = dict(zip(("id", "title", "artist", "album", "genre", "year"), r))
    text = L.load(con, [tid]).get(tid, ("none", ""))
    body = T.block(t, (text[1] or "")[:T.LYRIC_CAP])
    out = common.llm(T.RUBRIC + "\n\nTHEMES: " + ", ".join(T.THEMES) +
                     "\nSTANCES: " + ", ".join(T.STANCES) + "\n\n" + body,
                     T.SCHEMA)
    T.save(con, [(tid, json.dumps(c["themes"]), c["stance"], c["valence"],
                  c["energy"], c["summary"],
                  "lyrics" if text[0] != "none" else "metadata",
                  c["confidence"]) for c in out["cards"]])


# ------------------------------------------------------------------ sustain

def sustain_cmd(con, title, artist, n, known_only, library_only, budget):
    import pool as P

    seed = find_seed(con, title, artist)
    print("\nSEED")
    show(seed, "  ")

    if library_only:
        cand = require_cards(con, known_only)
    else:
        # The pool is the corpus, not the library. With an empty database this
        # still works: candidates come from co-listening and get tagged on the
        # way through. See DESIGN.md §4.
        cand = P.build(con, seed["title"], seed["artist"], budget=budget)
        if known_only:
            cand = [c for c in cand if c["confidence"] == "known"]
        if not cand:
            sys.exit("no candidates — neither source knows that song")

    cand = [c for c in cand if c["id"] != seed["id"]]
    ranked = E.sustain(seed, cand, n, w_owned=P.OWNED_BONUS)
    owned = sum(1 for c in ranked if c.get("owned"))
    print("\nSTAYS IN THAT MOOD  (%d of %d candidates, %d yours)"
          % (len(ranked), len(cand), owned))
    for i, c in enumerate(ranked, 1):
        show(c, "%d. " % i)
        print("     match %.2f%s" % (E.sustain_score(seed, c, w_owned=P.OWNED_BONUS),
                                     "   ● yours" if c.get("owned") else ""))
    return ranked


# -------------------------------------------------------------------- shift

def shift_cmd(con, start, end, minutes, known_only, seed="", budget=60,
              genres=(), library_only=False):
    """Walk from one named mood to another. The picking is engine.shift — pure,
    constrained and unit-tested; this function only resolves names and prints."""
    import pool as P

    for m in (start, end):
        if m not in MOODS:
            sys.exit("unknown mood %r — try: %s" % (m, ", ".join(sorted(MOODS))))
    a, b = MOODS[start], MOODS[end]

    if library_only:
        pool = require_cards(con, known_only)
    elif seed:
        # Candidates from co-listening around a named song.
        title, _, artist = seed.partition("|")
        pool = P.build(con, title.strip(), artist.strip(), budget=budget,
                       include_library=True)
    else:
        # No seed and no library: genre hints (or the global chart) supply the
        # pool. This is the path a brand-new user takes, so it must not depend
        # on owning anything. See DESIGN.md §4.
        pool = P.discover(con, genres, budget=budget, include_library=True)
    if known_only:
        pool = [c for c in pool if c["confidence"] == "known"]
    if not pool:
        sys.exit("no candidates — try --genre, or a --seed \"Title|Artist\"")

    print("\n%s -> %s over %d min" % (start, end, minutes))
    return render_shift(pool, a, b, minutes)


def render_shift(pool, a, b, minutes):
    """Run and print a shift. Shared by `shift` (named anchors) and `make`
    (coordinates parsed from natural language)."""
    print("  from valence %.1f energy %.1f  to  valence %.1f energy %.1f"
          % (a + b))

    steps = E.shift(pool, a, b, minutes)
    if not steps:
        sys.exit("nothing to pick from")
    total = E.duration([s.card for s in steps])

    print("\nSHIFT  (%d tracks, %d min %02d sec)"
          % (len(steps), total // 60, total % 60))
    for s in steps:
        show(s.card, "%d. " % (s.index + 1))
        print("     target v%.1f e%.1f%s"
              % (s.target[0], s.target[1],
                 "   ! " + "; ".join(s.broke) if s.broke else ""))

    r = E.ramp_report(steps)
    print("\nramp: %.0f%% of steps move toward the destination; "
          "biggest energy jump %.1f, mean %.1f"
          % (100 * r["monotonic_frac"], r["max_jump"], r["mean_jump"]))
    print("(energy stands in for tempo — there is no BPM for this library, "
          "see DESIGN.md §2.4)")
    return [s.card for s in steps]


# --------------------------------------------------------------------- make

def make_cmd(con, text, n, known_only, budget, library_only):
    """Natural language to playlist. The command the product actually ships."""
    import intent
    import pool as P

    spec = intent.parse(text)
    print("\n%s" % intent.describe(spec))
    print("  %s\n" % spec.get("reason", ""))

    seed_title = spec.get("seed_title") or ""
    seed_artist = spec.get("seed_artist") or ""

    if library_only:
        cand = require_cards(con, known_only)
    elif seed_title:
        cand = P.build(con, seed_title, seed_artist, budget=budget,
                       include_library=True)
    else:
        cand = P.discover(con, spec.get("genres") or [], budget=budget,
                          include_library=True)
    if known_only:
        cand = [c for c in cand if c["confidence"] == "known"]
    if not cand:
        sys.exit("no candidates for that request")

    a = (spec["start"]["valence"], spec["start"]["energy"])
    b = (spec["end"]["valence"], spec["end"]["energy"])

    if spec["mode"] == "shift":
        return render_shift(cand, a, b, spec["minutes"])

    # Sustain needs something to hold. A named song is the best anchor because
    # its card is measured rather than imagined; without one, the spec itself
    # describes the point to sit on, so build a card-shaped target from it.
    if seed_title:
        seed = find_seed(con, seed_title, seed_artist)
        print("SEED")
        show(seed, "  ")
    else:
        seed = {"id": -1, "title": "(your request)", "artist": "",
                "themes": spec.get("themes") or [],
                "stance": spec.get("stance") or "",
                "valence": a[0], "energy": a[1]}

    cand = [c for c in cand if c["id"] != seed["id"]]
    ranked = E.sustain(seed, cand, n, w_owned=P.OWNED_BONUS)
    owned = sum(1 for c in ranked if c.get("owned"))
    print("\nSTAYS IN THAT MOOD  (%d of %d candidates, %d yours)"
          % (len(ranked), len(cand), owned))
    for i, c in enumerate(ranked, 1):
        show(c, "%d. " % i)
        print("     match %.2f%s"
              % (E.sustain_score(seed, c, w_owned=P.OWNED_BONUS),
                 "   ● yours" if c.get("owned") else ""))
    return ranked


# ------------------------------------------------------------------- moods

def moods_cmd(con, known_only):
    pool = require_cards(con, known_only)
    print("%d tagged tracks\n" % len(pool))
    print("named moods and how many of your tracks sit within 2.0 of each:")
    for name, (v, e) in sorted(MOODS.items(), key=lambda x: x[1]):
        near = [c for c in pool
                if math.hypot(c["valence"] - v, c["energy"] - e) <= 2.0]
        print("  %-12s v%.1f e%.1f   %3d tracks" % (name, v, e, len(near)))


# --------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--known-only", action="store_true",
                    help="only tracks whose card the model is confident in")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sustain", help="tracks that hold the seed's mood")
    p.add_argument("title")
    p.add_argument("artist", nargs="?", default="")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--library-only", action="store_true",
                   help="select only from music you own (default: the corpus)")
    p.add_argument("--budget", type=int, default=60,
                   help="max uncached candidates to tag for this request")
    p.add_argument("--push", metavar="NAME", default="",
                   help="create this playlist on YouTube Music")

    p = sub.add_parser("shift", help="walk from one mood to another")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("minutes", type=int)
    p.add_argument("--seed", default="",
                   help='"Title|Artist" — build the pool around this song')
    p.add_argument("--genre", action="append", default=[], dest="genres",
                   help="genre hint, repeatable (e.g. --genre r&b). With no "
                        "seed and no genre, falls back to the global chart")
    p.add_argument("--library-only", action="store_true",
                   help="select only from music you own")
    p.add_argument("--budget", type=int, default=60,
                   help="max uncached candidates to tag for this request")
    p.add_argument("--push", metavar="NAME", default="",
                   help="create this playlist on YouTube Music")

    p = sub.add_parser("similar", help="co-listening neighbours (no model)")
    p.add_argument("title")
    p.add_argument("artist", nargs="?", default="")
    p.add_argument("-n", type=int, default=15)
    p.add_argument("--source", choices=["both", "ytm", "lastfm"],
                   default="both")
    p.add_argument("--per", type=int, default=50,
                   help="how many neighbours to pull from each source")
    p.add_argument("--deep", action="store_true",
                   help="also ask the top neighbours for their neighbours")
    p.add_argument("--no-same-artist", action="store_true")
    p.add_argument("--mine", action="store_true",
                   help="only tracks already in your library")

    p = sub.add_parser("make", help="plain English in, playlist out")
    p.add_argument("request", help='e.g. "I just got dumped and want to wallow"')
    p.add_argument("-n", type=int, default=12)
    p.add_argument("--budget", type=int, default=60,
                   help="max uncached candidates to tag for this request")
    p.add_argument("--push", metavar="NAME", default="",
                   help="create this playlist on YouTube Music")
    p.add_argument("--library-only", action="store_true",
                   help="select only from music you own")

    sub.add_parser("moods", help="the named moods and your coverage of them")

    a = ap.parse_args()
    con = common.connect()
    picked = None
    if a.cmd == "sustain":
        picked = sustain_cmd(con, a.title, a.artist, a.n, a.known_only,
                             a.library_only, a.budget)
    elif a.cmd == "shift":
        picked = shift_cmd(con, a.start, a.end, a.minutes, a.known_only,
                           a.seed, a.budget, a.genres, a.library_only)
    elif a.cmd == "make":
        picked = make_cmd(con, a.request, a.n, a.known_only, a.budget,
                          a.library_only)
    elif a.cmd == "similar":
        import neighbours
        neighbours.run(con, a.title, a.artist, a.n, a.source, a.per, a.deep,
                       a.no_same_artist, a.mine)
    else:
        moods_cmd(con, a.known_only)

    if getattr(a, "push", "") and picked:
        import push
        push.create(picked, a.push)


if __name__ == "__main__":
    main()
