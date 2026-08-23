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
SPAN = math.hypot(10, 10)      # longest possible distance in mood space
AVG_SECONDS = 210              # fallback when an export carries no duration

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

def score(seed, c):
    """Shared subject dominates; nearness in mood space breaks the ties.

    The weights are reasoned, not tuned — see DESIGN.md §6. They stay guesses
    until there is a test set to tune them against."""
    a, b = set(seed["themes"]), set(c["themes"])
    overlap = len(a & b) / len(a | b) if (a | b) else 0.0
    dist = math.hypot(seed["valence"] - c["valence"],
                      seed["energy"] - c["energy"]) / SPAN
    return 6.0 * overlap + (1.5 if seed["stance"] == c["stance"] else 0.0) \
        - 4.0 * dist


def sustain_cmd(con, title, artist, n, known_only):
    seed = find_seed(con, title, artist)
    print("\nSEED")
    show(seed, "  ")
    pool = [c for c in require_cards(con, known_only) if c["id"] != seed["id"]]
    ranked = sorted(pool, key=lambda c: -score(seed, c))[:n]
    print("\nSTAYS IN THAT MOOD  (%d of %d tagged tracks)" % (len(ranked),
                                                              len(pool)))
    for i, c in enumerate(ranked, 1):
        show(c, "%d. " % i)
        print("     match %.2f" % score(seed, c))


# -------------------------------------------------------------------- shift

def shift_cmd(con, start, end, minutes, known_only):
    """Phase 1 behaviour: straight-line interpolation, nearest untaken track.

    This is deliberately still the naive version. Phase 3 replaces the picker
    with a real constraint set — no artist twice in a window, bounded per-step
    energy delta, genre coherence early — which is where shift mode becomes the
    product rather than a demo. See DESIGN.md §5.
    """
    for m in (start, end):
        if m not in MOODS:
            sys.exit("unknown mood %r — try: %s" % (m, ", ".join(sorted(MOODS))))
    a, b = MOODS[start], MOODS[end]
    pool = require_cards(con, known_only)

    budget = minutes * 60
    steps = max(2, round(budget / AVG_SECONDS))
    print("\n%s -> %s over %d min (~%d tracks)" % (start, end, minutes, steps))
    print("  from valence %.1f energy %.1f  to  valence %.1f energy %.1f"
          % (a + b))

    used, total, taken = [], 0, set()
    for i in range(steps):
        f = i / (steps - 1)
        tv = a[0] + (b[0] - a[0]) * f
        te = a[1] + (b[1] - a[1]) * f
        left = [c for c in pool if c["id"] not in taken]
        if not left:
            break
        pick = min(left, key=lambda c: math.hypot(c["valence"] - tv,
                                                  c["energy"] - te))
        used.append(pick)
        taken.add(pick["id"])
        total += pick["seconds"] or AVG_SECONDS
        if total >= budget:
            break

    print("\nSHIFT  (%d tracks, %d min %02d sec)"
          % (len(used), total // 60, total % 60))
    for i, c in enumerate(used, 1):
        show(c, "%d. " % i)


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

    p = sub.add_parser("shift", help="walk from one mood to another")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("minutes", type=int)

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

    sub.add_parser("moods", help="the named moods and your coverage of them")

    a = ap.parse_args()
    con = common.connect()
    if a.cmd == "sustain":
        sustain_cmd(con, a.title, a.artist, a.n, a.known_only)
    elif a.cmd == "shift":
        shift_cmd(con, a.start, a.end, a.minutes, a.known_only)
    elif a.cmd == "similar":
        import neighbours
        neighbours.run(con, a.title, a.artist, a.n, a.source, a.per, a.deep,
                       a.no_same_artist, a.mine)
    else:
        moods_cmd(con, a.known_only)


if __name__ == "__main__":
    main()
