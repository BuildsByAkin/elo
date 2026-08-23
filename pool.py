"""The candidate pool: the songs a request actually chooses from.

The library is not the pool. A mood card is a property of the song, not of the
listener — `Changes — Black Sabbath` scores the same for everyone — so cards
live in a shared corpus, built once and reused. This module turns a seed into a
scored pool without needing the user to own anything at all. See DESIGN.md §4.

    candidates ─ co-listening (no key, no account, no library)
        ↓ ensure_tracks   put anything unknown into the corpus as external=1
        ↓ ensure_cards    fetch lyrics + tag whatever has no card yet, cached
        ↓                 forever, so the next user pays nothing for this song
    a list of cards the engine can select over

`external=1` means "not owned", not "second class". It is only ever read as a
preference (see `OWNED_BONUS`), never as a filter.
"""
import json
import sys

import common
import lyrics as L
import neighbours
import tag as T

# What owning a track is worth when ranking. Small on purpose: a song you own
# should win a close call, not beat a better match. Set to 0.0 to ignore
# ownership entirely, which is what a brand-new user gets for free.
OWNED_BONUS = 0.35


def _seconds(length):
    """'3:19' -> 199. Co-listening sources give a string or nothing."""
    if not length:
        return 0
    parts = str(length).split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    return secs


def index(con):
    """Normalised (title, artist) -> track id, for everything in the corpus."""
    return {(common.norm(t), common.norm(a)): tid
            for tid, t, a in con.execute("SELECT id, title, artist FROM tracks")}


def ensure_tracks(con, candidates):
    """Add anything the corpus has not seen. Returns (ids, how_many_are_new).

    Matching is on normalised title+artist so a candidate the user already owns
    is recognised as owned rather than duplicated as external.
    """
    idx = index(con)
    ids, new = [], 0
    for c in candidates:
        key = (common.norm(c.get("title") or ""), common.norm(c.get("artist") or ""))
        if not key[0] or not key[1]:
            continue
        if key in idx:
            ids.append(idx[key])
            continue
        album = c.get("album") or ""
        con.execute(
            "INSERT OR IGNORE INTO tracks (title, artist, album, external,"
            " seconds) VALUES (?,?,?,1,?)",
            (c["title"], c["artist"], album, _seconds(c.get("length"))))
        row = con.execute(
            "SELECT id FROM tracks WHERE title=? AND artist=? AND album=?",
            (c["title"], c["artist"], album)).fetchone()
        if row:
            idx[key] = row[0]
            ids.append(row[0])
            new += 1
    con.commit()
    return ids, new


def uncached(con, ids):
    """Which of these have no mood card yet — i.e. what a request must pay for."""
    if not ids:
        return []
    q = ("SELECT id FROM tracks WHERE id IN (%s) AND id NOT IN"
         " (SELECT track_id FROM moods)" % ",".join("?" * len(ids)))
    return [r[0] for r in con.execute(q, list(ids))]


def ensure_cards(con, ids, budget=0, quiet=False):
    """Fetch lyrics and tag anything without a card.

    `budget` caps how many uncached tracks this request will pay to tag; 0 means
    no cap. The cap exists because on-demand tagging is cheap per song and
    brutal on first contact — a 300-candidate request into an unexplored corner
    of the catalogue is ~15 batched calls plus 300 lyric fetches. Phase 4b
    (pre-seeding the corpus) is what makes the cap stop mattering.
    """
    todo = uncached(con, ids)
    if not todo:
        return 0
    if budget and len(todo) > budget:
        if not quiet:
            print("  %d candidates uncached; tagging %d this request (cap)"
                  % (len(todo), budget), file=sys.stderr)
        todo = todo[:budget]

    rows = con.execute(
        "SELECT id, title, artist, album, genre, year FROM tracks WHERE id IN"
        " (%s)" % ",".join("?" * len(todo)), todo).fetchall()
    cols = ("id", "title", "artist", "album", "genre", "year")
    tracks = [dict(zip(cols, r)) for r in rows]

    if not quiet:
        print("  fetching lyrics for %d new song(s)" % len(tracks),
              file=sys.stderr)
    L.fetch(con, tracks)
    if not quiet:
        print("  tagging %d new song(s)" % len(tracks), file=sys.stderr)
    return T.tag_tracks(con, tracks)


def cards_for(con, ids):
    """The cards for these ids, with `owned` attached."""
    if not ids:
        return []
    q = ("SELECT t.id, t.title, t.artist, t.genre, t.seconds, m.themes,"
         " m.stance, m.valence, m.energy, m.summary, m.confidence, m.basis,"
         " t.external FROM moods m JOIN tracks t ON t.id = m.track_id"
         " WHERE t.id IN (%s)" % ",".join("?" * len(ids)))
    cols = ("id", "title", "artist", "genre", "seconds", "themes", "stance",
            "valence", "energy", "summary", "confidence", "basis", "external")
    out = []
    for r in con.execute(q, list(ids)):
        c = dict(zip(cols, r))
        c["themes"] = json.loads(c["themes"])
        c["owned"] = not c["external"]
        out.append(c)
    return out


def build(con, seed_title, seed_artist="", per=50, deep=False, budget=60,
          include_library=True, quiet=False):
    """Seed -> scored candidate pool. No library required.

    `include_library` folds the user's own tagged tracks in alongside the
    discovered candidates. That is a preference, not a dependency: with an empty
    database it contributes nothing and the pool is still a pool.
    """
    if not quiet:
        print("finding candidates near %s — %s" % (seed_title, seed_artist or "?"),
              file=sys.stderr)
    cands, used = neighbours.pool_for(seed_title, seed_artist, "both", per, deep)
    if not cands and not include_library:
        return []
    if cands and not quiet:
        print("  %d candidates from %s" % (len(cands), ", ".join(used)),
              file=sys.stderr)

    ids, new = ensure_tracks(con, cands)
    if new and not quiet:
        print("  %d new to the corpus" % new, file=sys.stderr)
    ensure_cards(con, ids, budget=budget, quiet=quiet)

    if include_library:
        ids += [r[0] for r in con.execute(
            "SELECT track_id FROM moods m JOIN tracks t ON t.id=m.track_id"
            " WHERE t.external=0")]
    return cards_for(con, sorted(set(ids)))
