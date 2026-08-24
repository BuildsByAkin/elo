"""Import an Apple Music library from the file Music.app already knows how to
write.

    Music.app -> File -> Library -> Export Library...   (writes an XML plist)

Ten seconds, no account, no developer programme, no API key. The alternative is
MusicKit, which needs a paid Apple Developer membership to mint a developer
token before it will tell you anything about your own library — a hundred
dollars a year to read a file that is already on the disk. Not worth it.

The XML is also the *richest* import of the three services, and by a distance.
Spotify and YouTube Music can tell you what you saved. Apple's export tells you
what you saved, what genre it is, what year, **how many times you played it**,
**how many times you skipped it**, when you added it, what you rated it, and
every playlist you ever made. Play count is the strongest preference signal in
this project and no API offers it.

Also accepts the tab-separated file from File -> Library -> Export Playlist,
which is what you get for a single playlist, and which Music.app writes as
UTF-16 with carriage returns.
"""
import os
import plistlib
import sys

import common
import library

# Apple's plist keys -> our columns. Anything absent is simply not set.
FIELDS = (("Name", "title"), ("Artist", "artist"), ("Album Artist", "_aa"),
          ("Album", "album"), ("Genre", "genre"), ("Year", "year"),
          ("Total Time", "_ms"), ("Play Count", "plays"),
          ("Skip Count", "skips"), ("Rating", "rating"),
          ("Date Added", "added"), ("Loved", "_loved"))


def _row(t):
    """One plist track dict -> the shape library.upsert_tracks wants."""
    title = (t.get("Name") or "").strip()
    if not title:
        return None
    # Prefer the track artist; fall back to album artist, which is what
    # compilations and classical rips tend to fill in instead.
    artist = (t.get("Artist") or t.get("Album Artist") or "").strip()
    added = t.get("Date Added")
    return {
        "title": title,
        "artist": artist,
        "album": (t.get("Album") or "").strip(),
        "genre": (t.get("Genre") or "").strip(),
        "year": str(t.get("Year") or ""),
        "seconds": int((t.get("Total Time") or 0) / 1000),
        "plays": int(t.get("Play Count") or 0),
        "skips": int(t.get("Skip Count") or 0),
        "rating": int(t.get("Rating") or 0),
        "liked": 1 if t.get("Loved") else 0,
        "added": added.strftime("%Y-%m-%d") if hasattr(added, "strftime")
                 else str(added or "")[:10],
        "source": "apple",
    }


def from_xml(path):
    """Returns (tracks, playlists). Playlists are (name, [track keys])."""
    with open(path, "rb") as fh:
        lib = plistlib.load(fh)

    by_id, rows = {}, []
    for tid, t in (lib.get("Tracks") or {}).items():
        row = _row(t)
        if not row:
            continue
        # Music.app lists podcasts, audiobooks, voice memos and movies in the
        # same file. A playlist of podcasts is not a taste signal, and pushing
        # one to YouTube Music would be nonsense.
        if any(t.get(flag) for flag in ("Podcast", "Audiobook", "Movie",
                                        "TV Show", "Has Video", "Music Video")):
            continue
        rows.append(row)
        by_id[str(tid)] = common.key(row["title"], row["artist"])

    playlists = []
    for pl in (lib.get("Playlists") or []):
        name = (pl.get("Name") or "").strip()
        # Skip the containers Music.app synthesises: Library, Music, Downloaded,
        # Purchased, and the smart-folder parents. They are not curation.
        if not name or pl.get("Distinguished Kind") or pl.get("Master") \
                or pl.get("Folder"):
            continue
        keys = []
        for item in (pl.get("Playlist Items") or []):
            k = by_id.get(str(item.get("Track ID")))
            if k and k not in keys:
                keys.append(k)
        if keys:
            playlists.append((name, keys))
    return rows, playlists


def from_tsv(path):
    """The single-playlist export. Music.app writes UTF-16 with CR endings."""
    raw = open(path, "rb").read()
    text = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
            else raw.decode("utf-8-sig", errors="replace"))
    lines = text.splitlines()                  # handles \r, \n and \r\n alike
    if not lines:
        sys.exit("%s is empty" % path)
    idx = {n.strip().lower(): i for i, n in enumerate(lines[0].split("\t"))}
    if "name" not in idx:
        sys.exit("export has no 'Name' column; got %s" % sorted(idx)[:12])

    def col(parts, want, default=""):
        i = idx.get(want)
        return parts[i].strip() if i is not None and len(parts) > i else default

    rows = []
    for line in lines[1:]:
        p = line.split("\t")
        title = col(p, "name")
        if not title:
            continue
        rows.append({
            "title": title,
            "artist": col(p, "artist") or col(p, "album artist"),
            "album": col(p, "album"), "genre": col(p, "genre"),
            "year": col(p, "year"),
            "seconds": int(col(p, "time", "0") or 0),
            "plays": int(col(p, "plays", "0") or 0),
            "skips": int(col(p, "skips", "0") or 0),
            "rating": int(col(p, "rating", "0") or 0),
            "liked": 0, "added": col(p, "date added")[:10], "source": "apple"})
    name = os.path.splitext(os.path.basename(path))[0]
    keys = [common.key(r["title"], r["artist"]) for r in rows]
    return rows, ([(name, keys)] if keys else [])


def load(path, quiet=False):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        sys.exit("no such file: %s" % path)
    rows, playlists = (from_xml(path) if path.lower().endswith(".xml")
                       else from_tsv(path))
    if not rows:
        sys.exit("parsed no music out of %s — is it a library export?" % path)

    n_tracks = library.upsert_tracks(rows)
    n_pl = library.upsert_playlists("apple", playlists)
    # The genre column is free artist-tag data: no API call, no model, and it
    # is what makes "prioritise Meek Mill in the hip-hop block" possible at all.
    n_tags = library.tags_from_track_genres("apple")
    summary = {"source": "apple", "tracks": n_tracks, "playlists": n_pl,
               "artists_tagged": n_tags, "file": path}
    if not quiet:
        print("apple: %d tracks, %d playlists, %d artists tagged from genre"
              % (n_tracks, n_pl, n_tags), file=sys.stderr)
        played = sum(1 for r in rows if r["plays"])
        if played:
            print("       %d tracks carry a play count (%d plays total)"
                  % (played, sum(r["plays"] for r in rows)), file=sys.stderr)
        else:
            print("       no play counts in this export — Apple omits them "
                  "when the library is cloud-only", file=sys.stderr)
    return summary


HELP = """Export your library first — it takes about ten seconds:

  1. open Music.app
  2. File > Library > Export Library...
  3. save it anywhere, e.g. ~/Desktop/Library.xml

Then:  python elo.py import apple ~/Desktop/Library.xml

File > Library > Export Playlist writes a single playlist as tab-separated
text; that works here too. Both are read locally and nothing is uploaded."""


def main():
    if len(sys.argv) != 2:
        sys.exit(HELP)
    load(sys.argv[1])


if __name__ == "__main__":
    main()
