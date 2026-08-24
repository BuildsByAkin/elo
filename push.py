"""Push a generated playlist to YouTube Music, without piling up duplicates.

Search and metadata work unauthenticated — that is why candidate gathering has
needed no setup all along. Writing to your account does not, and that is the
one step only you can do; see `elo.py auth ytmusic`.

Matching is deliberately conservative. A playlist quietly containing the wrong
song is worse than one that is short and says which tracks it skipped.

WHY THIS FILE KEEPS A RECORD

    A playlist tool that only ever calls create_playlist turns your account
    into a landfill: ask for "hip hop bangers" on four evenings and you own
    four playlists with the same name and overlapping contents, and the good
    one is whichever you can still find. So every push is recorded here — id,
    title, and a fingerprint of the track set — and a second push of the same
    title updates that playlist in place instead of making another.

    Updating is scoped to playlists elo created and recorded. A playlist you
    made by hand is never touched, even if the titles collide, because we have
    no record of having made it. And the record is verified against the live
    account before it is trusted: you may have deleted the playlist since, and
    a stale local row must not silently turn a create into a no-op.

DEDUPE HAPPENS TWICE

    Once on the track list, because two candidates can resolve to the same
    videoId — "Often" and "Often (Kygo Remix)" are different rows from
    different sources and the same video — and a playlist containing the same
    song twice is a bug regardless of how it got there.

    Once on the playlist, by diffing against what is already there. An update
    adds only what is missing and removes only what is no longer wanted, so
    the playlist keeps its URL, its position in your library, and anything you
    reordered by hand.
"""
import hashlib
import sys
import time

import common


def client(need_auth=True):
    """Credentials and their health live in ytauth — see the note there about
    why an expired session looks exactly like an empty account."""
    import ytauth
    return ytauth.client(need_auth)


def _hit(want_t, want_a, got_t, got_a):
    """Reuse the guard sources.py uses — a search will confidently return the
    wrong song otherwise."""
    a, b = common.norm(want_t), common.norm(got_t)
    if not a or not b or (a not in b and b not in a):
        return False
    x, y = common.norm(want_a), common.norm(got_a)
    return not x or not y or x in y or y in x


# ------------------------------------------------------------------ dedupe

def fingerprint(video_ids):
    """A stable id for a set of tracks, independent of order.

    Order-independent on purpose: a rebuild that returns the same songs in a
    different sequence is the same playlist, and re-pushing it should be
    recognised as such rather than treated as new work.
    """
    if not video_ids:
        return ""
    joined = ",".join(sorted(set(video_ids)))
    return hashlib.sha1(joined.encode()).hexdigest()[:16]


def dedupe(pairs):
    """Drop repeats from `[(track, videoId)]`. Returns (kept, dropped).

    Two sources can hand back the same recording under different titles, and
    fusion merges on normalised title+artist rather than on videoId — it has to,
    because most candidates never carry one. The duplicates that survive that
    are only visible here, once everything has been resolved.
    """
    kept, dropped, seen = [], [], set()
    for track, vid in pairs:
        if vid in seen:
            dropped.append((track, vid))
            continue
        seen.add(vid)
        kept.append((track, vid))
    return kept, dropped


# ------------------------------------------------------------------ records

def record(playlist_id, title, request, video_ids):
    con = common.connect()
    con.execute(
        "INSERT INTO pushed (playlist_id,title,request,fingerprint,n,at)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(playlist_id) DO UPDATE SET"
        " title=excluded.title, request=excluded.request,"
        " fingerprint=excluded.fingerprint, n=excluded.n, at=excluded.at",
        (playlist_id, title, request, fingerprint(video_ids), len(video_ids),
         time.strftime("%Y-%m-%d %H:%M")))
    con.commit()


def forget_push(playlist_id):
    con = common.connect()
    con.execute("DELETE FROM pushed WHERE playlist_id=?", (playlist_id,))
    con.commit()


def pushed(title=None):
    con = common.connect()
    rows = [dict(zip(("playlist_id", "title", "request", "fingerprint", "n",
                      "at"), r))
            for r in con.execute(
                "SELECT playlist_id,title,request,fingerprint,n,at FROM pushed"
                " ORDER BY at DESC")]
    if title is None:
        return rows
    want = common.norm(title)
    return [r for r in rows if common.norm(r["title"]) == want]


def decide(title, video_ids, history, new=False):
    """What to do, given what we have pushed before. Pure, so it is testable.

    Returns (action, row) where action is 'create', 'update' or 'unchanged'.
    """
    if new:
        return "create", None
    same_title = [r for r in history
                  if common.norm(r["title"]) == common.norm(title)]
    if not same_title:
        return "create", None
    row = same_title[0]
    if row["fingerprint"] and row["fingerprint"] == fingerprint(video_ids):
        return "unchanged", row
    return "update", row


# -------------------------------------------------------------------- write

