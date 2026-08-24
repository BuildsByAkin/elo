"""On-disk cache for everything we fetch from YouTube Music and Last.fm.

Every request re-asked the same questions: the same seed's radio, the same
curated playlists, the same mood categories, several seconds of network per
run and most of it identical to the last run. None of it is personal or
volatile — it is an aggregate of what strangers played — so it caches well.

TTLs ARE PER SOURCE, BECAUSE THE SOURCES MOVE AT DIFFERENT SPEEDS

    The mood and genre categories are a navigation structure that changes when
    Google redesigns a page. The curated playlists behind them are edited
    weekly-ish. A radio queue is regenerated constantly and is the most
    personal-feeling thing here, so it gets the shortest life. Last.fm's
    similarity is computed over years of scrobbles and barely moves at all.
    One global TTL would have to be short enough for the fastest of those,
    which throws away almost all of the benefit.

FAILURES AND EMPTIES ARE NOT CACHED FOR LONG

    A source that returns nothing is usually a source having a bad minute:
    Last.fm goes blind on plenty of catalogue and returns an empty list rather
    than an error, and YouTube Music can fail a parse after a payload change.
    Caching that for two weeks would turn a transient outage into a fortnight
    of silently missing candidates, so an empty result is kept for an hour —
    long enough not to hammer a struggling service, short enough to heal.

Cached rows never change what elo decides: a hit and a miss return the same
value. `--fresh` skips the cache for one run, and `elo.py cache clear` drops it.
"""
import hashlib
import json
import os
import sys
import time

import common

HOUR = 3600.0
DAY = 24 * HOUR

# How long each kind of answer stays useful.
TTL = {
    "categories": 30 * DAY,   # the mood/genre nav; changes on a redesign
    "shelves": 7 * DAY,       # which playlists sit in a category
    "playlist": 3 * DAY,      # what is in one of those playlists
    "radio": 1 * DAY,         # regenerated constantly, so trust it least
    "search": 7 * DAY,        # title/artist -> videoId barely moves
    "similar": 14 * DAY,      # years of scrobbles behind each number
    "tag": 14 * DAY,
    "chart": 1 * DAY,         # it is a chart
}
EMPTY_TTL = 1 * HOUR          # a source having a bad minute, not an answer

# Set by the CLI's --fresh, or ELO_NO_CACHE in the environment.
enabled = os.environ.get("ELO_NO_CACHE", "") not in ("1", "true", "yes")


def _key(kind, *parts):
    raw = "|".join(str(p) for p in parts)
    if len(raw) > 120:                       # keep the index tidy
        raw = hashlib.sha1(raw.encode()).hexdigest()
    return "%s:%s" % (kind, raw)


def get(kind, *parts):
    """The cached value, or None for a miss. `None` never means "empty" —
    an empty list is a real answer and comes back as an empty list."""
    if not enabled:
        return None
    k = _key(kind, *parts)
    try:
        con = common.connect()
        row = con.execute(
            "SELECT v, fetched, ttl FROM cache WHERE k=?", (k,)).fetchone()
    except Exception:
        return None                          # a broken cache must never break
    if not row:                              # a request
        return None
    v, fetched, ttl = row
    if time.time() - fetched > ttl:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


def put(kind, value, *parts):
    if not enabled:
        return value
    ttl = TTL.get(kind, DAY) if value else EMPTY_TTL
    try:
        con = common.connect()
        con.execute(
            "INSERT INTO cache (k,kind,v,fetched,ttl) VALUES (?,?,?,?,?)"
            " ON CONFLICT(k) DO UPDATE SET v=excluded.v,"
            " fetched=excluded.fetched, ttl=excluded.ttl",
            (_key(kind, *parts), kind, json.dumps(value), time.time(), ttl))
        con.commit()
    except Exception as e:
        print("  cache write failed: %s" % str(e)[:80], file=sys.stderr)
    return value


def wrap(kind, parts, fn):
    """`fn()` unless we already asked recently. The whole interface."""
    hit = get(kind, *parts)
    if hit is not None:
        return hit
    return put(kind, fn(), *parts)


def prune(max_rows=4000):
    """Drop what has expired, then the oldest if it is still unreasonable."""
    con = common.connect()
    con.execute("DELETE FROM cache WHERE ? - fetched > ttl", (time.time(),))
    n = con.execute("SELECT count(*) FROM cache").fetchone()[0]
    if n > max_rows:
        con.execute(
            "DELETE FROM cache WHERE k IN (SELECT k FROM cache"
            " ORDER BY fetched LIMIT ?)", (n - max_rows,))
    con.commit()


def clear(kind=None):
    con = common.connect()
    if kind:
        n = con.execute("SELECT count(*) FROM cache WHERE kind=?",
                        (kind,)).fetchone()[0]
        con.execute("DELETE FROM cache WHERE kind=?", (kind,))
    else:
        n = con.execute("SELECT count(*) FROM cache").fetchone()[0]
        con.execute("DELETE FROM cache")
    con.commit()
    return n


def stats():
    con = common.connect()
    now = time.time()
    rows = []
    for kind, n, oldest, bytes_ in con.execute(
            "SELECT kind, count(*), min(fetched), sum(length(v))"
            " FROM cache GROUP BY kind ORDER BY count(*) DESC"):
        live = con.execute(
            "SELECT count(*) FROM cache WHERE kind=? AND ?-fetched<=ttl",
            (kind, now)).fetchone()[0]
        rows.append({"kind": kind, "n": n, "live": live,
                     "kb": (bytes_ or 0) / 1024.0,
                     "age_h": (now - oldest) / HOUR if oldest else 0.0,
                     "ttl_h": TTL.get(kind, DAY) / HOUR})
    return rows


def show(out=sys.stdout):
    rows = stats()
    if not rows:
        print("cache is empty", file=out)
        return
    print("%-12s %6s %6s %9s %9s %9s" % ("kind", "rows", "live", "size",
                                         "oldest", "ttl"), file=out)
    for r in rows:
        print("%-12s %6d %6d %8.0fK %8.1fh %8.0fh"
              % (r["kind"], r["n"], r["live"], r["kb"], r["age_h"],
                 r["ttl_h"]), file=out)
    print("\n%d rows, %.0fK total" % (sum(r["n"] for r in rows),
                                      sum(r["kb"] for r in rows)), file=out)


def main():
    args = sys.argv[1:]
    if args and args[0] == "clear":
        print("dropped %d rows" % clear(args[1] if len(args) > 1 else None),
              file=sys.stderr)
    elif args and args[0] == "prune":
        prune()
        show()
    else:
        show()


if __name__ == "__main__":
    main()
