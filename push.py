"""Push a generated playlist to YouTube Music.

Search and metadata work unauthenticated — that is why `similar` has needed no
setup all along. Creating a playlist writes to your account, so it needs
credentials, and that is the one step only you can do. See README.

Two auth modes, and browser is the one to use:

  browser.json  copy your request headers once, valid ~2 years, no Google Cloud
                project involved.
  oauth.json    needs a Google Cloud OAuth client ("TVs and Limited Input
                devices"); client_id and client_secret became mandatory in
                November 2024. More setup for no benefit here.

Matching is deliberately conservative. A playlist quietly containing the wrong
song is worse than one that is short and says which tracks it skipped.
"""
import os
import sys

import common

AUTH_FILES = ("browser.json", "oauth.json")


def _auth_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in AUTH_FILES:
        for base in (os.getcwd(), here):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
    return None


def client(need_auth=True):
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        sys.exit("ytmusicapi is not installed — pip install ytmusicapi")
    if not need_auth:
        return YTMusic()
    path = _auth_path()
    if not path:
        sys.exit(
            "No YouTube Music credentials found.\n\n"
            "Set them up once (about two minutes):\n"
            "  1. open https://music.youtube.com in your browser, logged in\n"
            "  2. open developer tools, Network tab\n"
            "  3. filter for  /browse  and click an authenticated POST (status 200)\n"
            "  4. copy the request headers\n"
            "       Firefox: right-click > Copy > Copy Request Headers\n"
            "       Chrome:  right-click > Copy > Copy as fetch (Node.js)\n"
            "  5. run:  ytmusicapi browser\n"
            "     paste the headers, then press Ctrl-D\n\n"
            "That writes browser.json here and lasts about two years.")
    return YTMusic(path)


def _hit(want_t, want_a, got_t, got_a):
    """Reuse the guard sources.py uses — a search will confidently return the
    wrong song otherwise."""
    a, b = common.norm(want_t), common.norm(got_t)
    if not a or not b or (a not in b and b not in a):
        return False
    x, y = common.norm(want_a), common.norm(got_a)
    return not x or not y or x in y or y in x


def resolve(yt, tracks, quiet=False):
    """Find a videoId for each track. Returns (ids, misses)."""
    ids, misses = [], []
    for t in tracks:
        vid = t.get("videoId") or ""
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
            ids.append(vid)
            if not quiet:
                print("  ok    %s — %s" % (t["title"][:40], t["artist"][:24]),
                      file=sys.stderr)
        else:
            misses.append((t, "no confident match in the YTM catalogue"))
            if not quiet:
                print("  SKIP  %s — %s" % (t["title"][:40], t["artist"][:24]),
                      file=sys.stderr)
    return ids, misses


def create(tracks, title, description="", quiet=False):
    """Create the playlist. Returns its id, or None if nothing resolved."""
    yt = client()
    print("resolving %d tracks against the YouTube Music catalogue..."
          % len(tracks), file=sys.stderr)
    ids, misses = resolve(yt, tracks, quiet)
    if not ids:
        print("none of those tracks resolved — nothing to create",
              file=sys.stderr)
        return None
    pid = yt.create_playlist(title, description or "Built by elo", "PRIVATE",
                             video_ids=ids)
    # create_playlist returns the id as a string, or a dict on some errors.
    if isinstance(pid, dict):
        sys.exit("YouTube Music refused the playlist: %s" % str(pid)[:300])
    print("\ncreated %r with %d of %d tracks" % (title, len(ids), len(tracks)),
          file=sys.stderr)
    print("https://music.youtube.com/playlist?list=%s" % pid, file=sys.stderr)
    if misses:
        print("\n%d skipped:" % len(misses), file=sys.stderr)
        for t, why in misses:
            print("  %s — %s   (%s)" % (t["title"], t["artist"], why),
                  file=sys.stderr)
    return pid
