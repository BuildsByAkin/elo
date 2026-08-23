"""The selection engine: pure math over mood cards. No network, no database.

This is the part the brief calls the product, and it is deliberately the one
module with no I/O in it — every function here takes a list of cards and
returns a list of cards, so the whole thing is testable without a key, a
network, or a tagged library. See tests/test_engine.py.

Two modes:

  sustain — hold a posture. Rank by shared subject, then shared stance, then
            nearness in mood space.
  shift   — move between postures. Walk a line through mood space and pick a
            track for each step, under DJ constraints.

On the missing tempo constraint: the brief asks for "no tempo jumps > 15 BPM
between adjacent tracks". There is no BPM for this library — Spotify's audio
features are dead, AcousticBrainz is frozen at 2022, and Essentia needs audio
files we do not have (DESIGN.md §2.4). `max_energy_step` is the same constraint
expressed in the axis we can actually measure. It is a substitute, not the
real thing, and it is honest about that.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence, TypedDict

# Mood space is 0..10 on both axes (DESIGN.md §3 — kept from the existing
# schema rather than churned to the brief's -1..+1, which is the same space).
AXIS_MIN, AXIS_MAX = 0.0, 10.0
SPAN = math.hypot(AXIS_MAX - AXIS_MIN, AXIS_MAX - AXIS_MIN)
AVG_SECONDS = 210


class Card(TypedDict, total=False):
    """A scored track. Only the fields the engine actually reads are required."""
    id: int
    title: str
    artist: str
    genre: str
    seconds: int
    themes: list[str]
    stance: str
    valence: float
    energy: float
    confidence: str
    basis: str


Point = tuple[float, float]


@dataclass(frozen=True)
class Constraints:
    """DJ rules. Every one of these is a penalty, not a hard filter.

    A hard filter makes the path infeasible on a small library — 843 tracks
    cannot always supply a track that satisfies every rule at every step, and
    a shift that stops early is worse than one that bends a rule once and
    tells you. Penalties degrade; filters fail.
    """
    max_energy_step: float = 1.5   # per-track energy delta before it costs
    artist_window: int = 4         # no repeat artist within this many tracks
    genre_lock_frac: float = 0.35  # fraction of the path that prefers the
                                   # starting genre, so the ramp does not
                                   # change genre and mood at the same time
    avg_seconds: int = AVG_SECONDS

    w_artist: float = 6.0
    w_energy_jump: float = 2.0
    w_genre: float = 1.0
    w_backtrack: float = 1.5       # penalise moving away from the destination


@dataclass
class Step:
    """One position on the path, and what got picked for it."""
    index: int
    target: Point
    card: Card
    cost: float
    broke: list[str] = field(default_factory=list)


# --------------------------------------------------------------- primitives

def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_of(c: Card) -> Point:
    return (float(c["valence"]), float(c["energy"]))


def interpolate(a: Point, b: Point, steps: int) -> list[Point]:
    """`steps` evenly spaced points from a to b, inclusive of both ends."""
    if steps < 1:
        return []
    if steps == 1:
        return [a]
    return [(a[0] + (b[0] - a[0]) * i / (steps - 1),
             a[1] + (b[1] - a[1]) * i / (steps - 1)) for i in range(steps)]


def dedupe(cards: Sequence[Card], key) -> list[Card]:
    """Collapse different releases of the same recording.

    `Beanie` and `Beanie (Slowed)` are separate rows with separate ids, so
    deduping by id — which is all the selection loop does — happily serves both
    in one playlist. Found by shipping: the first playlist pushed to YouTube
    Music contained the same Chezile song at positions 2 and 8.

    Where two rows collapse, the shorter title wins, which reliably prefers the
    original over `(Slowed)`, `(Piano Version)` or `(Acoustic Version)`.
    """
    best: dict[tuple[str, str], Card] = {}
    order: list[tuple[str, str]] = []
    for c in cards:
        k = (key(c.get("title") or ""), key(c.get("artist") or ""))
        if not k[0]:
            continue
        if k not in best:
            best[k] = c
            order.append(k)
        elif len(c.get("title") or "") < len(best[k].get("title") or ""):
            best[k] = c
    return [best[k] for k in order]


def duration(cards: Iterable[Card], avg: int = AVG_SECONDS) -> int:
    return sum(int(c.get("seconds") or 0) or avg for c in cards)


def steps_for(minutes: int, avg: int = AVG_SECONDS) -> int:
    """How many tracks fill the requested minutes."""
    return max(2, round(minutes * 60 / avg))


# ------------------------------------------------------------------ sustain

def theme_overlap(a: Card, b: Card) -> float:
    """Jaccard over the closed theme vocabulary."""
    x, y = set(a.get("themes") or []), set(b.get("themes") or [])
    return len(x & y) / len(x | y) if (x | y) else 0.0


def sustain_score(seed: Card, c: Card,
                  w_theme: float = 6.0,
                  w_stance: float = 1.5,
                  w_distance: float = 4.0,
                  w_owned: float = 0.0) -> float:
    """Shared subject dominates; stance breaks ties; mood-space nearness sorts
    the rest.

    The weights are reasoned, not tuned (DESIGN.md §6). Exposed as arguments
    precisely so they can be tuned once there is a test set to tune against.

    `w_owned` is the ownership preference. It defaults to 0.0 because owning a
    song must never be required to be selected — a new user owns nothing and
    must still get a good playlist. Turn it up to break close calls toward the
    listener's own music.
    """
    return (w_theme * theme_overlap(seed, c)
            + (w_stance if seed.get("stance") == c.get("stance") else 0.0)
            - w_distance * distance(point_of(seed), point_of(c)) / SPAN
            + (w_owned if c.get("owned") else 0.0))


def sustain(seed: Card, pool: Sequence[Card], n: int = 10,
            **weights: float) -> list[Card]:
    ranked = sorted((c for c in pool if c.get("id") != seed.get("id")),
                    key=lambda c: -sustain_score(seed, c, **weights))
    return list(ranked[:n])


# -------------------------------------------------------------------- shift

def _penalties(c: Card, target: Point, chosen: list[Card], i: int,
               total: int, anchor_genre: str,
               k: Constraints) -> tuple[float, list[str]]:
    cost, broke = 0.0, []

    # Graded by recency, not flat. A flat penalty over the window is useless
    # once every candidate has appeared somewhere in it — everything ties and
    # the tie breaks on list order, which is how you get A B A A B A A B. The
    # artist you just played must cost strictly more than one three tracks ago.
    me = (c.get("artist") or "").lower()
    if me:
        recent = chosen[-k.artist_window:]
        for back, prev in enumerate(reversed(recent), start=1):
            if (prev.get("artist") or "").lower() == me:
                cost += k.w_artist * (k.artist_window - back + 1) / k.artist_window
                broke.append("artist repeat (%d back)" % back)
                break

    if chosen:
        jump = abs(float(c["energy"]) - float(chosen[-1]["energy"]))
        if jump > k.max_energy_step:
            cost += k.w_energy_jump * (jump - k.max_energy_step)
            broke.append("energy jump %.1f" % jump)

    if anchor_genre and i < total * k.genre_lock_frac:
        if (c.get("genre") or "").lower() != anchor_genre.lower():
            cost += k.w_genre
            broke.append("genre change early")

    return cost, broke


def shift(pool: Sequence[Card], start: Point, end: Point, minutes: int,
          k: Constraints | None = None) -> list[Step]:
    """Walk from `start` to `end`, filling roughly `minutes` of music.

    Greedy with lookahead-free penalties: at each point on the line, take the
    cheapest untaken track, where cost is distance to the point plus whatever
    DJ rules that pick would break. Greedy is the right call here — the pool is
    small, the path is monotonic, and an optimal solver would spend its budget
    proving something inaudible.
    """
    k = k or Constraints()
    if not pool:
        return []

    budget = minutes * 60
    targets = interpolate(start, end, steps_for(minutes, k.avg_seconds))
    goal = end

    chosen: list[Card] = []
    out: list[Step] = []
    taken: set[int] = set()
    total = 0
    anchor_genre = ""

    for i, target in enumerate(targets):
        left = [c for c in pool if c.get("id") not in taken]
        if not left:
            break

        def cost_of(c: Card) -> float:
            base = distance(point_of(c), target)
            pen, _ = _penalties(c, target, chosen, i, len(targets),
                                anchor_genre, k)
            # Discourage a pick that sits further from the destination than the
            # point we are aiming at — that is a step backwards along the ramp.
            back = max(0.0, distance(point_of(c), goal) - distance(target, goal))
            return base + pen + k.w_backtrack * back / SPAN

        pick = min(left, key=cost_of)
        _, broke = _penalties(pick, target, chosen, i, len(targets),
                              anchor_genre, k)
        out.append(Step(index=i, target=target, card=pick,
                        cost=cost_of(pick), broke=broke))

        chosen.append(pick)
        taken.add(int(pick["id"]))
        if i == 0:
            anchor_genre = pick.get("genre") or ""
        total += int(pick.get("seconds") or 0) or k.avg_seconds
        if total >= budget:
            break

    return out


def ramp_report(steps: Sequence[Step]) -> dict[str, float]:
    """Is the ramp actually monotonic, and how big are the jumps? The brief
    asks to eyeball this; this makes it a number."""
    if len(steps) < 2:
        return {"tracks": len(steps), "monotonic_frac": 1.0, "max_jump": 0.0}
    es = [float(s.card["energy"]) for s in steps]
    deltas = [b - a for a, b in zip(es, es[1:])]
    forward = sum(1 for d in deltas if d >= 0)
    # Direction of travel: if the arc descends, monotonic means non-increasing.
    if es[-1] < es[0]:
        forward = sum(1 for d in deltas if d <= 0)
    return {
        "tracks": float(len(steps)),
        "monotonic_frac": forward / len(deltas),
        "max_jump": max(abs(d) for d in deltas),
        "mean_jump": sum(abs(d) for d in deltas) / len(deltas),
    }
