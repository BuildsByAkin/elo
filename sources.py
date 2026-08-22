"""Candidate generation: get plausible songs out of all music, not the library.

Nothing here judges mood. These are recall engines — they cast wide and cheap,
and the tagger downstream supplies precision. Every candidate lands in `tracks`
with external=1 unless it matches something you already own, in which case it
resolves to the owned row and picks up the ownership boost for free.

Sources, and what each is actually good for:

  lastfm_tag      human-applied mood tags at real scale. `betrayal` has 334
                  taggings here against 6 in MusicBrainz. The workhorse.
  lastfm_similar  co-listening neighbours of a seed track. Artist adjacency,
                  not mood — useful for reach, useless alone.
  model           the model's own recall. Strong on canon, weak on recent and
                  long-tail, so it is capped rather than trusted.
"""
import os
import sys
import time

import requests

import common
from tag import THEMES, STANCES

LASTFM = "https://ws.audioscrobbler.com/2.0/"
UA = {"User-Agent": "elo/0.1 (personal music research)"}
MODEL_CAP = 0.20          # at most this share of a pool may come from the model


def _fm(method, **params):
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        sys.exit("LASTFM_API_KEY is not set — put it in .env")
    p = {"method": method, "api_key": key, "format": "json"}
    p.update(params)
    for attempt in (0, 1, 2):
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


def _cand(title, artist, source, weight=1.0):
    return {"title": (title or "").strip(), "artist": (artist or "").strip(),
            "source": source, "weight": weight}


def lastfm_tag(tag, limit=200):
    """Top tracks carrying a mood tag. Last.fm's @attr totals are wrong --
    it reports 14 for `breakup` and then hands back 669 -- so trust len()."""
    j = _fm("tag.getTopTracks", tag=tag, limit=limit)
    out = []
    for t in (j.get("tracks", {}).get("track") or []):
        name = t.get("name")
        artist = (t.get("artist") or {}).get("name")
        if name and artist:
            out.append(_cand(name, artist, "lastfm:%s" % tag))
    return out


def lastfm_similar(title, artist, limit=100):
    j = _fm("track.getSimilar", track=title, artist=artist, limit=limit,
            autocorrect=1)
    out = []
    for t in (j.get("similartracks", {}).get("track") or []):
        name = t.get("name")
        who = (t.get("artist") or {}).get("name")
        if name and who:
            out.append(_cand(name, who, "lastfm:similar",
                             float(t.get("match") or 0) or 1.0))
    return out


def lastfm_track_tags(title, artist, top=8):
    """What humans call this specific track. Free mood signal for a seed."""
    j = _fm("track.getTopTags", track=title, artist=artist, autocorrect=1)
    tags = j.get("toptags", {}).get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    return [t["name"].lower() for t in tags[:top] if t.get("name")]


MODEL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["tags", "songs", "target"],
    "properties": {
        "tags": {"type": "array", "minItems": 2, "maxItems": 8,
                 "items": {"type": "string"},
                 "description": "lowercase Last.fm-style tags to search"},
        "songs": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "artist"],
            "properties": {"title": {"type": "string"},
                           "artist": {"type": "string"}}}},
        "target": {
            "type": "object", "additionalProperties": False,
            "required": ["themes", "stance", "valence", "energy"],
            "properties": {
                "themes": {"type": "array", "minItems": 1, "maxItems": 3,
                           "items": {"type": "string", "enum": THEMES}},
                "stance": {"type": "string", "enum": STANCES},
                "valence": {"type": "number", "minimum": 0, "maximum": 10},
                "energy": {"type": "number", "minimum": 0, "maximum": 10}}}}}


def expand(query, n_songs=40):
    """Turn a request into Last.fm tags to search plus the model's own picks.

    The tags matter more than the songs: they steer the wide, human-tagged
    sources. The songs are canon-heavy by nature and get capped downstream.
    """
    out = common.llm(
        'A person asked for music: "%s"\n\n'
        "Return two things.\n\n"
        "tags: 2-8 lowercase tags as they would actually be written on "
        "Last.fm by listeners — single words or short phrases like `breakup`, "
        "`heartbreak`, `melancholy`, `hype`. Prefer tags people really apply "
        "over precise ones nobody uses. Include the emotional stance, not just "
        "the subject.\n\n"
        "songs: up to %d real, existing songs that fit. Reach past the obvious "
        "canon — no `I Will Survive`, no `Someone Like You` unless nothing "
        "else fits. Recent and non-Western tracks are welcome.\n\n"
        "target: the mood card the ideal answer would have.\n"
        "THEMES: %s\nSTANCES: %s\n"
        "valence 0-10 desolate to elated; energy 0-10 still to frantic.\n"
        % (query, n_songs, ", ".join(THEMES), ", ".join(STANCES)),
        MODEL_SCHEMA)
    tags = [t.strip().lower() for t in out["tags"] if t.strip()]
    songs = [_cand(s["title"], s["artist"], "model") for s in out["songs"]]
    return tags, songs, out["target"]


def dedupe(pools):
    """Merge candidate lists, keeping the first sighting and noting agreement.

    A track surfaced by three independent sources is a better bet than one
    surfaced by one, so `hits` is carried forward into ranking.
    """
    seen = {}
    for pool in pools:
        for c in pool:
            k = (common.norm(c["title"]), common.norm(c["artist"]))
            if not k[0]:
                continue
            if k in seen:
                seen[k]["hits"] += 1
                seen[k]["sources"].add(c["source"].split(":")[0])
            else:
                c = dict(c, hits=1, sources={c["source"].split(":")[0]})
                seen[k] = c
    return list(seen.values())


def cap_model(cands, share=MODEL_CAP):
    """Keep the model from flooding the pool with canon."""
    model = [c for c in cands if c["source"] == "model" and c["hits"] == 1]
    rest = [c for c in cands if c not in model]
    allowed = int(len(cands) * share)
    return rest + model[:allowed]


def owned_index(con):
    idx = {}
    for tid, title, artist in con.execute(
            "SELECT id, title, artist FROM tracks WHERE external=0"):
        idx[(common.norm(title), common.norm(artist))] = tid
    return idx


def persist(con, cands):
    """Resolve each candidate to a track id, creating external rows as needed."""
    idx = owned_index(con)
    for c in cands:
        k = (common.norm(c["title"]), common.norm(c["artist"]))
        if k in idx:
            c["id"], c["owned"] = idx[k], True
            continue
        c["owned"] = False
        row = con.execute("SELECT id FROM tracks WHERE title=? AND artist=?"
                          " AND album=''", (c["title"], c["artist"])).fetchone()
        if not row:
            con.execute("INSERT INTO tracks (title, artist, album, external)"
                        " VALUES (?,?,'',1)", (c["title"], c["artist"]))
            row = con.execute("SELECT id FROM tracks WHERE title=? AND artist=?"
                              " AND album=''",
                              (c["title"], c["artist"])).fetchone()
        c["id"] = row[0]
    con.commit()
    return cands
