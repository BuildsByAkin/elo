"""Lyric fetch + cache for the tracks the model cannot read from metadata.

Two open sources, tried in order. LRCLIB (lrclib.net) is a plain JSON API with
no auth and clean plain-text lyrics. Genius has no usable public API — its
official api.genius.com 401s — but the search endpoint its own site calls
answers unauthenticated, and the song page carries the text in
`data-lyrics-container` divs. Both are needed: the coverage test found Genius
wins on rap, LRCLIB wins on African music, and the union beats either.

Misses are cached as source='none' so a re-run does not re-fetch them.
"""
import html
import re
import sys
import time

import requests

from common import norm

LRC_UA = {"User-Agent": "elo-probe/0.1 (research; admin@whi-ff.com)"}
WEB_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 "
                       "Safari/537.36"}

LYRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lyrics (
    track_id INTEGER PRIMARY KEY,
    source   TEXT NOT NULL,          -- lrclib | genius | none
    text     TEXT NOT NULL DEFAULT '',
    chars    INTEGER NOT NULL DEFAULT 0
);
"""


def ensure(con):
    con.executescript(LYRICS_SCHEMA)
    con.commit()


def _get(url, **kw):
    """One retry on transport error or 429; None rather than raising."""
    for attempt in (0, 1):
        try:
            r = requests.get(url, timeout=25, **kw)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 429:
            time.sleep(5)
            continue
        return r if r.ok else None
    return None


def _titles_match(want_title, want_artist, got_title, got_artist):
    """Guard against a search returning a confidently wrong song."""
    a, b = norm(want_title), norm(got_title)
    if not a or not b or (a not in b and b not in a):
        return False
    x, y = norm(want_artist), norm(got_artist)
    return not x or not y or x in y or y in x


def from_lrclib(title, artist):
    # /api/get is an exact signature lookup and is both cheaper and safer than
    # search — no fuzzy hit to second-guess. It carries most of the coverage;
    # search is the fallback for near-miss titles.
    r = _get("https://lrclib.net/api/get", headers=LRC_UA,
             params={"track_name": title, "artist_name": artist})
    if r:
        text = ((r.json() or {}).get("plainLyrics") or "").strip()
        if text:
            return text

    r = _get("https://lrclib.net/api/search", headers=LRC_UA,
             params={"track_name": title, "artist_name": artist})
    if not r:
        return None
    for hit in (r.json() or [])[:6]:
        text = (hit.get("plainLyrics") or "").strip()
        if text and _titles_match(title, artist, hit.get("trackName") or "",
                                  hit.get("artistName") or ""):
            return text
    return None


_LYRIC_DIV = re.compile(r'data-lyrics-container="true"[^>]*>(.*?)</div>', re.S)
_CREDIT = re.compile(r"^\s*(\d+\s+Contributors?|Translations)\b.*?Lyrics", re.S)


def from_genius(title, artist):
    r = _get("https://genius.com/api/search/multi", headers=WEB_UA,
             params={"q": "%s %s" % (title, artist)})
    if not r:
        return None
    url = None
    for sec in r.json().get("response", {}).get("sections", []):
        if sec.get("type") != "song":
            continue
        for hit in sec.get("hits", [])[:5]:
            res = hit["result"]
            if _titles_match(title, artist, res.get("title") or "",
                             (res.get("primary_artist") or {}).get("name") or ""):
                url = res.get("url")
                break
        break
    if not url:
        return None
    page = _get(url, headers=WEB_UA)
    if not page:
        return None
    body = "\n".join(_LYRIC_DIV.findall(page.text))
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    body = _CREDIT.sub("", body, count=1)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body or None


def fetch(con, tracks, pause=0.4):
    """Fetch and cache lyrics for tracks with no cache row yet."""
    ensure(con)
    have = {r[0] for r in con.execute("SELECT track_id FROM lyrics")}
    todo = [t for t in tracks if t["id"] not in have]
    print("lyrics: %d cached, %d to fetch" % (len(tracks) - len(todo), len(todo)),
          file=sys.stderr)
    for i, t in enumerate(todo, 1):
        text = from_lrclib(t["title"], t["artist"])
        source = "lrclib"
        if not text:
            text = from_genius(t["title"], t["artist"])
            source = "genius"
        if not text:
            source, text = "none", ""
        con.execute("INSERT OR REPLACE INTO lyrics VALUES (?,?,?,?)",
                    (t["id"], source, text, len(text)))
        con.commit()
        print("  %3d/%d  %-8s %s — %s" % (i, len(todo), source,
                                          t["title"][:34], t["artist"][:22]),
              file=sys.stderr)
        time.sleep(pause)


def load(con, ids):
    ensure(con)
    ids = list(ids)
    if not ids:
        return {}
    q = "SELECT track_id, source, text FROM lyrics WHERE track_id IN (%s)" % (
        ",".join("?" * len(ids)))
    return {r[0]: (r[1], r[2]) for r in con.execute(q, ids)}


def summarise(con):
    rows = dict(con.execute("SELECT source, count(*) FROM lyrics GROUP BY source"))
    total = sum(rows.values())
    if not total:
        print("no lyrics cached yet")
        return
    found = total - rows.get("none", 0)
    print("\n=== LYRICS ===")
    print("cached   %d" % total)
    print("found    %d (%.0f%%)" % (found, 100.0 * found / total))
    for src, n in sorted(rows.items(), key=lambda x: -x[1]):
        print("  %-8s %4d" % (src, n))


def main():
    import argparse

    import common

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=0,
                    help="only fetch this many, to sanity-check the output first")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()

    con = common.connect()
    if a.stats:
        return summarise(con)

    q = ("SELECT id, title, artist FROM tracks WHERE external=0 AND title!=''"
         " AND id NOT IN (SELECT track_id FROM lyrics) ORDER BY id")
    if a.limit:
        q += " LIMIT %d" % a.limit
    todo = [dict(zip(("id", "title", "artist"), r)) for r in con.execute(q)]
    if not todo:
        print("every track already has a lyrics row (hits and misses are both"
              " cached) — nothing to fetch")
        return summarise(con)
    fetch(con, todo)
    summarise(con)


if __name__ == "__main__":
    main()
