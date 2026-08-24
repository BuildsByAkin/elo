"""Fill each segment of a plan, then stitch the segments into a playlist.

The shape of this file is the whole argument of the redesign, so it is worth
stating plainly.

The obvious way to build a playlist from co-listening data is greedy: take the
seed, ask what people play next, pick one, ask again. It is also the wrong way.
Every pick is a separate round trip and a separate model call, so a thirty
minute playlist is ten sequential calls; worse, each pick only ever sees the
song before it, so the playlist drifts — ten hops of "reasonable next song"
ends up somewhere nobody asked for. Telephone, with a bill attached.

So nothing here picks a next song. It fills a segment:

  1. GATHER, in parallel, everything that could belong in each block —
     the seed's radio and Last.fm neighbours (fetched once, shared by every
     segment), each block's curated mood pool, each block's tags. Sixty-odd
     candidates per block, all at once.
  2. WEIGH, in code — reciprocal rank fusion across the sources, then the
     library multiplier. The shortlist is already personalised and already
     ordered before any model sees it.
  3. ORDER, one model call per block — here are the candidates, here is what
     this block is for, here is the track the previous block ended on, return
     an ordered set that fills the minutes and flows.

Three segments is three model calls, not ten, and they are the cheap kind:
judging a list, not writing one. Drift is gone for free, because the model
sees a whole block at once and the segment boundaries are the guardrails.

The model is a quality layer, not load-bearing. `--no-llm` takes the
code-ranked shortlist straight off the top and still produces a coherent
playlist, because the ranking already encodes co-listening frequency and your
own library. Losing the model costs taste, not function.
"""
import sys
from concurrent.futures import ThreadPoolExecutor

import common
import feedback as F
import sources
import taste as T

AVG_SECS = 210          # what an unknown-duration track is assumed to run
MIN_SLOT = 90           # a gap smaller than this is not worth another track
POOL = 60               # candidates shown to the model per segment
MAX_PER_ARTIST = 2
MAX_OWNED = 0.6         # at most this share of a block can be tracks you own

# How much each source counts, in the opening block and in later blocks. The
# seed decays because a journey's later blocks are supposed to leave it behind;
# `yours` and `library` do not, because your taste does not change between
# block one and block three.
W_OPEN = {"radio": 1.0, "similar": 0.9, "mood": 0.7, "tag": 0.5,
          "yours": 1.1, "library": 0.8}
W_LATER = {"radio": 0.35, "similar": 0.3, "mood": 1.0, "tag": 0.7,
           "yours": 1.1, "library": 0.8}


# ----------------------------------------------------------------- gathering

def _run(tasks, quiet):
    """Run `(label, callable)` pairs concurrently, keeping failures local.

    Every source here is blocking network IO against two different services,
    and one of them going quiet should cost a slot in the shortlist, not the
    request.
    """
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn): label for label, fn in tasks}
        for f, label in futs.items():
            try:
                out[label] = f.result() or []
            except Exception as e:
                if not quiet:
                    print("  %s failed: %s" % (label, str(e)[:100]),
                          file=sys.stderr)
                out[label] = []
    return out


