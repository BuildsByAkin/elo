"""Where candidate tracks come from. No mood model, no tagging — just other
people's listening, asked four different ways.

RADIO (YouTube Music, unauthenticated). `get_watch_playlist` is the queue
    YouTube Music builds when you press play: Google's own "up next", derived
    from what people actually played after this song. This is the core signal
    the whole tool rests on. There is no official API for it; ytmusicapi
    reaches the internal InnerTube endpoints the web player uses, which works
    today and can break whenever Google changes the payload.

SIMILAR (Last.fm). An official, keyed, stable API built on scrobbles, and the
    only source that returns a real similarity *score*. It goes blind on a lot
    of catalogue — returning nothing rather than erring — so it is a second
    opinion, not a replacement.

MOOD POOL (YouTube Music). `get_mood_categories` exposes eleven human-curated
    moods (Chill, Sad, Party, Workout...) and twenty-seven genres, each backed
    by dozens of editorial playlists of eighty-odd tracks. This is how a
    segment gets candidates when nothing similar to the seed belongs in it —
    you cannot reach "happy" by asking a sad song's radio for neighbours.

TAG TOP (Last.fm). `tag.getTopTracks` for the same job when a mood or genre
    word has no YouTube Music category. Ask it for "shoegaze", not for
    "melancholy": its tag mass sits on genre and decade, not emotion.

Radio and mood-pool tracks arrive carrying a videoId and a duration, which
means they can be pushed to a playlist with no further lookup and their length
is known rather than assumed. Last.fm tracks carry neither and have to be
resolved at push time. That asymmetry is why fusion keeps whichever fields a
source happened to supply.
"""
import os
import sys
import threading
import time

import requests

import cache
import common

LASTFM = "https://ws.audioscrobbler.com/2.0/"
UA = {"User-Agent": "elo/0.2 (personal music research)"}

_local = threading.local()
_moods_lock = threading.Lock()
_moods = None


def yt():
    """One unauthenticated client per thread. ytmusicapi wraps a requests
    Session, and candidate gathering runs several fetches concurrently."""
    client = getattr(_local, "yt", None)
    if client is None:
        try:
            from ytmusicapi import YTMusic
        except ImportError:
            sys.exit("ytmusicapi is not installed — pip install ytmusicapi")
        client = _local.yt = YTMusic()
    return client


def _cand(title, artist, rank, source, **kw):
    return {"title": common.clean(title), "artist": artist,
            "album": kw.get("album", ""), "video_id": kw.get("video_id", ""),
            "secs": kw.get("secs", 0), "year": kw.get("year", ""),
            "rank": rank, "score": kw.get("score"), "source": source}


def _hit(want_t, want_a, got_t, got_a):
    """Guard against a search confidently returning the wrong song."""
    a, b = common.norm(want_t), common.norm(got_t)
    if not a or not b or (a not in b and b not in a):
        return False
    x, y = common.norm(want_a), common.norm(got_a)
    return not x or not y or x in y or y in x


def _artists(entry):
    return ", ".join(a["name"] for a in (entry.get("artists") or [])
                     if a.get("name"))


def _slim(t):
    """The six fields we actually read, for storing in the cache.

    ytmusicapi hands back the whole InnerTube row — thumbnail sets at five
    resolutions, tracking params, feedback tokens, like status. Cached whole,
    one three-segment request weighed nearly a megabyte, almost all of it
    fields nothing here has ever looked at. Slimming to what is read makes the
    cache about a tenth the size and, more usefully, makes its format an
    explicit decision rather than "whatever the library happened to return".
    """
    return {"title": t.get("title"),
            "artists": [{"name": a["name"]}
                        for a in (t.get("artists") or []) if a.get("name")],
            "album": {"name": (t.get("album") or {}).get("name") or ""}
                     if isinstance(t.get("album"), dict) else None,
            "videoId": t.get("videoId"),
            "year": t.get("year"),
            "length": t.get("length"),
            "duration": t.get("duration"),
            "duration_seconds": t.get("duration_seconds")}


