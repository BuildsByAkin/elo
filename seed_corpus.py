#!/usr/bin/env python3
"""Pre-seed the shared corpus so the first user through a genre pays nothing.

On-demand tagging is correct but slow on first contact: an unexplored corner of
the catalogue costs a couple of minutes before it returns anything. This is the
build step that removes that, and it is a fixed one-time asset rather than a
per-user cost — a card is a property of the song, so every user reuses it.

We never predict which songs get asked for. We cover the popular head of each
tag, and let on-demand tagging handle the tail.

    python seed_corpus.py --per 60            # the default tag list
    python seed_corpus.py --tags workout,r&b  # just these
    python seed_corpus.py --dry-run           # what would it cost?

Resumable: anything already carrying a card is skipped, so re-running after an
interruption costs only what is missing.
"""
import argparse
import sys
import time

import common
import pool as P
import sources as S
import tag as T

# Genre tags do the heavy lifting; situation tags exist because that is how
# people actually ask ("gym songs", "breakup songs"). Both are only candidate
# sources — every mood judgement still comes from the card we write.
GENRES = [
    "hip-hop", "r&b", "pop", "rock", "indie", "electronic", "country",
    "metal", "jazz", "soul", "reggae", "afrobeats", "latin", "k-pop",
    "gospel", "folk", "punk", "classical", "blues", "house",
]
SITUATIONS = [
    "workout", "breakup", "party", "chill", "sad", "happy", "love",
    "motivational", "sleep", "study", "road trip", "summer",
]
DEFAULT_TAGS = GENRES + SITUATIONS


def gather(tags, per, quiet=False):
    """Candidates for every tag, deduped across tags by the fuser."""
    groups, empty = [], []
    for t in tags:
        got = S.tag_top(t, per)
        if got:
            groups.append(got)
        else:
            empty.append(t)
        if not quiet:
            print("  %-14s %3d" % (t, len(got)), file=sys.stderr)
        time.sleep(0.2)
    if empty:
        print("  (no tracks for: %s)" % ", ".join(empty), file=sys.stderr)
    return S.fuse(groups) if groups else []


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per", type=int, default=60,
                    help="tracks to pull per tag (default 60)")
    ap.add_argument("--tags", default="",
                    help="comma-separated tags instead of the default list")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the cost without spending it")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap how many uncached tracks to tag this run")
    a = ap.parse_args()

    tags = [t.strip() for t in a.tags.split(",") if t.strip()] or DEFAULT_TAGS
    con = common.connect()

    print("pulling %d tracks from each of %d tags" % (a.per, len(tags)),
          file=sys.stderr)
    cands = gather(tags, a.per)
    if not cands:
        sys.exit("no candidates — is LASTFM_API_KEY set?")
    print("\n%d distinct tracks after dedup" % len(cands), file=sys.stderr)

    ids, new = P.ensure_tracks(con, cands)
    todo = P.uncached(con, ids)
    print("%d already in the corpus, %d new, %d still need a card"
          % (len(ids) - new, new, len(todo)), file=sys.stderr)

    calls = -(-len(todo) // T.BATCH)
    print("\ncost: %d tracks -> %d lyric fetches + %d tagging calls"
          % (len(todo), len(todo), calls), file=sys.stderr)
    if a.dry_run:
        print("(dry run — nothing spent)", file=sys.stderr)
        return
    if not todo:
        print("corpus already covers these tags", file=sys.stderr)
        return

    t0 = time.time()
    P.ensure_cards(con, ids, budget=a.limit)
    dt = time.time() - t0

    total, lyric, known = con.execute(
        "SELECT count(*), sum(basis='lyrics'), sum(confidence='known')"
        " FROM moods").fetchone()
    print("\n=== CORPUS ===", file=sys.stderr)
    print("cards       %d" % total, file=sys.stderr)
    print("from lyrics %d (%.0f%%)" % (lyric, 100.0 * lyric / max(1, total)),
          file=sys.stderr)
    print("known       %d (%.0f%%)" % (known, 100.0 * known / max(1, total)),
          file=sys.stderr)
    print("took        %d min %02d sec" % (dt // 60, dt % 60), file=sys.stderr)


if __name__ == "__main__":
    main()
