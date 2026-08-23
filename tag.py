#!/usr/bin/env python3
"""Write a mood card for every track: what it is about, and where it sits.

A card is three things — 1-3 themes from a fixed vocabulary, an emotional
stance, and a point in valence/energy space. The vocabulary is closed on
purpose: free-text themes drift ("breakup" / "heartbreak" / "the end of a
relationship") and stop being filterable.

Lyrics are attached where we have them. Where we do not, the model works from
title and artist and is told to say so, because the probe established it can
only actually read about a quarter of this library from metadata alone.

    python tag.py                # tag everything untagged
    python tag.py --limit 40     # a slice, to eyeball the output first
    python tag.py --retag        # rebuild every card
"""
import argparse
import json
import sys

import common
import lyrics as L

BATCH = 20
LYRIC_CAP = 1600

THEMES = [
    "breakup", "heartbreak", "unrequited-love", "new-love", "devotion",
    "desire", "betrayal", "jealousy", "grief", "loneliness", "nostalgia",
    "depression", "anxiety", "anger", "defiance", "self-worth", "healing",
    "hope", "party", "dancing", "wealth", "hustle", "street-life", "violence",
    "faith", "gratitude", "protest", "friendship", "family", "hometown",
    "escape", "intoxication", "humor", "searching", "growing-up", "death",
]
STANCES = [
    "devastated", "defiant", "resigned", "hopeful", "angry", "tender", "numb",
    "euphoric", "anxious", "reflective", "playful", "triumphant",
]

CARD = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "themes", "stance", "valence", "energy", "summary",
                 "confidence"],
    "properties": {
        "id": {"type": "integer"},
        "themes": {"type": "array", "minItems": 1, "maxItems": 3,
                   "items": {"type": "string", "enum": THEMES}},
        "stance": {"type": "string", "enum": STANCES},
        "valence": {"type": "number", "minimum": 0, "maximum": 10},
        "energy": {"type": "number", "minimum": 0, "maximum": 10},
        "summary": {"type": "string"},
        "confidence": {"type": "string", "enum": ["known", "guessed"]},
    }}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cards"],
          "properties": {"cards": {"type": "array", "items": CARD}}}

RUBRIC = """For each track write a mood card.

themes     1-3 from the given list. Pick what the song is ABOUT, not its genre.
           Be discriminating: breakup, grief, loneliness and betrayal are four
           different things and must not be used interchangeably.
stance     how the song holds that subject. A breakup song can be `devastated`,
           `defiant` or `resigned` and those belong on different playlists.
valence    0-10. 0 is desolate, 5 is neutral or mixed, 10 is elated. Judge the
           emotional colour of the song as a listener experiences it, not
           whether the events described are happy.
energy     0-10. 0 is still and sparse, 5 is a steady mid-tempo, 10 is frantic.
           This is intensity and drive, not volume or tempo alone.
summary    one short sentence on what the song is doing. If lyrics are given,
           quote a fragment of three to eight words from them, in quotes. If no
           lyrics are given, do not invent a quote.
confidence `known` ONLY if you are reasoning from lyrics given below or from
           genuine knowledge of this specific recording. `guessed` if you are
           inferring from the title or artist name. Be strict: a guessed card is
           useful, a wrong card claimed as known is not.

Return exactly one card per track, keyed by the leading id. Omit nothing."""


def load(con, retag, limit):
    q = ("SELECT t.id, t.title, t.artist, t.album, t.genre, t.year"
         " FROM tracks t WHERE t.external=0")
    if not retag:
        q += " AND t.id NOT IN (SELECT track_id FROM moods)"
    q += " ORDER BY t.id"
    if limit:
        q += " LIMIT %d" % limit
    cols = ("id", "title", "artist", "album", "genre", "year")
    return [dict(zip(cols, r)) for r in con.execute(q)]


def block(t, lyric):
    head = "%d. %s — %s%s" % (t["id"], t["title"],
                              t["artist"] or "(unknown artist)",
                              " [%s]" % t["album"] if t["album"] else "")
    if t["genre"] or t["year"]:
        head += "  (%s)" % ", ".join(x for x in (t["genre"], t["year"]) if x)
    return head + ("\nLYRICS:\n" + lyric if lyric else "\n(no lyrics found)")


REQUIRED = ("id", "themes", "stance", "valence", "energy", "summary",
            "confidence")