def gather(plan, taste=None, wide=2, quiet=False):
    """Everything that could belong in any block, fetched concurrently.

    Returns `{segment_index: [(weight, candidates), ...]}`, ready for fusion.
    """
    segs = plan["segments"]
    seed_t = (plan.get("seed_title") or "").strip()
    seed_a = (plan.get("seed_artist") or "").strip()

    tasks = []
    # The library is free, cannot fail, and is the only source that knows
    # anything about this particular listener, so it is computed here rather
    # than fetched. Your playlists are NOT gathered here: that pool depends on
    # what has already been picked, so it is rebuilt per block in `build`.
    mine = {i: (T.library_pool(taste, s) if taste else [])
            for i, s in enumerate(segs)}
    if seed_t:
        tasks.append(("radio", lambda: sources.radio(seed_t, seed_a, 50)))
        tasks.append(("similar", lambda: sources.similar(seed_t, seed_a, 100)))
    for i, s in enumerate(segs):
        tasks.append(("mood/%d" % i,
                      lambda s=s: sources.mood_pool(s["mood"], s["genres"],
                                                    playlists=3, per=60)))
        for j, tag in enumerate(s.get("tags") or []):
            tasks.append(("tag/%d/%d" % (i, j),
                          lambda t=tag: sources.tag_top(t, 50)))
    if not quiet:
        print("gathering %d source%s across %d segment%s..."
              % (len(tasks), "s"[:len(tasks) != 1], len(segs),
                 "s"[:len(segs) != 1]), file=sys.stderr)
    got = _run(tasks, quiet)

    # A second, narrower ring: the radios of the strongest few tracks the first
    # pass surfaced. One seed's radio is fifty songs deep but only one song
    # wide, and a thirty minute journey needs more spread than that. Expanding
    # from what came back rather than from another guess keeps the widening
    # inside the neighbourhood the request actually described.
    hubs, seen = [], set()
    for label in ("radio", "mood/0"):
        for c in got.get(label, []):
            k = common.key(c["title"], c["artist"])
            if k not in seen and c["title"]:
                seen.add(k)
                hubs.append(c)
            if len(hubs) >= wide:
                break
        if len(hubs) >= wide:
            break
    if hubs:
        if not quiet:
            print("  widening from %s" % ", ".join(
                "%s" % h["title"][:26] for h in hubs), file=sys.stderr)
        got.update(_run([("wide/%d" % n,
                          lambda h=h: sources.radio(h["title"], h["artist"], 30))
                         for n, h in enumerate(hubs)], quiet))

    per_seg = {}
    for i, s in enumerate(segs):
        w = W_OPEN if i == 0 else W_LATER
        groups = []
        if got.get("radio"):
            groups.append((w["radio"], got["radio"]))
        if got.get("similar"):
            groups.append((w["similar"], got["similar"]))
        for n in range(len(hubs)):
            groups.append((w["radio"] * 0.5, got.get("wide/%d" % n, [])))
        groups.append((w["mood"], got.get("mood/%d" % i, [])))
        for j in range(len(s.get("tags") or [])):
            groups.append((w["tag"], got.get("tag/%d/%d" % (i, j), [])))
        if mine.get(i):
            groups.append((w["library"], mine[i]))
        per_seg[i] = groups
    if not quiet and any(mine.values()):
        print("  yours: %d owned tracks fit these segments"
              % sum(len(v) for v in mine.values()), file=sys.stderr)
    return per_seg


# ------------------------------------------------------------------ ordering

PICK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["picks"],
    "properties": {
        "picks": {
            "type": "array", "minItems": 1, "maxItems": 24,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["n"],
                "properties": {"n": {"type": "integer"},
                               "why": {"type": "string"}}}},
        "note": {"type": "string"}}}

PICK_RUBRIC = """You are filling one block of a playlist.

Below is a numbered candidate list. Every candidate is already known to be
plausible — they came from what people actually play around this music, and
they are already ordered by how strongly the sources and the listener's own
library back them. Your job is not to find good songs. It is to choose which
of these belong TOGETHER in this block, and in what order.

Return `picks` as an ordered list of candidate numbers, first track first.

  - Choose about %(want)d tracks. Order matters: pick %%1 is the first thing
    that plays in this block.
  - Fit the block's brief. A candidate that is a fine song but the wrong
    feeling for this block is a wrong answer.
  - Flow. Consecutive tracks should not collide in tempo, era or texture. The
    ordering is where you earn your place: the same set in a bad order is a
    worse playlist.
  - Do not pick the same artist more than twice, and never back to back.
  - The list is ordered by evidence, so prefer the top of it when two
    candidates serve the block equally. Reaching far down the list should buy
    something specific.
  - `why` is optional and at most eight words, for the listener to read.

%(handover)s
Only use numbers that appear in the list. Do not invent tracks.
"""


def _brief(seg, index, total, eras=""):
    lines = ["BLOCK %d of %d — %s" % (index + 1, total, seg["label"]),
             "  mood:    %s" % seg["mood"],
             "  length:  about %d minutes" % seg["minutes"]]
    if seg.get("genres"):
        lines.append("  genres:  %s" % ", ".join(seg["genres"]))
    if seg.get("tags"):
        lines.append("  tags:    %s" % ", ".join(seg["tags"]))
    if seg.get("note"):
        lines.append("  brief:   %s" % seg["note"])
    if eras:
        # Most candidates arrive with no release year, so this is where the
        # listener's era preference actually gets applied: the reader knows
        # roughly when records came out and the data does not.
        lines.append("  era:     this listener's library is %s. Prefer that "
                     "era when two candidates serve the block equally; do not "
                     "force it when the block calls for something else."
                     % eras)
    return "\n".join(lines)


