"""The store every importer writes into, and the YouTube Music importer.

Three services, one table. A track's identity is `norm(title)|norm(artist)`,
so the same song imported from Apple and from Spotify merges into one row that
carries whatever each service happened to know — Apple's play count and genre,
Spotify's release year, YouTube Music's videoId. Merging is the point: nobody
keeps their whole life in one service, and the union is a better picture of
taste than any single export.

Merge rules are per-column and deliberately not "last write wins":

  counts  (plays, skips, rating)  take the MAX. They are per-service tallies of
          the same listening; summing would double-count a song you have in two
          places, and overwriting would let an empty Spotify row erase Apple's
          play count.
  flags   (liked) take the OR. Liking it anywhere is liking it.
  text    (genre, year, album, videoId) fill in only if we do not have one, so
          a service that ships blanks cannot blank out a service that did not.
  added   takes the EARLIEST. When you first saved a song is a fact about you;
          re-importing should not make everything look new.
"""
import json
import sys
import time

import common

SOURCES = ("apple", "spotify", "ytmusic")


def _merge_sources(old, new):
    have = [s for s in (old or "").split(",") if s]
    if new and new not in have:
        have.append(new)
    return ",".join(have)


def upsert_tracks(rows):
    """Insert or merge track rows. Returns the number of rows touched."""
    con = common.connect()
    n = 0
    for r in rows:
        title = (r.get("title") or "").strip()
        artist = (r.get("artist") or "").strip()
        k = common.key(title, artist)
        if not k.split("|")[0]:
            continue
        old = con.execute(
            "SELECT title,artist,album,genre,year,seconds,plays,skips,rating,"
            "liked,added,sources,video_id FROM library_tracks WHERE key=?",
            (k,)).fetchone()
        new = (common.clean(title), artist, r.get("album", ""),
               r.get("genre", ""), str(r.get("year") or ""),
               int(r.get("seconds") or 0), int(r.get("plays") or 0),
               int(r.get("skips") or 0), int(r.get("rating") or 0),
               int(r.get("liked") or 0), r.get("added", ""),
               _merge_sources("", r.get("source", "")),
               r.get("video_id", ""))
        if old:
            new = (old[0] or new[0], old[1] or new[1], old[2] or new[2],
                   old[3] or new[3], old[4] or new[4],
                   max(old[5], new[5]), max(old[6], new[6]),
                   max(old[7], new[7]), max(old[8], new[8]),
                   max(old[9], new[9]),
                   min([d for d in (old[10], new[10]) if d] or [""]),
                   _merge_sources(old[11], r.get("source", "")),
                   old[12] or new[12])
        con.execute(
            "INSERT INTO library_tracks (key,title,artist,album,genre,year,"
            "seconds,plays,skips,rating,liked,added,sources,video_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET title=excluded.title,"
            " artist=excluded.artist,album=excluded.album,genre=excluded.genre,"
            " year=excluded.year,seconds=excluded.seconds,plays=excluded.plays,"
            " skips=excluded.skips,rating=excluded.rating,liked=excluded.liked,"
            " added=excluded.added,sources=excluded.sources,"
            " video_id=excluded.video_id", (k,) + new)
        n += 1
    con.commit()
    rebuild_artists()
    return n


def upsert_playlists(source, playlists):
    """`playlists` is [(name, [track_key, ...])]. Replaces this source's set."""
    con = common.connect()
    con.execute("DELETE FROM library_playlist_tracks WHERE playlist_id IN"
                " (SELECT id FROM library_playlists WHERE source=?)", (source,))
    con.execute("DELETE FROM library_playlists WHERE source=?", (source,))
    for i, (name, keys) in enumerate(playlists):
        pid = "%s:%d" % (source, i)
        con.execute("INSERT INTO library_playlists (id,name,source,n)"
                    " VALUES (?,?,?,?)", (pid, name, source, len(keys)))
        con.executemany(
            "INSERT OR IGNORE INTO library_playlist_tracks"
            " (playlist_id,track_key,pos) VALUES (?,?,?)",
            [(pid, k, pos) for pos, k in enumerate(keys)])
    con.commit()
    rebuild_artists()
    return len(playlists)


def mark_top(keys):
    """Flag tracks a service reported as your most-played. Spotify gives no
    play counts, so its top-tracks lists are the only equivalent it offers;
    recording them as a nominal count keeps one comparable column."""
    con = common.connect()
    con.executemany("UPDATE library_tracks SET plays=max(plays,?)"
                    " WHERE key=?", [(25, k) for k in keys])
    con.commit()


def mark_subscribed(names):
    con = common.connect()
    for name in names:
        k = common.norm(name)
        if not k:
            continue
        con.execute("INSERT INTO library_artists (key,name,subscribed)"
                    " VALUES (?,?,1) ON CONFLICT(key) DO UPDATE SET"
                    " subscribed=1", (k, name))
    con.commit()