def find(title, artist):
    """Resolve a title/artist to a YouTube Music videoId, or None.

    Falls back to the top hit when nothing matches confidently, because for
    seeding a radio a near-miss still lands in roughly the right neighbourhood.
    Push does *not* do this — there a wrong match ends up in your playlist.
    """
    def go():
        try:
            res = yt().search("%s %s" % (title, artist), filter="songs",
                              limit=5)
        except Exception as e:
            print("  search failed: %s" % str(e)[:120], file=sys.stderr)
            return ""
        for r in res:
            if _hit(title, artist, r.get("title") or "", _artists(r)):
                return r.get("videoId") or ""
        return (res[0].get("videoId") or "") if res else ""

    return cache.wrap("search", (common.key(title, artist),), go) or None


def radio(title, artist, limit=50):
    """What YouTube Music plays after this song."""
    vid = find(title, artist)
    if not vid:
        return []

    def go():
        try:
            w = yt().get_watch_playlist(videoId=vid, limit=limit)
        except Exception as e:
            print("  radio failed: %s" % str(e)[:120], file=sys.stderr)
            return []
        return [_slim(t) for t in (w.get("tracks") or [])]

    out = []
    for i, t in enumerate(cache.wrap("radio", (vid, limit), go)):
        name, who = t.get("title"), _artists(t)
        if not name or not who:
            continue
        if i == 0 and _hit(title, artist, name, who):
            continue                      # the seed itself heads the queue
        out.append(_cand(name, who, len(out) + 1, "radio",
                         album=(t.get("album") or {}).get("name") or "",
                         video_id=t.get("videoId") or "",
                         year=str(t.get("year") or ""),
                         secs=common.seconds(t.get("length"))))
    return out


# ------------------------------------------------------------ youtube moods

def mood_categories():
    """The eleven moods and twenty-seven genres YouTube Music curates.

    Fetched once per process and shared: it is the same list for everybody and
    changes about as often as Google redesigns the home page.
    """
    global _moods
    with _moods_lock:
        if _moods is None:
            def go():
                try:
                    return yt().get_mood_categories() or {}
                except Exception as e:
                    print("  mood categories failed: %s" % str(e)[:120],
                          file=sys.stderr)
                    return {}
            _moods = cache.wrap("categories", ("v1",), go)
    return _moods


def mood_names():
    """Flat list of every category title, moods first."""
    cats = mood_categories()
    out = []
    for section in ("Moods & moments", "Genres"):
        out += [c["title"] for c in cats.get(section, [])]
    for section, items in cats.items():
        if section not in ("Moods & moments", "Genres"):
            out += [c["title"] for c in items]
    return out


def _mood_params(name):
    want = common.norm(name)
    if not want:
        return None
    best = None
    for items in mood_categories().values():
        for c in items:
            got = common.norm(c["title"])
            if got == want:
                return c["params"]
            if best is None and (want in got or got in want):
                best = c["params"]
    return best


# Shelves that are on the page but are not music we can queue.
_SKIP_SHELF = ("music videos", "artists", "new releases", "videos",
               "featured artists", "similar artists")