def _render(cands):
    out = []
    for i, c in enumerate(cands, 1):
        dur = common.hhmm(c["secs"]) if c.get("secs") else "?:??"
        src = ",".join(sorted(c["sources"]))
        tail = ("  <- %s" % c["aff"]) if c.get("aff") else ""
        out.append("%3d. %s — %s  [%s]  %s%s"
                   % (i, c["title"][:52], c["artist"][:34], dur, src, tail))
    return "\n".join(out)


def _want(seg):
    """Roughly how many tracks fill this block, before real durations."""
    return max(2, int(round(seg["minutes"] * 60.0 / AVG_SECS)))


def _cap_owned(picked, taste, want, max_owned):
    """Hold the discovery ratio, preserving the model's ordering.

    Over-quota tracks you already own are skipped rather than the block being
    truncated, so what survives is still in the order the model chose. With no
    library this is a no-op.
    """
    if not taste.tracks or max_owned >= 1.0:
        return picked
    cap = max(1, int(round(want * max_owned)))
    out, spare, owned = [], [], 0
    for c in picked:
        if c["key"] in taste.tracks:
            if owned >= cap:
                spare.append(c)
                continue
            owned += 1
        out.append(c)
    # The cap is a preference for discovery, not a reason to hand back a short
    # block. If holding it would leave the block unable to fill its minutes —
    # which happens whenever the shortlist was mostly your own music, exactly
    # the case a big library produces — put the surplus back, in the order the
    # model chose. A block of your own songs beats a block of two songs.
    if len(out) < want and spare:
        keep = {id(c) for c in out} | {id(c) for c in spare[:want - len(out)]}
        out = [c for c in picked if id(c) in keep]
    return out


def order(seg, index, total, cands, tail, eras="", quiet=False):
    """One model call: choose and order this block from its shortlist."""
    want = _want(seg)
    shown = cands[:POOL]
    handover = ""
    if tail:
        handover = ("The previous block ended on %s — %s. Open this block so "
                    "that transition works; do not restate it.\n"
                    % (tail["title"], tail["artist"]))
    elif index == 0:
        handover = "This is the opening block. Pick %1 sets the tone.\n"

    prompt = (PICK_RUBRIC % {"want": want + 3, "handover": handover}
              + "\n" + _brief(seg, index, total, eras)
              + "\n\nCANDIDATES:\n" + _render(shown))
    out = common.llm(prompt, PICK_SCHEMA, max_tokens=4000)

    picked, seen = [], set()
    for p in out.get("picks") or []:
        n = p.get("n")
        if not isinstance(n, int) or not (1 <= n <= len(shown)) or n in seen:
            continue
        seen.add(n)
        c = dict(shown[n - 1])
        c["why"] = (p.get("why") or "").strip()
        picked.append(c)
    if not picked:
        if not quiet:
            print("  model returned nothing usable — falling back to rank",
                  file=sys.stderr)
        picked = [dict(c) for c in shown[:want]]
    return picked


def trim(picked, minutes, carry=0):
    """Cut the ordered block to its budget using real durations.

    Length is arithmetic, so the model is never asked to do it — it is asked
    for a few more tracks than fit and the surplus is dropped here. A block may
    overshoot by up to half a track rather than end conspicuously short, but
    the overshoot is charged to the next block via `carry`. Without that,
    three blocks each rounding up by a minute turn a thirty minute request into
    thirty-three, and the error only ever runs one way.
    """
    budget = minutes * 60 - carry
    out, total = [], 0
    for c in picked:
        if out and budget - total < MIN_SLOT:
            break                       # no room left worth filling
        secs = c.get("secs") or AVG_SECS
        if out and total + secs > budget + secs * 0.5:
            continue                    # too long for the gap; try the next
        out.append(c)
        total += secs
    return out, total, total - budget


# ------------------------------------------------------------------- stitch

