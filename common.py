"""Shared plumbing: env, the library cache, title matching, and the LLM call.

The previous version of this file carried a schema for a tagged corpus — every
track scored for valence, energy and theme so the engine could select by mood
coordinates. That is gone. Nothing here tags music any more.

What survives is small on purpose: the database is now a *cache of your own
library*, not a model of the world's music. Co-listening data lives at YouTube
Music and Last.fm and is fetched fresh per request; the only thing worth
keeping on disk is the one fact those two services cannot tell us, which is
what you personally own.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata

import requests


def _load_env():
    """Read .env beside this file so keys never land in the shell history."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
CLI_MODEL = os.environ.get("ELO_MODEL", "sonnet")
API_MODEL = os.environ.get("ELO_API_MODEL", "claude-sonnet-5")
DB = os.environ.get("ELO_DB") or os.path.join(HERE, "data", "elo.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_tracks (
    key      TEXT PRIMARY KEY,             -- norm(title)|norm(artist)
    title    TEXT NOT NULL,
    artist   TEXT NOT NULL DEFAULT '',
    album    TEXT NOT NULL DEFAULT '',
    genre    TEXT NOT NULL DEFAULT '',     -- Apple ships one per track
    year     TEXT NOT NULL DEFAULT '',
    seconds  INTEGER NOT NULL DEFAULT 0,
    plays    INTEGER NOT NULL DEFAULT 0,   -- the strongest signal in the file
    skips    INTEGER NOT NULL DEFAULT 0,   -- and the only negative one
    rating   INTEGER NOT NULL DEFAULT 0,   -- 0-100, Apple's star rating x20
    liked    INTEGER NOT NULL DEFAULT 0,
    added    TEXT NOT NULL DEFAULT '',     -- ISO date, for recency
    sources  TEXT NOT NULL DEFAULT '',     -- apple,spotify,ytmusic
    video_id TEXT NOT NULL DEFAULT ''      -- known only from YouTube Music
);
CREATE TABLE IF NOT EXISTS library_artists (
    key        TEXT PRIMARY KEY,           -- norm(name)
    name       TEXT NOT NULL,
    tracks     INTEGER NOT NULL DEFAULT 0, -- songs of theirs you have
    albums     INTEGER NOT NULL DEFAULT 0,
    playlists  INTEGER NOT NULL DEFAULT 0, -- how many of yours they appear in
    plays      INTEGER NOT NULL DEFAULT 0,
    liked      INTEGER NOT NULL DEFAULT 0,
    subscribed INTEGER NOT NULL DEFAULT 0,
    sources    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS library_playlists (
    id     TEXT PRIMARY KEY,               -- source-prefixed
    name   TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    n      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS library_playlist_tracks (
    playlist_id TEXT NOT NULL,
    track_key   TEXT NOT NULL,
    pos         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, track_key)
);
CREATE INDEX IF NOT EXISTS plt_track ON library_playlist_tracks (track_key);
CREATE TABLE IF NOT EXISTS artist_tags (
    key     TEXT PRIMARY KEY,              -- norm(name)
    tags    TEXT NOT NULL DEFAULT '',      -- comma-separated, ranked
    source  TEXT NOT NULL DEFAULT '',      -- apple | spotify | lastfm
    fetched TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS feedback (
    id        INTEGER PRIMARY KEY,
    track_key TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    artist    TEXT NOT NULL DEFAULT '',
    verdict   INTEGER NOT NULL,          -- -1 rejected, +1 kept
    mood      TEXT NOT NULL DEFAULT '',  -- the block it appeared in
    tags      TEXT NOT NULL DEFAULT '',
    request   TEXT NOT NULL DEFAULT '',  -- what was asked for, for provenance
    at        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS feedback_track ON feedback (track_key);
CREATE TABLE IF NOT EXISTS last_playlist (
    pos       INTEGER PRIMARY KEY,       -- the number printed next to it
    track_key TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT '',
    artist    TEXT NOT NULL DEFAULT '',
    mood      TEXT NOT NULL DEFAULT '',
    tags      TEXT NOT NULL DEFAULT '',
    request   TEXT NOT NULL DEFAULT '',
    at        TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pushed (
    playlist_id TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    request     TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',  -- the track set, order-independent
    n           INTEGER NOT NULL DEFAULT 0,
    at          TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cache (
    k       TEXT PRIMARY KEY,              -- kind:arguments
    kind    TEXT NOT NULL,
    v       TEXT NOT NULL,                 -- JSON
    fetched REAL NOT NULL,                 -- unix seconds
    ttl     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS cache_kind ON cache (kind);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect():
    """Open the store, creating it on first run. An empty one is normal — it
    means we have no library yet, not that anything is wrong.

    Candidate gathering runs several fetches concurrently and each one writes
    its result to the cache, so a plain connection would eventually collide on
    the write lock and raise. A busy timeout makes the loser wait rather than
    fail; there is no contention worth optimising past that, since the writes
    are small and the readers are the same process.
    """
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout = 30000")
    con.executescript(SCHEMA)
    con.commit()
    return con


_SUFFIX = re.compile(
    r"\b(remaster(ed)?|remix|live|deluxe|edition|version|mono|stereo|"
    r"radio edit|extended|acoustic|instrumental|bonus track|explicit|"
    r"official (music )?video|official (lyric|audio) video|lyric video|"
    r"visualizer|audio)\b")


def norm(s):
    """Aggressive normalisation for cross-source title/artist matching.

    Keeps every script. Accents are folded so `Café` matches `Cafe`, but the
    character class is Unicode-aware, so `東京` survives as `東京` rather than
    normalising to the empty string and colliding with every other non-Latin
    title.
    """
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # (feat. X), [Remix]
    s = re.sub(r"\b(feat|ft|featuring|with)\b.*", " ", s)
    s = s.replace("&", " and ").replace("+", " and ")
    s = _SUFFIX.sub(" ", s)
    s = "".join(c for c in s if not unicodedata.combining(c))   # fold accents
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)            # keep letters
    return re.sub(r"\s+", " ", s).strip()


def key(title, artist):
    return "%s|%s" % (norm(title), norm(artist))


def clean(title):
    """Strip the video-platform noise YouTube Music playlist titles carry.

    Mood playlists come back with titles like `logical (Official Lyric Video)`.
    The parenthetical is not part of the song and reading fifty of them wastes
    the model's attention, so it goes before the prompt is built. The videoId
    is what actually identifies the track, and that is untouched.
    """
    t = re.sub(r"\s*[\(\[][^\)\]]*"
               r"(official|video|audio|visuali[sz]er|lyric|hd|4k|explicit)"
               r"[^\)\]]*[\)\]]", "", title or "", flags=re.I)
    return re.sub(r"\s+", " ", t).strip() or (title or "")


def seconds(length):
    """`"5:35"` -> 335. Sources that give no duration get 0 and the caller
    substitutes an average; guessing here would hide the missing data."""
    if not length:
        return 0
    parts = str(length).split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    out = 0
    for p in parts:
        out = out * 60 + p
    return out


def hhmm(secs):
    m, s = divmod(int(secs), 60)
    return "%d:%02d" % (m, s)


# ---------------------------------------------------------------- the model

def _extract(text):
    """The CLI returns prose-free JSON when asked, but tolerate a code fence."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    start = min([i for i in (t.find("{"), t.find("[")) if i != -1] or [0])
    return json.loads(t[start:])


def _via_cli(prompt, schema, retries=2):
    """Use the logged-in Claude Code CLI. No separate API key, no separate bill.

    The CLI has no server-side schema enforcement, so the schema is stated in
    the prompt and the reply is parsed here; a parse failure is retried once
    with the error fed back."""
    ask = (prompt + "\n\nReturn ONLY a JSON object matching this schema. No "
           "markdown fence, no commentary, no explanation before or after.\n"
           + json.dumps(schema))
    # .env carries an ANTHROPIC_API_KEY for the `api` backend, and its mere
    # presence makes the CLI refuse to use the claude.ai login it would
    # otherwise prefer. Hide it from the child so choosing the CLI backend
    # actually gets the CLI backend, and the key stays inert until asked for.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    for attempt in range(retries):
        p = subprocess.run(
            ["claude", "-p", "--model", CLI_MODEL, "--output-format", "text"],
            input=ask, capture_output=True, text=True, timeout=900, env=env)
        if p.returncode != 0:
            raise RuntimeError("claude CLI failed: %s" % p.stderr.strip()[:300])
        try:
            return _extract(p.stdout)
        except (ValueError, json.JSONDecodeError) as e:
            if attempt == retries - 1:
                raise RuntimeError("model did not return usable JSON: %s\n%s"
                                   % (e, p.stdout[:400]))
            ask += "\n\nYour last reply could not be parsed as JSON (%s). " \
                   "Return only the raw JSON object." % e


# Keywords the API's json_schema enforcement rejects outright, mapped to how
# the same constraint reads as an instruction. Measured, not guessed: `enum`,
# `minLength`, `minItems`, nested objects and optional properties all pass;
# these three return a 400.
_UNENFORCEABLE = {"minimum": "at least %s", "maximum": "at most %s",
                  "maxItems": "at most %s items"}


def _relax(schema, path="", notes=None):
    """Strip the keywords the API refuses, restating each as prose.

    Server-side enforcement is stricter than the CLI's parse-and-hope, which is
    the reason to use it — but it supports a subset of JSON Schema, and a range
    it will not enforce is still a constraint the model should honour. Dropping
    the keyword silently would quietly widen every bound in the file; dropping
    it into the prompt keeps it, just advisory instead of guaranteed.
    """
    notes = [] if notes is None else notes
    if isinstance(schema, list):
        return [_relax(s, path, notes) for s in schema], notes
    if not isinstance(schema, dict):
        return schema, notes
    out = {}
    for k, v in schema.items():
        if k in _UNENFORCEABLE:
            notes.append("%s: %s" % (path or "value",
                                     _UNENFORCEABLE[k] % (v,)))
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _relax(sub, "%s.%s" % (path, name) if path
                                   else name, notes)[0]
                      for name, sub in v.items()}
        elif k in ("items", "additionalProperties") or isinstance(v, dict):
            out[k] = _relax(v, path, notes)[0]
        else:
            out[k] = v
    return out, notes


def _via_api(prompt, schema, max_tokens, retries=3):
    key_ = os.environ["ANTHROPIC_API_KEY"]
    schema, notes = _relax(schema)
    if notes:
        prompt += ("\n\nThese bounds are not machine-enforced. Respect them "
                   "anyway:\n  " + "\n  ".join(notes))
    for attempt in range(retries):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key_, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": API_MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}],
                  "output_config": {"format": {"type": "json_schema",
                                               "schema": schema}}},
            timeout=600)
        if r.status_code in (429, 500, 502, 503, 529) and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
            continue
        if not r.ok:
            sys.exit("API %d: %s" % (r.status_code, r.text[:500]))
        body = r.json()
        if body.get("stop_reason") == "refusal":
            sys.exit("Model declined: %s" % body.get("stop_details"))
        if body.get("stop_reason") == "max_tokens":
            sys.exit("Hit max_tokens — shrink the candidate list and retry.")
        return json.loads("".join(b["text"] for b in body["content"]
                                  if b["type"] == "text"))


def llm(prompt, schema, max_tokens=8000):
    """Prefer the Claude Code CLI you are already logged into. ANTHROPIC_API_KEY
    is only used if you set ELO_BACKEND=api deliberately — it gets schema
    enforcement server-side, which is stricter, but it bills separately."""
    if os.environ.get("ELO_BACKEND") == "api" and os.environ.get(
            "ANTHROPIC_API_KEY"):
        return _via_api(prompt, schema, max_tokens)
    if shutil.which("claude"):
        return _via_cli(prompt, schema)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _via_api(prompt, schema, max_tokens)
    sys.exit("No backend: install the Claude Code CLI, or set ANTHROPIC_API_KEY.")