def rebuild_artists():
    """Recompute the artist table from the tracks and playlists.

    Derived, never authored, so it cannot drift out of step with an import that
    merged, replaced or partially failed. A credit like "Drake, 21 Savage"
    counts for both.
    """
    con = common.connect()
    agg = {}
    pl_count = dict(con.execute(
        "SELECT track_key, count(DISTINCT playlist_id)"
        " FROM library_playlist_tracks GROUP BY track_key"))
    for k, artist, plays, liked, srcs in con.execute(
            "SELECT key,artist,plays,liked,sources FROM library_tracks"):
        for part in str(artist or "").split(","):
            name = part.strip()
            ak = common.norm(name)
            if not ak:
                continue
            a = agg.setdefault(ak, {"name": name, "tracks": 0, "plays": 0,
                                    "liked": 0, "playlists": 0, "src": ""})
            a["tracks"] += 1
            a["plays"] += plays
            a["liked"] += liked
            a["playlists"] += pl_count.get(k, 0)
            for s in (srcs or "").split(","):
                a["src"] = _merge_sources(a["src"], s)
    albums = {}
    for artist, album in con.execute(
            "SELECT artist, album FROM library_tracks WHERE album<>''"):
        for part in str(artist or "").split(","):
            ak = common.norm(part)
            if ak:
                albums.setdefault(ak, set()).add(album)

    subbed = {r[0] for r in con.execute(
        "SELECT key FROM library_artists WHERE subscribed=1")}
    con.execute("DELETE FROM library_artists")
    con.executemany(
        "INSERT INTO library_artists (key,name,tracks,albums,playlists,plays,"
        "liked,subscribed,sources) VALUES (?,?,?,?,?,?,?,?,?)",
        [(ak, a["name"], a["tracks"], len(albums.get(ak, ())), a["playlists"],
          a["plays"], a["liked"], int(ak in subbed), a["src"])
         for ak, a in agg.items()])
    for ak in subbed - set(agg):
        con.execute("INSERT OR IGNORE INTO library_artists"
                    " (key,name,subscribed) VALUES (?,?,1)", (ak, ak))
    con.commit()


# ------------------------------------------------------------- artist tags

def upsert_artist_tags(pairs, source):
    """`pairs` is [(artist name, [tag, ...])], ranked most relevant first."""
    con = common.connect()
    now = time.strftime("%Y-%m-%d")
    n = 0
    for name, tags in pairs:
        k = common.norm(name)
        tags = [t.strip().lower() for t in tags if t and t.strip()]
        if not k or not tags:
            continue
        con.execute(
            "INSERT INTO artist_tags (key,tags,source,fetched)"
            " VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
            " tags=excluded.tags, source=excluded.source,"
            " fetched=excluded.fetched",
            (k, ",".join(tags[:12]), source, now))
        n += 1
    con.commit()
    return n


def tags_from_track_genres(source):
    """Derive artist tags from the per-track genre column Apple ships.

    Free — no API call, no model. An artist's tags are the genres their tracks
    carry, most common first, which for a tagged library is both accurate and
    already in your own vocabulary rather than Last.fm's.
    """
    con = common.connect()
    counts = {}
    for artist, genre in con.execute(
            "SELECT artist, genre FROM library_tracks WHERE genre<>''"):
        for part in str(artist or "").split(","):
            k = common.norm(part)
            if not k:
                continue
            bucket = counts.setdefault(k, [part.strip(), {}])
            g = genre.strip().lower()
            bucket[1][g] = bucket[1].get(g, 0) + 1
    pairs = []
    for k, (name, genres) in counts.items():
        ranked = [g for g, _ in sorted(genres.items(), key=lambda x: -x[1])]
        pairs.append((name, ranked))
    return upsert_artist_tags(pairs, source)


def backfill_tags_from_lastfm(limit=120, quiet=False):
    """Ask Last.fm for tags on the biggest artists we still know nothing about.

    Only the top `limit` by tracks owned, because this is one HTTP call each and
    the tail does not change any ranking — an artist you own one song by is not
    going to swing a segment either way.
    """
    import sources
    con = common.connect()
    known = {r[0] for r in con.execute("SELECT key FROM artist_tags")}
    todo = [(k, n) for k, n in con.execute(
        "SELECT key, name FROM library_artists ORDER BY tracks DESC, plays DESC")
        if k not in known][:limit]
    if not todo:
        return 0
    if not quiet:
        print("  fetching tags for %d artists from last.fm..." % len(todo),
              file=sys.stderr)
    pairs = []
    for _, name in todo:
        j = sources.fm("artist.getTopTags", artist=name, autocorrect=1)
        tags = [t["name"] for t in ((j.get("toptags") or {}).get("tag") or [])
                if t.get("name")]
        if tags:
            pairs.append((name, tags))
    return upsert_artist_tags(pairs, "lastfm")


