"""Shared plumbing: database, schema, and the one LLM call everything uses."""
import json
import os
import re
import sqlite3
import sys
import unicodedata
import time

import shutil
import subprocess

import requests

def _load_env():
    """Read .env beside this file so keys never land in the shell history."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()

CLI_MODEL = os.environ.get("ELO_MODEL", "sonnet")
API_MODEL = os.environ.get("ELO_API_MODEL", "claude-sonnet-5")
DB = os.environ.get("ELO_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "elo.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id       INTEGER PRIMARY KEY,
    title    TEXT NOT NULL,
    artist   TEXT NOT NULL DEFAULT '',
    album    TEXT NOT NULL DEFAULT '',
    genre    TEXT NOT NULL DEFAULT '',
    year     TEXT NOT NULL DEFAULT '',
    seconds  INTEGER NOT NULL DEFAULT 0,
    external INTEGER NOT NULL DEFAULT 0,   -- 1 = a seed we looked up, not owned
    UNIQUE (title, artist, album)
);
CREATE TABLE IF NOT EXISTS lyrics (
    track_id INTEGER PRIMARY KEY,
    source   TEXT NOT NULL,                -- lrclib | genius | none
    text     TEXT NOT NULL DEFAULT '',
    chars    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS moods (
    track_id   INTEGER PRIMARY KEY,
    themes     TEXT NOT NULL DEFAULT '[]', -- JSON array, controlled vocabulary
    stance     TEXT NOT NULL DEFAULT '',
    valence    REAL,                       -- 0 desolate .. 10 elated
    energy     REAL,                       -- 0 still .. 10 frantic
    summary    TEXT NOT NULL DEFAULT '',
    basis      TEXT NOT NULL DEFAULT '',   -- lyrics | metadata
    confidence TEXT NOT NULL DEFAULT ''    -- known | guessed
);
CREATE INDEX IF NOT EXISTS moods_ve ON moods (valence, energy);
"""


def connect(create=False):
    if not create and not os.path.exists(DB):
        sys.exit("No library at %s — run: python ingest.py ~/Desktop/library.txt"
                 % DB)
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    for col, decl in (("seconds", "INTEGER NOT NULL DEFAULT 0"),
                      ("external", "INTEGER NOT NULL DEFAULT 0")):
        cols = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
        if col not in cols:
            con.execute("ALTER TABLE tracks ADD COLUMN %s %s" % (col, decl))
    con.commit()
    return con


_SUFFIX = re.compile(
    r"\b(remaster(ed)?|remix|live|deluxe|edition|version|mono|stereo|"
    r"radio edit|extended|acoustic|instrumental|bonus track|explicit)\b")


def norm(s):
    """Aggressive normalisation for cross-source title/artist matching."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)          # (feat. X), [Remix]
    s = re.sub(r"\b(feat|ft|featuring|with)\b.*", " ", s)
    s = s.replace("&", " and ").replace("+", " and ")
    s = _SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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
    for attempt in range(retries):
        p = subprocess.run(
            ["claude", "-p", "--model", CLI_MODEL, "--output-format", "text"],
            input=ask, capture_output=True, text=True, timeout=900)
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


def _via_api(prompt, schema, max_tokens, retries=3):
    key = os.environ["ANTHROPIC_API_KEY"]
    for attempt in range(retries):
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
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
            sys.exit("Hit max_tokens — lower the batch size and retry.")
        return json.loads("".join(b["text"] for b in body["content"]
                                  if b["type"] == "text"))


def llm(prompt, schema, max_tokens=16000):
    """Prefer the Claude Code CLI you are already logged into. ANTHROPIC_API_KEY
    is only used if you set it deliberately — it gets schema enforcement
    server-side, which is stricter, but it bills separately."""
    if os.environ.get("ELO_BACKEND") == "api" and os.environ.get(
            "ANTHROPIC_API_KEY"):
        return _via_api(prompt, schema, max_tokens)
    if shutil.which("claude"):
        return _via_cli(prompt, schema)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _via_api(prompt, schema, max_tokens)
    sys.exit("No backend: install the Claude Code CLI, or set ANTHROPIC_API_KEY.")
