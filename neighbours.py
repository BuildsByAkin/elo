"""Candidate generation: what songs sit near this one in listening behaviour?

This is the half of the product that mood cards cannot do. It answers "what
exists near this song" — including music you do not own — and it answers it
without a language model anywhere: YouTube Music's own radio queue, with
Last.fm's scrobble-derived similar tracks behind it. See sources.py for what
each one is and is not.

It deliberately does NOT answer "what is this song about". The probe settled
that: 4,594 co-listening neighbours surfaced 7 library tracks and they were the
seeds it started from. Co-listening carries adjacency, not theme. Theme comes
from the mood card; this supplies the pool the card then filters.

Tracks you own are marked, never promoted.
"""
import sys

import common
import sources as S


def gather(title, artist, source, per):
    groups, used = [], []
    if source in ("ytm", "both"):
        g = S.ytm(title, artist, per)
        if g:
            groups.append(g)
            used.append("youtube music (%d)" % len(g))
    if source == "lastfm" or source == "both":
        g = S.lastfm(title, artist, per)
        if g:
            groups.append(g)
            used.append("last.fm (%d)" % len(g))
    return groups, used


def deepen(pool, source, per, top=5):
    """Second hop: ask the strongest neighbours for their neighbours."""
    out = []
    for s in pool[:top]:
        g, _ = gather(s["title"], s["artist"], source, per)
        for c in [x for grp in g for x in grp]:
            c["rank"] += 25          # a second-hop track starts further back
            c["hops"] = 2
            out.append(c)
    return out


def owned(con):
    if con is None:
        return {}
    return {(common.norm(t), common.norm(a)): True
            for t, a in con.execute("SELECT title, artist FROM tracks")}


def pool_for(title, artist, source="both", per=50, deep=False,
             no_same_artist=False):
    """The ranked candidate pool. Shared by the `similar` command and, from
    phase 4, by shift mode when the library cannot fill a step on the path."""
    groups, used = gather(title, artist, source, per)
    if not groups:
        return [], []
    pool = S.fuse(groups)
    if deep:
        print("expanding one more hop", file=sys.stderr)
        pool = S.fuse(groups + [deepen(pool, source, per)])
    if no_same_artist and artist:
        me = common.norm(artist)
        pool = [c for c in pool if common.norm(c["artist"]) != me]
    return pool, used


def run(con, title, artist, n, source, per, deep, no_same_artist, mine):
    print("seed: %s — %s" % (title, artist or "(no artist given)"),
          file=sys.stderr)
    pool, used = pool_for(title, artist, source, per, deep, no_same_artist)
    if not pool:
        sys.exit("Neither source knows that song. Check the spelling, or try "
                 "the artist name exactly as it appears on the release.")
    print("sources: %s" % ", ".join(used), file=sys.stderr)

    have = owned(con)
    if mine:
        pool = [c for c in pool
                if (common.norm(c["title"]), common.norm(c["artist"])) in have]
        if not pool:
            sys.exit("None of those are in your library.")

    print("\n%d songs to play after %s:\n" % (min(n, len(pool)), title))
    for i, c in enumerate(pool[:n], 1):
        k = (common.norm(c["title"]), common.norm(c["artist"]))
        tags = "".join([
            "  ● in your library" if k in have else "",
            "  ·2 hops" if c.get("hops") == 2 else "",
            "  ★ both sources" if len(c["sources"]) > 1 else "",
        ])
        print("%2d. %s — %s%s" % (i, c["title"], c["artist"], tags))
        detail = "  ".join(x for x in (c.get("album"), c.get("length")) if x)
        if detail:
            print("    %s" % detail)
    n_mine = sum(1 for c in pool[:n]
                 if (common.norm(c["title"]), common.norm(c["artist"])) in have)
    print("\n%d of %d in your library.   ★both = both sources agreed"
          % (n_mine, min(n, len(pool))))
