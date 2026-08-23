"""Natural language in, a structured request out.

    "I just got dumped and I want to wallow"
    "pump me up for the gym but start slow"
    "wake me up on my drive to work, 20 minutes, afrobeats"

The point of this module is that the user should never have to know the mood
anchors, the theme vocabulary, or the difference between sustain and shift. They
describe a situation; this turns it into the request the engine already takes.

One LLM call. The model is given the closed vocabularies verbatim so the output
lands in the same space the cards were written in — an intent that asks for a
theme the tagger never assigns is an intent that matches nothing.
"""
import json
import sys

import common
import tag as T

# The named anchors, restated for the model so its coordinates are calibrated
# against the same points `elo.py moods` reports. Kept in sync by importing
# rather than retyping would be nicer, but elo.py imports this module.
ANCHORS = """
heartbroken v1.0 e3.0   numb       v3.0 e1.5   content  v7.0 e4.0
sad         v2.0 e2.5   reflective v4.0 e2.5   happy    v8.0 e6.0
angry       v2.5 e8.5   calm       v5.5 e1.5   hyped    v8.5 e9.0
focus       v5.0 e4.0   romantic   v6.5 e3.5   chill    v6.0 e3.0
"""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["mode", "start", "end", "minutes", "themes", "genres",
                 "reason"],
    "properties": {
        "mode": {"type": "string", "enum": ["sustain", "shift"]},
        "start": {
            "type": "object", "additionalProperties": False,
            "required": ["valence", "energy"],
            "properties": {
                "valence": {"type": "number", "minimum": 0, "maximum": 10},
                "energy": {"type": "number", "minimum": 0, "maximum": 10}}},
        "end": {
            "type": "object", "additionalProperties": False,
            "required": ["valence", "energy"],
            "properties": {
                "valence": {"type": "number", "minimum": 0, "maximum": 10},
                "energy": {"type": "number", "minimum": 0, "maximum": 10}}},
        "minutes": {"type": "integer", "minimum": 5, "maximum": 240},
        "themes": {"type": "array", "maxItems": 3,
                   "items": {"type": "string", "enum": T.THEMES}},
        "stance": {"type": "string", "enum": T.STANCES},
        "genres": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "seed_title": {"type": "string"},
        "seed_artist": {"type": "string"},
        "reason": {"type": "string"},
    }}

RUBRIC = """Turn this listening request into a structured playlist spec.

mode      `sustain` if they want to STAY somewhere — "breakup songs", "keep me
          in this mood", "songs to wallow to". `shift` if they want to MOVE —
          "sad to strong", "start slow then pump me up", "wake me up".
          If they describe a destination different from where they are, it is a
          shift, even when they do not use the word.
start     where the playlist begins, in valence (0 desolate .. 10 elated) and
          energy (0 still .. 10 frantic). For sustain this is where they are
          and want to remain.
end       where it finishes. For `sustain` set this EQUAL to start.
minutes   how long. If they say nothing, use 45. A commute is ~25, a gym
          session ~45, "a few songs" ~15.
themes    up to 3 from the closed list, describing the SUBJECT they asked for.
          Leave empty if they only described a feeling and no subject — do not
          invent a subject from a mood.
stance    optional, from the closed list — the posture they want toward that
          subject. "wallow" is devastated; "breakup songs that make me feel
          strong" is defiant. This is often the most important field: it is
          what separates a wallow from a revenge playlist.
genres    genre words they used, lowercased, as free text ("r&b", "afrobeats",
          "country"). Empty if they named none. Do NOT put moods here.
seed_title / seed_artist
          only if they named a specific song to build around ("songs like Tell
          Your Friends"). Omit both otherwise.
reason    one short sentence explaining your reading, so a wrong parse is
          visible rather than silent.

Judge the emotional colour they are ASKING FOR, not the events they describe.
"I just got dumped and want to wallow" is a request for low valence and low
energy, even though being dumped is high-arousal.

Reference anchors, so your numbers are calibrated:
""" + ANCHORS


def parse(text):
    out = common.llm(
        RUBRIC + "\nTHEMES: " + ", ".join(T.THEMES) +
        "\nSTANCES: " + ", ".join(T.STANCES) +
        "\n\nREQUEST: " + text, SCHEMA, max_tokens=2000)
    # sustain means one point, whatever the model returned for `end`.
    if out.get("mode") == "sustain":
        out["end"] = dict(out["start"])
    return out


def describe(spec):
    a, b = spec["start"], spec["end"]
    line = "%s  v%.1f e%.1f" % (spec["mode"], a["valence"], a["energy"])
    if spec["mode"] == "shift":
        line += "  ->  v%.1f e%.1f" % (b["valence"], b["energy"])
    bits = ["%d min" % spec["minutes"]]
    if spec.get("themes"):
        bits.append("/".join(spec["themes"]))
    if spec.get("stance"):
        bits.append(spec["stance"])
    if spec.get("genres"):
        bits.append(", ".join(spec["genres"]))
    if spec.get("seed_title"):
        bits.append("seed: %s — %s" % (spec["seed_title"],
                                       spec.get("seed_artist") or "?"))
    return line + "   [" + " | ".join(bits) + "]"


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: python intent.py "pump me up for the gym but start slow"')
    spec = parse(" ".join(sys.argv[1:]))
    print(describe(spec))
    print("  %s" % spec.get("reason", ""))
    print()
    print(json.dumps(spec, indent=2))


if __name__ == "__main__":
    main()