def resolve(yt, tracks, quiet=False):
    """Find a videoId for each track. Returns (pairs, misses).

    Most tracks already carry one: YouTube Music radio queues and mood
    playlists hand back the videoId with the track, and so does your cached
    library. Only Last.fm-only candidates need a search here, which is both
    faster and safer — a search is the step where the wrong song gets in.
    """
    pairs, misses = [], []
    for t in tracks:
        vid = t.get("video_id") or t.get("videoId") or ""
        if not vid:
            try:
                res = yt.search("%s %s" % (t["title"], t["artist"]),
                                filter="songs", limit=5)
            except Exception as e:
                misses.append((t, "search failed: %s" % str(e)[:60]))
                continue
            for r in res:
                names = ", ".join(x["name"] for x in (r.get("artists") or []))
                if _hit(t["title"], t["artist"], r.get("title") or "", names):
                    vid = r.get("videoId") or ""
                    break
        if vid:
            pairs.append((t, vid))
            if not quiet:
                print("  ok    %s — %s" % (t["title"][:40], t["artist"][:24]),
                      file=sys.stderr)
        else:
            misses.append((t, "no confident match in the YTM catalogue"))
            if not quiet:
                print("  SKIP  %s — %s" % (t["title"][:40], t["artist"][:24]),
                      file=sys.stderr)
    return pairs, misses


def _current(yt, playlist_id):
    """What is in the playlist now: [(videoId, setVideoId)], or None if gone.

    None means the local record is stale — you deleted it, or it was pushed
    from another machine — and the caller must fall back to creating.
    """
    try:
        pl = yt.get_playlist(playlist_id, limit=500)
    except Exception:
        return None
    out = []
    for t in (pl.get("tracks") or []):
        vid = t.get("videoId")
        if vid:
            out.append((vid, t.get("setVideoId")))
    return out


def _update(yt, row, ids, quiet=False):
    """Diff the playlist against what we want. Returns (added, removed)."""
    have = _current(yt, row["playlist_id"])
    if have is None:
        return None, None                      # caller falls back to create
    have_ids = [v for v, _ in have]
    want = set(ids)
    add = [v for v in ids if v not in set(have_ids)]
    drop = [{"videoId": v, "setVideoId": s} for v, s in have
            if v not in want and s]
    if add:
        yt.add_playlist_items(row["playlist_id"], videoIds=add,
                              duplicates=False)
    if drop:
        yt.remove_playlist_items(row["playlist_id"], drop)
    if not quiet:
        print("  +%d  -%d  (kept %d)" % (len(add), len(drop),
                                         len(have_ids) - len(drop)),
              file=sys.stderr)
    return len(add), len(drop)


def create(tracks, title, description="", request="", new=False, quiet=False):
    """Push. Creates, updates in place, or does nothing. Returns the id."""
    import ytauth
    ytauth.require("pushing a playlist")
    yt = client()

    print("resolving %d tracks against the YouTube Music catalogue..."
          % len(tracks), file=sys.stderr)
    pairs, misses = resolve(yt, tracks, quiet)
    pairs, dupes = dedupe(pairs)
    if dupes and not quiet:
        for t, _ in dupes:
            print("  dup   %s — %s   (already in this playlist under another "
                  "title)" % (t["title"][:36], t["artist"][:22]),
                  file=sys.stderr)
    ids = [v for _, v in pairs]
    if not ids:
        print("none of those tracks resolved — nothing to push",
              file=sys.stderr)
        return None

    action, row = decide(title, ids, pushed(), new=new)

    if action == "unchanged":
        # Verify before trusting the record; a deleted playlist must not turn
        # a push into a silent no-op.
        if _current(yt, row["playlist_id"]) is not None:
            print("\nalready pushed exactly these %d tracks as %r — nothing to "
                  "do" % (len(ids), row["title"]), file=sys.stderr)
            print(url(row["playlist_id"]), file=sys.stderr)
            print("  pass --new to push a second copy anyway", file=sys.stderr)
            return row["playlist_id"]
        forget_push(row["playlist_id"])
        action, row = "create", None

    if action == "update":
        added, removed = _update(yt, row, ids, quiet)
        if added is None:
            forget_push(row["playlist_id"])   # it is gone; make a new one
            action, row = "create", None
        else:
            record(row["playlist_id"], title, request, ids)
            print("\nupdated %r: %d tracks (+%d, -%d)"
                  % (title, len(ids), added, removed), file=sys.stderr)
            print(url(row["playlist_id"]), file=sys.stderr)
            _report_misses(misses)
            return row["playlist_id"]

    pid = yt.create_playlist(title, description or "Built by elo", "PRIVATE",
                             video_ids=ids)
    # create_playlist returns the id as a string, or a dict on some errors.
    if isinstance(pid, dict):
        sys.exit("YouTube Music refused the playlist: %s" % str(pid)[:300])
    record(pid, title, request, ids)
    print("\ncreated %r with %d of %d tracks" % (title, len(ids), len(tracks)),
          file=sys.stderr)
    print(url(pid), file=sys.stderr)
    _report_misses(misses)
    return pid


def url(playlist_id):
    return "https://music.youtube.com/playlist?list=%s" % playlist_id


def _report_misses(misses):
    if not misses:
        return
    print("\n%d skipped:" % len(misses), file=sys.stderr)
    for t, why in misses:
        print("  %s — %s   (%s)" % (t["title"], t["artist"], why),
              file=sys.stderr)


def show(out=sys.stdout):
    rows = pushed()
    if not rows:
        print("nothing pushed yet", file=out)
        return
    for r in rows:
        print("  %s  %-34s %3d tracks  %s"
              % (r["at"], r["title"][:34], r["n"], url(r["playlist_id"])),
              file=out)