def _shelves(params):
    """Parse a mood/genre category page into `{title, songs, playlists}`.

    ytmusicapi has `get_mood_playlists` for this, and it works on the eleven
    mood categories and fails on all twenty-seven genre ones — a genre page
    leads with a shelf of songs rather than playlists, and the parser walks
    into it expecting a playlist and raises. Rather than lose every genre pool,
    the page is walked here.

    That means reaching past the library into `_send_request`, which is a
    private method. It is a smaller bet than it sounds: the endpoint underneath
    is already unofficial, and this parser is *more* tolerant than the one it
    replaces — it skips shelves it does not recognise instead of raising on
    them, so the next time YouTube Music adds a shelf type it costs us that
    shelf rather than the request.

    The trade is worth making because the genre pages are the better data. A
    genre page hands back fifty songs directly and a hundred and forty-five
    playlists; a mood page has a few dozen playlists and no songs.
    """
    hit = cache.get("shelves", params)
    if hit is not None:
        return hit
    try:
        r = yt()._send_request("browse", {
            "browseId": "FEmusic_moods_and_genres_category",
            "params": params})
        sections = (r["contents"]["singleColumnBrowseResultsRenderer"]
                     ["tabs"][0]["tabRenderer"]["content"]
                     ["sectionListRenderer"]["contents"])
    except Exception as e:
        print("  category page failed: %s" % str(e)[:120], file=sys.stderr)
        return cache.put("shelves", [], params)

    out = []
    for section in sections:
        shelf = section.get("musicCarouselShelfRenderer")
        if not shelf:
            continue
        try:
            title = (shelf["header"]["musicCarouselShelfBasicHeaderRenderer"]
                          ["title"]["runs"][0]["text"])
        except (KeyError, IndexError):
            title = ""
        if common.norm(title) in [common.norm(s) for s in _SKIP_SHELF]:
            continue
        songs, pls = [], []
        for item in shelf.get("contents") or []:
            row = item.get("musicResponsiveListItemRenderer")
            if row:
                cols = []
                for fc in row.get("flexColumns") or []:
                    runs = (fc.get("musicResponsiveListItemFlexColumnRenderer")
                            or {}).get("text", {}).get("runs") or []
                    cols.append([x.get("text", "") for x in runs])
                vid = (row.get("playlistItemData") or {}).get("videoId") or ""
                if cols and cols[0] and vid:
                    who = " ".join(cols[1]).split(" • ")[0] if len(cols) > 1 \
                        else ""
                    songs.append((cols[0][0], who.strip(), vid))
                continue
            two = item.get("musicTwoRowItemRenderer")
            if not two:
                continue
            try:
                name = two["title"]["runs"][0]["text"]
                bid = two["navigationEndpoint"]["browseEndpoint"]["browseId"]
            except (KeyError, IndexError):
                continue
            if bid.startswith("VL"):        # everything else is an artist etc
                pls.append({"id": bid[2:], "title": name})
        if songs or pls:
            # JSON round-trips lists, not tuples; keep the shape stable so a
            # hit and a miss are indistinguishable to the caller.
            out.append({"title": title, "songs": [list(s) for s in songs],
                        "playlists": pls})
    return cache.put("shelves", out, params)


def mood_pool(name, genres=(), playlists=3, per=60):
    """Tracks from a YouTube Music mood or genre category.

    A category holds dozens of curated playlists and we only want a few. When
    the request named a genre, playlists whose title or shelf mentions it sort
    first — `Hip Hop Heartbreak` beats `Country Breakup` for an r&b request —
    and that is a one-line ranking rather than another model call. Otherwise
    the category's own ordering stands, which is YouTube Music's editorial
    priority.
    """
    params = _mood_params(name)
    if not params:
        return []
    shelves = _shelves(params)
    if not shelves:
        return []
    words = [common.norm(g) for g in genres if common.norm(g)]

    out = []
    for sh in shelves:                    # songs sitting on the page directly
        for i, (title, who, vid) in enumerate(sh["songs"]):
            if title and who:
                out.append(_cand(title, who, i + 1,
                                 "mood:%s/%s" % (name, sh["title"][:24]),
                                 video_id=vid))

    ranked = []
    for order, sh in enumerate(shelves):
        for i, pl in enumerate(sh["playlists"]):
            hit = sum(w in common.norm(pl["title"] + " " + sh["title"])
                      for w in words)
            ranked.append((-hit, order, i, pl))
    ranked.sort(key=lambda r: r[:3])

    for _, _, _, pl in ranked[:playlists]:
        def go(pid=pl["id"]):
            try:
                full = yt().get_playlist(pid, limit=per)
            except Exception as e:
                print("  playlist %s failed: %s" % (pid, str(e)[:90]),
                      file=sys.stderr)
                return {}
            return {"title": full.get("title") or "",
                    "tracks": [_slim(t) for t in (full.get("tracks") or [])]}

        full = cache.wrap("playlist", (pl["id"], per), go)
        label = "mood:%s/%s" % (name, (full.get("title") or pl["title"])[:24])
        for i, t in enumerate(full.get("tracks") or []):
            name_, who = t.get("title"), _artists(t)
            if not name_ or not who:
                continue
            out.append(_cand(name_, who, i + 1, label,
                             album=(t.get("album") or {}).get("name") or "",
                             video_id=t.get("videoId") or "",
                             secs=common.seconds(t.get("duration"))
                                  or int(t.get("duration_seconds") or 0)))
    return out


