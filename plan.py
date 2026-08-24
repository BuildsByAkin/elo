"""Plain English in, a segmented plan out. The first of the two model calls.

    "i'm in the mood of tell your friends by the weeknd"
    "take me from sad to happy, 30 minutes"
    "wake me up on my drive to work, 20 minutes, afrobeats"

A plan is a list of segments. Sustaining one mood is one segment; a journey is
three or four, and dividing the minutes between them is the whole trick. The
old design tried to move the listener smoothly by scoring every track's
emotional coordinates and walking a line through them, which required knowing
the mood of all the music in the world. Segments get the same result from a
much cheaper premise: cut the time into blocks, name the mood of each block,
and let each block be filled from a pool that is already the right mood
because somebody at YouTube Music curated it that way.

The moods are not invented here. They are read live from
`get_mood_categories`, so the plan can only ask for a mood that has real
playlists behind it — a segment naming a mood the catalogue does not have is a
segment nothing can fill.
"""
import json
import sys

import common
import sources

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["minutes", "mode", "segments", "reason"],
    "properties": {
        "minutes": {"type": "integer", "minimum": 5, "maximum": 240},
        "mode": {"type": "string", "enum": ["sustain", "shift"]},
        "title": {"type": "string"},
        "seed_title": {"type": "string"},
        "seed_artist": {"type": "string"},
        "segments": {
            "type": "array", "minItems": 1, "maxItems": 5,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["label", "mood", "minutes", "note"],
                "properties": {
                    "label": {"type": "string"},
                    "mood": {"type": "string"},
                    "tags": {"type": "array", "maxItems": 4,
                             "items": {"type": "string"}},
                    "genres": {"type": "array", "maxItems": 3,
                               "items": {"type": "string"}},
                    "minutes": {"type": "integer", "minimum": 3,
                                "maximum": 240},
                    "note": {"type": "string"}}}},
        "reason": {"type": "string"},
    }}

RUBRIC = """Turn this listening request into a segmented playlist plan.

minutes   total length. If they gave none, use 45. A commute is ~25, a gym
          session ~45, "a few songs" ~15.
mode      `sustain` if they want to STAY somewhere — "songs like this", "keep
          me in this mood", "breakup songs". `shift` if they want to MOVE —
          "sad to happy", "start slow then pump me up", "wake me up". A
          destination different from the starting point is a shift even when
          they never use the word.
seed_title / seed_artist
          only if they named a specific song to build around. Omit both
          otherwise. If they named an artist but no song, omit these and put
          the artist in the first segment's genres.
title     a short, human playlist name. No dates, no "elo", no quotes.

segments  the blocks the time is cut into.
          - `sustain` is ONE segment covering all the minutes.
          - `shift` is 2-4 segments that get from the start to the destination.
            Do not jump. A sad-to-happy shift wants a middle: sad, then
            something lifting, then happy. Give the transitional middle real
            time — a shift that spends 90% of itself at one end and lurches at
            the last song is worse than an even split. Weight the ends
            slightly if the request implies it ("start slow" = a shorter
            opening).
          - minutes across all segments must sum to the total.

  label   two or three words naming this block in the listener's terms
          ("still wallowing", "picking up", "full lift").
  mood    ONE value copied EXACTLY from the POOLS list below. This selects the
          curated pool the block is filled from, so it must match a real entry
          character for character. The list is coarse on purpose; pick the
          closest thing to it.
          The list holds both feelings (Sad, Chill, Party) and genres (Hip-hop,
          Jazz). For a `shift`, every block should name a FEELING — the blocks
          differ by where the listener is, and naming the same genre three
          times gives all three blocks the same pool and defeats the point. For
          a `sustain` led by a genre rather than a feeling, naming the genre is
          the better pool.
  tags    up to 4 free-text Last.fm tags that sharpen the block beyond the
          coarse mood — genre words, scene words, era words ("shoegaze",
          "boom bap", "80s synthpop", "afrobeats"). Not mood words: the mood
          field already carries that and Last.fm's tags are bad at emotion.
  genres  genre words the user actually said, lowercased. Empty if they said
          none. These bias which curated playlists get opened, so only put
          words here that the user's request supports.
  note    one sentence on what this block should feel like and how it should
          hand over to the next. This is the instruction the second model call
          follows when it orders the block, so write it for that reader.

reason    one short sentence explaining your reading of the request, so a
          misread is visible instead of silent.

Judge the feeling they are ASKING FOR, not the events they describe. "I just
got dumped and want to wallow" is a request to stay low, even though being
dumped is high-arousal. "Breakup songs that make me feel strong" is a shift,
not a wallow.
"""


def parse(text):
    moods = sources.mood_names()
    if not moods:
        sys.exit("could not read YouTube Music's mood categories — the "
                 "InnerTube endpoint may have changed shape")
    out = common.llm(
        RUBRIC + "\nPOOLS (copy one exactly):\n  " + "\n  ".join(moods) +
        "\n\nREQUEST: " + text, SCHEMA, max_tokens=3000)
    return _tidy(out, moods)


def _tidy(plan, moods):
    """Repair the two things the model gets wrong often enough to matter:
    segment minutes that do not sum to the total, and a mood spelled close to
    a real one but not exactly."""
    segs = plan.get("segments") or []
    if not segs:
        sys.exit("the plan came back with no segments")
    lookup = {common.norm(m): m for m in moods}
    for s in segs:
        m = lookup.get(common.norm(s.get("mood", "")))
        if not m:                                # nearest containing match
            want = common.norm(s.get("mood", ""))
            m = next((v for k, v in lookup.items()
                      if want and (want in k or k in want)), moods[0])
        s["mood"] = m
        s["tags"] = [t for t in (s.get("tags") or []) if t.strip()]
        s["genres"] = [g.lower().strip() for g in (s.get("genres") or [])
                       if g.strip()]

    total = int(plan.get("minutes") or 45)
    got = sum(int(s.get("minutes") or 0) for s in segs)
    if got != total and got > 0:
        scale = total / float(got)
        for s in segs:
            s["minutes"] = max(3, int(round(s["minutes"] * scale)))
        drift = total - sum(s["minutes"] for s in segs)
        segs[-1]["minutes"] = max(3, segs[-1]["minutes"] + drift)
    plan["minutes"] = sum(s["minutes"] for s in segs)
    if plan.get("mode") == "sustain" and len(segs) > 1:
        plan["mode"] = "shift"               # trust the segments over the label
    return plan


def describe(plan):
    head = "%s  %d min" % (plan.get("mode", "?"), plan["minutes"])
    if plan.get("seed_title"):
        head += "   seed: %s — %s" % (plan["seed_title"],
                                      plan.get("seed_artist") or "?")
    lines = [head]
    for i, s in enumerate(plan["segments"], 1):
        bits = [s["mood"]]
        if s.get("genres"):
            bits.append("/".join(s["genres"]))
        if s.get("tags"):
            bits.append("#" + " #".join(s["tags"]))
        lines.append("  %d. %-18s %2d min  [%s]" % (i, s["label"],
                                                    s["minutes"],
                                                    "  ".join(bits)))
        lines.append("     %s" % s.get("note", ""))
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: python plan.py "take me from sad to happy, 30 min"')
    plan = parse(" ".join(sys.argv[1:]))
    print(describe(plan))
    print("\n  %s\n" % plan.get("reason", ""))
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
