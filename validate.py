#!/usr/bin/env python3
"""Phase 2 — is the mood card any good?

Three checks, cheapest and most informative first.

    python validate.py            # all three
    python validate.py --ear      # just the slice you react to
    python validate.py --gate     # just the named test set

`--ear` is the primary validation and it is deliberately not automated. It
prints the cards the model claims to be most sure of and the cards it admits it
guessed, and you spend five minutes reacting. You know these 843 songs; you are
better ground truth for this library than any published dataset, all of which
are built on the same Last.fm tag pool that DESIGN.md §2.1 measured at 8%
coverage here.

The top slice answers: does `known` mean anything?
The bottom slice answers: is `guessed` failing gracefully, or inventing things?
"""
import argparse
import json
import random
import sys

import common

# The brief's five, plus the cases that actually stress this library: a title
# that says nothing, a lyric-less instrumental, Afrobeats, drill, gospel.
GATE = [
    # (title, artist, what must be true, why it is here)
    ("Changes", "Black Sabbath", {"themes": {"breakup", "heartbreak"},
                                  "valence": (0, 4), "energy": (0, 4)},
     "the canonical case: title says nothing, lyrics say everything"),
    ("Someone Like You", "Adele", {"themes": {"breakup", "heartbreak",
                                              "unrequited-love"},
                                   "valence": (0, 4)},
     "a wallow — must not land near I Will Survive"),
    ("I Will Survive", "Gloria Gaynor", {"themes": {"breakup", "healing",
                                                    "self-worth", "defiance"},
                                         "valence": (5, 10)},
     "same situation as Adele, opposite posture — stance must separate them"),
    ("good 4 u", "Olivia Rodrigo", {"themes": {"breakup", "anger", "betrayal",
                                               "jealousy"},
                                    "energy": (6, 10)},
     "a revenge track — high energy breakup"),
    ("Tell Your Friends", "The Weeknd", {},
     "the seed used throughout the README"),
]


def cards(con):
    q = ("SELECT t.id, t.title, t.artist, t.genre, t.year, m.themes, m.stance,"
         " m.valence, m.energy, m.summary, m.confidence, m.basis, t.external"
         " FROM moods m JOIN tracks t ON t.id = m.track_id")
    cols = ("id", "title", "artist", "genre", "year", "themes", "stance",
            "valence", "energy", "summary", "confidence", "basis", "external")
    out = []
    for r in con.execute(q):
        c = dict(zip(cols, r))
        c["themes"] = json.loads(c["themes"])
        out.append(c)
    return out


def show(c, prefix="  "):
    print("%s%s — %s" % (prefix, c["title"], c["artist"] or "(unknown artist)"))
    print("%s   %s | %s | v %.1f  e %.1f   [%s, %s]"
          % (" " * len(prefix), "/".join(c["themes"]), c["stance"],
             c["valence"], c["energy"], c["confidence"], c["basis"]))
    if c["summary"]:
        print("%s   %s" % (" " * len(prefix), c["summary"]))


# ------------------------------------------------------------------- numbers

def numbers(pool):
    """The four figures the README has been admitting it does not have."""
    n = len(pool)
    lyr = sum(1 for c in pool if c["basis"] == "lyrics")
    known = sum(1 for c in pool if c["confidence"] == "known")
    known_no_lyrics = sum(1 for c in pool
                          if c["confidence"] == "known" and c["basis"] != "lyrics")
    print("\n=== THE NUMBERS ===")
    print("cards                 %d" % n)
    print("from lyrics           %d (%.0f%%)" % (lyr, 100.0 * lyr / n))
    print("model says `known`    %d (%.0f%%)" % (known, 100.0 * known / n))
    print("`known` WITHOUT lyrics %d (%.0f%%)  <- claims to know the recording"
          % (known_no_lyrics, 100.0 * known_no_lyrics / n))

    counts = {}
    for c in pool:
        for th in c["themes"]:
            counts[th] = counts.get(th, 0) + 1
    print("\ntop themes")
    for th, k in sorted(counts.items(), key=lambda x: -x[1])[:12]:
        print("  %4d  %s" % (k, th))
    stances = {}
    for c in pool:
        stances[c["stance"]] = stances.get(c["stance"], 0) + 1
    print("\nstances (the field that separates a wallow from an anthem)")
    for st, k in sorted(stances.items(), key=lambda x: -x[1]):
        print("  %4d  %s" % (k, st))


# ----------------------------------------------------------------------- ear