def build(plan, taste=None, use_llm=True, wide=2,
          max_per_artist=MAX_PER_ARTIST, max_owned=MAX_OWNED, learned=None,
          quiet=False):
    """Run the whole pipeline. Returns `(tracks, blocks)`."""
    taste = T.load() if taste is None else taste
    learned = F.load() if learned is None else learned
    segs = plan["segments"]
    per_seg = gather(plan, taste, wide=wide, quiet=quiet)

    seed_artist = common.norm(plan.get("seed_artist") or "")
    # What the playlist is "about" so far, for co-occurrence. Starts as the
    # seed and grows with every block, so the question asked of your playlists
    # widens from "what goes with this song" to "what goes with these eight".
    chosen = set()
    if plan.get("seed_title"):
        chosen.add(common.key(plan["seed_title"],
                              plan.get("seed_artist") or ""))
    eras = T.era_hint(taste)
    if eras and not quiet:
        print("  era: your library is %s" % eras, file=sys.stderr)
    used, by_artist, tracks, blocks = set(), {}, [], []
    tail, carry = None, 0

    for i, seg in enumerate(segs):
        yours = T.playlist_pool(taste, chosen) if taste else []
        groups = per_seg[i] + ([((W_OPEN if i == 0 else W_LATER)["yours"],
                                 yours)] if yours else [])
        cands = sources.fuse(groups)
        T.boost(cands, taste, seg, chosen)
        # After the inferred weights, never before: what you said outright
        # overrules what we guessed, and it is the only thing here allowed to
        # remove a candidate rather than demote it.
        vetoed = F.apply(cands, learned, seg)
        if vetoed and not quiet:
            print("  dropped %d you rejected before: %s"
                  % (len(vetoed), ", ".join(c["title"][:24]
                                            for c in vetoed[:4])),
                  file=sys.stderr)

        # Deduplicate against everything already chosen, cap how often one
        # artist can come back, and cap how much of the block can be music you
        # already own. All three are code decisions applied before the prompt,
        # so the model never spends attention policing them and cannot quietly
        # break them.
        #
        # The owned cap is the one that is easy to get wrong in the other
        # direction. With the library as a source *and* a boost, a big library
        # will happily fill every block with tracks you already have, which is
        # not a playlist — it is shuffle with extra steps. Leaving room for
        # things you have never heard is the point of asking two strangers'
        # services what people play next.
        #
        # It is enforced on the picks rather than the shortlist: the model
        # should see plenty of your music and choose among it, and then we hold
        # the final ratio. Capping the shortlist instead would starve it of
        # choice and quietly bias which of your tracks it ever considers.
        fresh = []
        for c in cands:
            if c["key"] in used:
                continue
            cap = max_per_artist + (1 if seed_artist and
                                    seed_artist in common.norm(c["artist"])
                                    else 0)
            if by_artist.get(common.norm(c["artist"]), 0) >= cap:
                continue
            fresh.append(c)
        if not fresh:
            if not quiet:
                print("  segment %d had no candidates left" % (i + 1),
                      file=sys.stderr)
            continue

        if not quiet:
            mine = sum(1 for c in fresh[:POOL] if c["key"] in taste.tracks)
            print("\n%s\n  %d candidates (%d shown, %d yours, %d you file "
                  "with this playlist)"
                  % (_brief(seg, i, len(segs)), len(cands),
                     min(POOL, len(fresh)), mine, len(yours)),
                  file=sys.stderr)
        picked = (order(seg, i, len(segs), fresh, tail, eras, quiet)
                  if use_llm else [dict(c) for c in fresh])
        picked = _cap_owned(picked, taste, _want(seg), max_owned)
        picked, secs, carry = trim(picked, seg["minutes"], carry)

        for c in picked:
            used.add(c["key"])
            chosen.add(c["key"])
            a = common.norm(c["artist"])
            by_artist[a] = by_artist.get(a, 0) + 1
        tracks += picked
        blocks.append({"segment": seg, "tracks": picked, "seconds": secs})
        if picked:
            tail = picked[-1]
    return tracks, blocks


def show(blocks, out=sys.stdout, taste=None):
    """Numbered, because the numbers are what `elo.py no 3 5` points at."""
    total, pos = 0, 0
    for b in blocks:
        s = b["segment"]
        print("\n%s  (%s, %s)" % (s["label"], s["mood"],
                                  common.hhmm(b["seconds"])), file=out)
        for c in b["tracks"]:
            pos += 1
            owned = taste and c.get("key") in taste.tracks
            print("  %2d %s %-40s %-23s %5s  %-18s %s"
                  % (pos, "*" if owned else " ", c["title"][:40],
                     c["artist"][:23],
                     common.hhmm(c["secs"]) if c["secs"] else "-",
                     ",".join(sorted(x.split(":")[0]
                                     for x in c["sources"]))[:18],
                     c.get("why") or c.get("aff") or ""), file=out)
        total += b["seconds"]
    asked = sum(b["segment"]["minutes"] for b in blocks) * 60
    print("\n%d tracks, %s  (asked for %s)"
          % (pos, common.hhmm(total), common.hhmm(asked)), file=out)