def usable(c, by_id):
    """Is this card safe to store?

    The CLI backend has no server-side schema enforcement (common.py) — the
    schema is stated in the prompt and the model can quietly omit a field. One
    card missing `valence` should cost that one card, not the remaining 38
    batches, so validate here and drop what does not survive. Cards are
    re-requested on the next run.
    """
    if not isinstance(c, dict) or c.get("id") not in by_id:
        return False
    if any(k not in c for k in REQUIRED):
        return False
    try:
        c["valence"] = min(10.0, max(0.0, float(c["valence"])))
        c["energy"] = min(10.0, max(0.0, float(c["energy"])))
    except (TypeError, ValueError):
        return False
    themes = c.get("themes")
    if isinstance(themes, str):
        themes = [themes]
    themes = [t for t in (themes or []) if t in THEMES][:3]
    if not themes:
        return False
    c["themes"] = themes
    if c.get("stance") not in STANCES:
        return False
    if c.get("confidence") not in ("known", "guessed"):
        c["confidence"] = "guessed"
    c["summary"] = str(c.get("summary") or "")
    return True


def save(con, cards):
    con.executemany(
        "INSERT OR REPLACE INTO moods (track_id, themes, stance, valence,"
        " energy, summary, basis, confidence) VALUES (?,?,?,?,?,?,?,?)", cards)
    con.commit()


def run(con, retag=False, limit=0):
    tracks = load(con, retag, limit)
    if not tracks:
        print("everything is already tagged — use --retag to rebuild")
        return
    tag_tracks(con, tracks)
    summarise(con)


def tag_tracks(con, tracks):
    """Tag an explicit list of tracks, owned or not.

    Split out from run() because the corpus needs it: candidates arriving from
    co-listening are external=1 and load() deliberately does not see them, but
    they need exactly the same card. See DESIGN.md §4.
    """
    if not tracks:
        return 0
    lyr = L.load(con, [t["id"] for t in tracks])
    have = sum(1 for v in lyr.values() if v[0] != "none")
    print("tagging %d tracks in %d calls (%d have lyrics)"
          % (len(tracks), -(-len(tracks) // BATCH), have), file=sys.stderr)

    done, failed, missing = 0, [], 0
    for i in range(0, len(tracks), BATCH):
        batch = tracks[i:i + BATCH]
        texts = {t["id"]: (lyr.get(t["id"], ("none", ""))[1] or "")[:LYRIC_CAP]
                 for t in batch}
        # One bad batch must not cost the other 42. Cards are committed per
        # batch, and a re-run skips whatever already landed, so the cheapest
        # recovery is to note the failure and keep going.
        try:
            out = common.llm(
                RUBRIC + "\n\nTHEMES: " + ", ".join(THEMES) +
                "\nSTANCES: " + ", ".join(STANCES) + "\n\n" +
                "\n\n".join(block(t, texts[t["id"]]) for t in batch), SCHEMA)
        except Exception as e:
            failed.append((i // BATCH + 1, str(e)[:160]))
            print("  batch %d FAILED: %s" % (i // BATCH + 1, str(e)[:160]),
                  file=sys.stderr)
            continue
        by_id = {t["id"]: t for t in batch}
        cards = [c for c in out.get("cards", []) if usable(c, by_id)]
        save(con, [(c["id"], json.dumps(c["themes"]), c["stance"],
                    c["valence"], c["energy"], c["summary"],
                    "lyrics" if texts.get(c["id"]) else "metadata",
                    c["confidence"]) for c in cards])
        done += len(cards)
        # The rubric says one card per track; if the model drops one, say so
        # rather than letting the count quietly disagree with the library.
        gap = len(batch) - len({c["id"] for c in cards})
        missing += gap
        print("  %d/%d%s" % (min(i + BATCH, len(tracks)), len(tracks),
                             "   (%d track(s) got no card)" % gap if gap else ""),
              file=sys.stderr)

    print("wrote %d cards" % done, file=sys.stderr)
    if missing:
        print("%d track(s) came back without a card — re-run to fill them"
              % missing, file=sys.stderr)
    if failed:
        print("%d batch(es) failed:" % len(failed), file=sys.stderr)
        for n, err in failed:
            print("  batch %d: %s" % (n, err), file=sys.stderr)
        print("re-run `python tag.py` to retry only what is missing",
              file=sys.stderr)
    return done


def summarise(con):
    n, known, lyric = con.execute(
        "SELECT count(*), sum(confidence='known'), sum(basis='lyrics')"
        " FROM moods").fetchone()
    print("\n=== TAGGED ===")
    print("cards       %d" % n)
    print("from lyrics %d (%.0f%%)" % (lyric, 100.0 * lyric / n))
    print("known       %d (%.0f%%)" % (known, 100.0 * known / n))
    rows = con.execute("SELECT themes FROM moods").fetchall()
    counts = {}
    for (js,) in rows:
        for th in json.loads(js):
            counts[th] = counts.get(th, 0) + 1
    print("\ntop themes")
    for th, c in sorted(counts.items(), key=lambda x: -x[1])[:15]:
        print("  %4d  %s" % (c, th))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retag", action="store_true")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    con = common.connect()
    summarise(con) if a.stats else run(con, a.retag, a.limit)


if __name__ == "__main__":
    main()