def ear(pool, n=10, seed=7):
    """The slice you react to. Not automated on purpose."""
    rng = random.Random(seed)
    best = [c for c in pool if c["confidence"] == "known"
            and c["basis"] == "lyrics"]
    worst = [c for c in pool if c["confidence"] == "guessed"]

    print("\n" + "=" * 68)
    print("EAR CHECK — five minutes of your attention, please")
    print("=" * 68)

    print("\n--- %d cards the model is MOST sure of (known, read from lyrics)"
          % min(n, len(best)))
    print("    asking: does `known` mean anything?\n")
    for c in rng.sample(best, min(n, len(best))):
        show(c)
        print()

    print("--- %d cards the model ADMITS it guessed (no lyrics found)"
          % min(n, len(worst)))
    print("    asking: is `guessed` failing gracefully, or inventing things?\n")
    for c in rng.sample(worst, min(n, len(worst))):
        show(c)
        print()

    print("If a card in the top block is wrong, `known` is not trustworthy and")
    print("--known-only buys nothing. If a card in the bottom block is confidently")
    print("wrong rather than vaguely right, title inference is worse than useless")
    print("and those %d tracks should be excluded, not merely marked." % len(worst))


# ---------------------------------------------------------------------- gate

def _find(pool, title, artist):
    t, a = title.lower(), artist.lower()
    for c in pool:
        if t in c["title"].lower() and a in c["artist"].lower():
            return c
    return None


def bootstrap(con):
    """Pull any gate track you do not own in as external=1, then tag it.

    Three of the five named songs are not in this library, and a gate that
    silently skips them is not a gate. `external=1` keeps them out of the
    selection pool, so they can be checked without polluting playlists.
    """
    import elo
    import lyrics as L

    added = []
    for title, artist, _, _ in GATE:
        row = con.execute("SELECT id FROM tracks WHERE lower(title)=?"
                          " AND lower(artist) LIKE ?",
                          (title.lower(), "%%%s%%" % artist.lower())).fetchone()
        if not row:
            con.execute("INSERT OR IGNORE INTO tracks (title, artist, album,"
                        " external) VALUES (?,?,'',1)", (title, artist))
            con.commit()
            row = con.execute("SELECT id FROM tracks WHERE title=? AND artist=?"
                              " AND album=''", (title, artist)).fetchone()
            print("  added %s — %s as external" % (title, artist))
        tid = row[0]
        if not con.execute("SELECT 1 FROM moods WHERE track_id=?",
                           (tid,)).fetchone():
            L.fetch(con, [{"id": tid, "title": title, "artist": artist}])
            elo._tag_one(con, tid)
            added.append("%s — %s" % (title, artist))
    if added:
        print("  tagged %d gate track(s)" % len(added))
    return added


def gate(pool):
    print("\n=== TEST SET GATE ===")
    failed = 0
    for title_frag, artist_frag, want, why in GATE:
        c = _find(pool, title_frag, artist_frag)
        if not c:
            print("\n  SKIP  %s — %s (not in the library)" % (title_frag, artist_frag))
            print("        %s" % why)
            continue
        problems = []
        if "themes" in want and not (set(c["themes"]) & want["themes"]):
            problems.append("themes %s share nothing with %s"
                            % (c["themes"], sorted(want["themes"])))
        for axis in ("valence", "energy"):
            if axis in want:
                lo, hi = want[axis]
                if not (lo <= c[axis] <= hi):
                    problems.append("%s %.1f outside %g-%g" % (axis, c[axis], lo, hi))
        print("\n  %s  %s — %s" % ("FAIL" if problems else "pass",
                                   c["title"], c["artist"]))
        print("        %s" % why)
        show(c, "        ")
        for p in problems:
            print("        ! %s" % p)
        failed += bool(problems)

    adele = _find(pool, "someone like you", "adele")
    gloria = _find(pool, "i will survive", "gloria gaynor")
    if adele and gloria:
        print("\n  --- the stance test ---")
        print("  Same situation, opposite records. If stance or valence does not")
        print("  separate these two, `sustain` cannot hold a posture.")
        print("        Adele  : %s / %s / v %.1f" % ("+".join(adele["themes"]),
                                                     adele["stance"], adele["valence"]))
        print("        Gaynor : %s / %s / v %.1f" % ("+".join(gloria["themes"]),
                                                     gloria["stance"], gloria["valence"]))
        if adele["stance"] == gloria["stance"]:
            print("        ! FAIL — identical stance")
            failed += 1
        else:
            print("        pass — stance separates them")
    return failed


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ear", action="store_true")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("-n", type=int, default=10, help="cards per ear-check block")
    ap.add_argument("--no-bootstrap", action="store_true",
                    help="do not pull in gate tracks you do not own")
    a = ap.parse_args()

    con = common.connect()
    everything = not (a.ear or a.gate)

    if (everything or a.gate) and not a.no_bootstrap:
        bootstrap(con)

    pool = cards(con)
    if not pool:
        sys.exit("no tagged tracks — run: python lyrics.py && python tag.py")
    # Library statistics describe the library; the gate tracks we pulled in to
    # be checked are not part of it.
    mine = [c for c in pool if not c["external"]]

    if everything:
        numbers(mine)
    if everything or a.gate:
        failed = gate(pool)
        print("\n%s" % ("gate: %d check(s) failed" % failed if failed
                        else "gate: all checks passed"))
    if everything or a.ear:
        ear(mine, a.n)


if __name__ == "__main__":
    main()
