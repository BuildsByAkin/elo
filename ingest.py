#!/usr/bin/env python3
"""Parse a Music.app library export into data/elo.db.

Accepts the XML plist Music.app writes (File > Library > Export Library) or the
tab-separated export (File > Library > Export Playlist). The TSV carries a
`Time` column in seconds, which the arc builder needs to fill a duration.

    python ingest.py ~/Desktop/library.txt
"""
import os
import plistlib
import sys

from common import DB, connect

FIELDS = ("title", "artist", "album", "genre", "year", "seconds")


def from_xml(path):
    with open(path, "rb") as fh:
        lib = plistlib.load(fh)
    for t in lib.get("Tracks", {}).values():
        if t.get("Name"):
            yield (t["Name"], t.get("Artist") or "", t.get("Album") or "",
                   t.get("Genre") or "", str(t.get("Year") or ""),
                   int((t.get("Total Time") or 0) / 1000))


def from_tsv(path):
    """Music.app writes UTF-16 with CR line endings; plain UTF-8 also works."""
    raw = open(path, "rb").read()
    text = (raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff")
            else raw.decode("utf-8-sig", errors="replace"))
    lines = text.splitlines()                  # handles \r, \n and \r\n alike
    if not lines:
        sys.exit("%s is empty" % path)
    idx = {n.strip().lower(): i for i, n in enumerate(lines[0].split("\t"))}
    for key in ("name", "artist", "album"):
        if key not in idx:
            sys.exit("export has no %r column; got %s" % (key, sorted(idx)[:12]))
    for line in lines[1:]:
        col = line.split("\t")
        get = lambda k: (col[idx[k]].strip()
                         if k in idx and len(col) > idx[k] else "")
        if get("name"):
            yield (get("name"), get("artist"), get("album"), get("genre"),
                   get("year"), int(get("time") or 0))


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python ingest.py <Library.xml | library.txt>")
    path = os.path.expanduser(sys.argv[1])
    rows = list(from_xml(path) if path.lower().endswith(".xml")
                else from_tsv(path))

    con = connect(create=True)
    before = con.execute("SELECT count(*) FROM tracks").fetchone()[0]
    # Upsert so a re-ingest backfills columns (duration, genre) on rows that
    # were loaded by an earlier version of this script.
    con.executemany(
        "INSERT INTO tracks (title, artist, album, genre, year, seconds)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT (title, artist, album) DO UPDATE SET"
        "   genre=excluded.genre, year=excluded.year,"
        "   seconds=max(tracks.seconds, excluded.seconds)", rows)
    con.commit()
    after = con.execute("SELECT count(*) FROM tracks").fetchone()[0]
    stats = con.execute(
        "SELECT count(*), sum(artist=''), sum(seconds=0) FROM tracks"
        " WHERE external=0").fetchone()

    print("parsed  %d rows from %s" % (len(rows), path))
    print("added   %d new, %d updated" % (after - before,
                                          len(rows) - (after - before)))
    print("total   %d owned tracks in %s" % (stats[0], DB))
    if stats[1]:
        print("WARN    %d tracks have no artist" % stats[1])
    if stats[2]:
        print("WARN    %d tracks have no duration (arc will estimate)" % stats[2])


if __name__ == "__main__":
    main()