# ---------------------------------------------------- youtube music importer

def _artists(entry):
    return ", ".join(a["name"] for a in (entry.get("artists") or [])
                     if a.get("name"))


def load_ytmusic(quiet=False):
    # Check first. A signed-out session returns a valid, empty library rather
    # than an error, so without this the import "succeeds" with zero tracks
    # and silently wipes whatever YouTube Music had contributed before.
    import ytauth
    state = ytauth.require("importing your YouTube Music library")
    if not quiet and state.get("account"):
        print("ytmusic: signed in as %s" % state["account"], file=sys.stderr)
    yt = ytauth.client()
    warn = []

    def grab(label, fn):
        try:
            return fn()
        except Exception as e:
            warn.append("%s: %s" % (label, str(e)[:90]))
            return None

    songs = grab("songs", lambda: yt.get_library_songs(limit=5000)) or []
    uploads = grab("uploads",
                   lambda: yt.get_library_upload_songs(limit=5000)) or []
    liked = (grab("liked", lambda: yt.get_liked_songs(limit=5000))
             or {}).get("tracks") or []
    subs = grab("subscriptions",
                lambda: yt.get_library_subscriptions(limit=1000)) or []

    rows = []
    for group, is_liked in ((songs, 0), (uploads, 0), (liked, 1)):
        for t in group:
            if not t.get("title"):
                continue
            album = t.get("album")
            rows.append({
                "title": t["title"], "artist": _artists(t) or "",
                "album": (album or {}).get("name") if isinstance(album, dict)
                         else (album or ""),
                "genre": "", "year": "",
                "seconds": int(t.get("duration_seconds") or 0),
                "plays": 0, "skips": 0, "rating": 0, "liked": is_liked,
                "added": "", "source": "ytmusic",
                "video_id": t.get("videoId") or ""})

    playlists = []
    for pl in (grab("playlists",
                    lambda: yt.get_library_playlists(limit=200)) or []):
        pid = pl.get("playlistId")
        if not pid:
            continue
        full = grab("playlist %s" % pid,
                    lambda p=pid: yt.get_playlist(p, limit=500)) or {}
        keys = []
        for t in (full.get("tracks") or []):
            if not t.get("title"):
                continue
            rows.append({"title": t["title"], "artist": _artists(t) or "",
                         "album": (t.get("album") or {}).get("name") or "",
                         "genre": "", "year": "",
                         "seconds": int(t.get("duration_seconds") or 0),
                         "plays": 0, "skips": 0, "rating": 0, "liked": 0,
                         "added": "", "source": "ytmusic",
                         "video_id": t.get("videoId") or ""})
            k = common.key(t["title"], _artists(t) or "")
            if k not in keys:
                keys.append(k)
        if keys:
            playlists.append((pl.get("title") or pid, keys))

    n = upsert_tracks(rows)
    n_pl = upsert_playlists("ytmusic", playlists)
    mark_subscribed([s.get("artist") or s.get("title") or "" for s in subs])
    if not rows:
        # We know the session is live because `require` proved it above, so an
        # empty result here really does mean an empty library rather than dead
        # credentials — which is worth saying, since the two used to be
        # indistinguishable and everyone's first guess is still the cookie.
        warn.append("signed in, but YouTube Music returned no saved music — "
                    "this account's library really is empty")
    summary = {"source": "ytmusic", "tracks": n, "playlists": n_pl,
               "subscribed": len(subs), "warnings": warn}
    if not quiet:
        print("ytmusic: %d tracks, %d playlists, %d subscribed"
              % (n, n_pl, len(subs)), file=sys.stderr)
        for w in warn:
            print("  warn: %s" % w, file=sys.stderr)
    return summary


# -------------------------------------------------------------------- entry

def load(source, path=None, quiet=False):
    if source == "apple":
        import apple
        if not path:
            sys.exit(apple.HELP)
        summary = apple.load(path, quiet)
    elif source == "spotify":
        import spotify
        summary = spotify.load(quiet)
    elif source == "ytmusic":
        summary = load_ytmusic(quiet)
    else:
        sys.exit("unknown source %r — one of: %s" % (source,
                                                     ", ".join(SOURCES)))
    backfill_tags_from_lastfm(quiet=quiet)
    con = common.connect()
    summary["at"] = time.strftime("%Y-%m-%d %H:%M")
    con.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)",
                ("import:%s" % source, json.dumps(summary)))
    con.commit()
    import taste
    taste.invalidate()
    return summary


def imports():
    con = common.connect()
    out = {}
    for k, v in con.execute("SELECT k,v FROM meta WHERE k LIKE 'import:%'"):
        out[k.split(":", 1)[1]] = json.loads(v)
    return out