# ------------------------------------------------------------------ last.fm

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


def _rows(j, path, with_match=False):
    """Last.fm replies carry mbids, urls, streamable flags and four image
    sizes per track. Reduce to the two or three fields anyone reads before it
    reaches the cache."""
    node = j
    for step in path:
        node = (node or {}).get(step) or {}
    out = []
    for t in (node if isinstance(node, list) else []):
        name, who = t.get("name"), (t.get("artist") or {}).get("name")
        if not name or not who:
            continue
        out.append([name, who, float(t.get("match") or 0.0)] if with_match
                   else [name, who])
    return out


def similar(title, artist, limit=100):
    rows = cache.wrap(
        "similar", (common.key(title, artist), limit),
        lambda: _rows(fm("track.getSimilar", track=title, artist=artist,
                         limit=limit, autocorrect=1),
                      ("similartracks", "track"), with_match=True))
    return [_cand(n, a, i + 1, "similar", score=m)
            for i, (n, a, m) in enumerate(rows)]


def tag_top(tag, limit=50):
    rows = cache.wrap(
        "tag", (common.norm(tag), limit),
        lambda: _rows(fm("tag.getTopTracks", tag=tag, limit=limit),
                      ("tracks", "track")))
    return [_cand(n, a, i + 1, "tag:%s" % tag)
            for i, (n, a) in enumerate(rows)]


def chart_top(limit=50):
    """The seedless, moodless last resort."""
    rows = cache.wrap(
        "chart", (limit,),
        lambda: _rows(fm("chart.getTopTracks", limit=limit),
                      ("tracks", "track")))
    return [_cand(n, a, i + 1, "chart") for i, (n, a) in enumerate(rows)]


# -------------------------------------------------------------------- merge

def fuse(groups, k=60):
    """Reciprocal rank fusion over `(weight, candidates)` groups.

    The sources are not comparable directly — Last.fm gives a 0-1 score, the
    others give only a position. RRF throws the scores away and uses rank
    alone, the one thing they share, and rewards a track more than one source
    placed highly. Agreement between an editorial playlist and a radio queue is
    the strongest evidence we have that a track belongs.

    The per-group weight is what lets one seed serve a whole journey. In a
    sad-to-happy shift the seed's radio is fetched once and offered to every
    segment, at full weight in the opening block and at a third of it later
    on. It stops being a source of tracks and becomes a tiebreaker: a song that
    appears in both the seed's radio and the *happy* pool is a bridge between
    where the listener is and where they are going, and it rises above the
    generically happy tracks around it without ever outranking them on its own.
    """
    merged = {}
    for weight, g in groups:
        for c in g:
            k_ = common.key(c["title"], c["artist"])
            if not k_.split("|")[0]:
                continue
            m = merged.get(k_)
            if m is None:
                m = merged[k_] = dict(c, rrf=0.0, sources=set(), key=k_)
            m["rrf"] += weight / (k + c["rank"])
            m["sources"].add(c["source"].split("/")[0])
            if c.get("score") is not None:
                m["score"] = max(m.get("score") or 0.0, c["score"])
            # Only the radio queue reports a release year — the curated
            # playlists and Last.fm both omit it entirely — so whichever
            # source happened to know it fills it in for the merged row.
            for f in ("album", "video_id", "year"):
                if not m.get(f) and c.get(f):
                    m[f] = c[f]
            if not m.get("secs") and c.get("secs"):
                m["secs"] = c["secs"]
    return sorted(merged.values(), key=lambda c: -c["rrf"])
