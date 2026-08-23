"""Two independent answers to "what would I play after this?".

YOUTUBE MUSIC (primary). `get_watch_playlist` is the queue YouTube Music itself
    builds when you hit play — Google's own "up next", derived from what people
    actually played. There is no official API for this. ytmusicapi reaches it by
    POSTing to music.youtube.com/youtubei/v1/, the internal InnerTube endpoints
    the web player uses, with the exact client context YouTube expects. That is
    reverse-engineered, not sanctioned: it works unauthenticated today and can
    break whenever Google changes the payload. Hence the fallback.

LAST.FM (fallback). An official, keyed, stable API built on scrobbles. It
    returns a real similarity score, which YouTube Music does not — but it goes
    blind on a lot of catalogue, returning nothing at all rather than erring.

Neither is ranked by mood. Both answer adjacency: people who played this played
that. That is the question this tool asks.
"""
import os
import sys
import time

import requests

import common

LASTFM = "https://ws.audioscrobbler.com/2.0/"
UA = {"User-Agent": "elo/0.1 (personal music research)"}

_yt = None


def yt():
    global _yt
    if _yt is None:
        try:
            from ytmusicapi import YTMusic
        except ImportError:
            sys.exit("ytmusicapi is not installed — pip install ytmusicapi")
        _yt = YTMusic()
    return _yt


def _hit(title, artist, cand_title, cand_artist):
    """Guard against a search confidently returning the wrong song."""
    a, b = common.norm(title), common.norm(cand_title)
    if not a or not b or (a not in b and b not in a):
        return False
    x, y = common.norm(artist), common.norm(cand_artist)
    return not x or not y or x in y or y in x


def ytm(title, artist, limit=50):
    """YouTube Music's radio queue. Ordered, but carries no similarity score,
    so position is the only signal it gives us."""
    try:
        res = yt().search("%s %s" % (title, artist), filter="songs", limit=5)
    except Exception as e:
        print("  youtube music search failed: %s" % str(e)[:120],
              file=sys.stderr)
        return []
    vid = None
    for r in res:
        names = ", ".join(a["name"] for a in (r.get("artists") or []))
        if _hit(title, artist, r.get("title") or "", names):
            vid = r["videoId"]
            break
    if not vid and res:
        vid = res[0]["videoId"]           # fall back to the top hit
    if not vid:
        return []
    try:
        w = yt().get_watch_playlist(videoId=vid, limit=limit)
    except Exception as e:
        print("  youtube music radio failed: %s" % str(e)[:120], file=sys.stderr)
        return []
    out = []
    for i, t in enumerate(w.get("tracks") or []):
        name = t.get("title")
        who = ", ".join(a["name"] for a in (t.get("artists") or []) if a.get("name"))
        if not name or not who:
            continue
        if i == 0 and _hit(title, artist, name, who):
            continue                      # the seed itself heads the queue
        out.append({"title": name, "artist": who, "rank": len(out) + 1,
                    "score": None, "source": "ytm",
                    "length": t.get("length") or "",
                    "album": (t.get("album") or {}).get("name") or "",
                    "videoId": t.get("videoId") or ""})
    return out


def fm(method, **params):
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        return {}
    p = {"method": method, "api_key": key, "format": "json"}
    p.update(params)
    for _ in range(3):
        try:
            r = requests.get(LASTFM, params=p, headers=UA, timeout=30)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(5)
            continue
        if not r.ok:
            return {}
        j = r.json()
        return {} if "error" in j else j
    return {}


def lastfm(title, artist, limit=100):
    j = fm("track.getSimilar", track=title, artist=artist, limit=limit,
           autocorrect=1)
    out = []
    for t in (j.get("similartracks", {}).get("track") or []):
        name, who = t.get("name"), (t.get("artist") or {}).get("name")
        if name and who:
            out.append({"title": name, "artist": who, "rank": len(out) + 1,
                        "score": float(t.get("match") or 0.0),
                        "source": "lastfm", "length": "", "album": "",
                        "videoId": ""})
    return out


def tag_top(tag, limit=50):
    """Top tracks for a Last.fm tag — this is the job tags are actually good at.

    DESIGN.md §2.1 killed Last.fm as a *mood* signal: 8% track-tag coverage on
    a real library, and the tag mass sits on genre, decade and artist identity
    rather than emotion. That same concentration is why it is a good *genre*
    candidate source. Ask it for "r&b", not for "melancholy" — it supplies the
    pool, our cards supply the mood.
    """
    j = fm("tag.getTopTracks", tag=tag, limit=limit)
    out = []
    for t in (j.get("tracks", {}).get("track") or []):
        name, who = t.get("name"), (t.get("artist") or {}).get("name")
        if name and who:
            out.append({"title": name, "artist": who, "rank": len(out) + 1,
                        "score": None, "source": "lastfm-tag:%s" % tag,
                        "length": "", "album": "", "videoId": ""})
    return out


def chart_top(limit=50):
    """Globally popular tracks — the seedless fallback when the request carries
    no genre hint at all."""
    j = fm("chart.getTopTracks", limit=limit)
    out = []
    for t in (j.get("tracks", {}).get("track") or []):
        name, who = t.get("name"), (t.get("artist") or {}).get("name")
        if name and who:
            out.append({"title": name, "artist": who, "rank": len(out) + 1,
                        "score": None, "source": "lastfm-chart",
                        "length": "", "album": "", "videoId": ""})
    return out


def fuse(groups, k=60):
    """Reciprocal rank fusion.

    The two sources are not comparable directly — Last.fm gives a 0-1 score,
    YouTube Music gives only a position. RRF throws both scores away and uses
    rank alone, which is the only thing they share, and rewards a track that
    both sources placed highly.
    """
    merged = {}
    for g in groups:
        for c in g:
            key = (common.norm(c["title"]), common.norm(c["artist"]))
            if not key[0]:
                continue
            m = merged.setdefault(key, dict(c, rrf=0.0, sources=set(),
                                            best_rank=c["rank"]))
            m["rrf"] += 1.0 / (k + c["rank"])
            m["sources"].add(c["source"])
            m["best_rank"] = min(m["best_rank"], c["rank"])
            if c.get("score") is not None:
                m["score"] = max(m.get("score") or 0.0, c["score"])
            for f in ("length", "album", "videoId"):   # keep whichever source has it
                if not m.get(f) and c.get(f):
                    m[f] = c[f]
    return sorted(merged.values(), key=lambda c: (-len(c["sources"]), -c["rrf"]))
