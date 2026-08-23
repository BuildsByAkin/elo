# DESIGN — mood transition engine

Phase 0 output. Written 2026-08-23 against the live APIs, not from memory.

The short version: **the brief's signal ordering is backwards for this library, and
I have the measurements to show it.** Everything else in the brief survives, and
about half of it is already built.

---

## 1. Where the code actually is

`git HEAD` is `checkpoint: library-first version before simplification`. The working
tree has since deleted `lyrics.py`, `tag.py`, `discover.py` and rewritten `elo.py`
into a pure co-listening tool with, in its own words, "no language model anywhere".

So there are two designs in the repo at once. Both are worth keeping — see §4.

| piece | state | verdict |
|---|---|---|
| `ingest.py` — Music.app export → SQLite | works, **843 tracks loaded** | keep as is |
| `common.py` — DB, schema, `llm()` | works | keep; `llm()` is the good part |
| `sources.py` — YTM radio + Last.fm similar, RRF-fused | works (working tree) | **keep — this is candidate generation** |
| `elo.py` — co-listening "what to play next" | works (working tree) | keep as one subcommand |
| `lyrics.py` — LRCLIB + Genius fetch/cache | written, **deleted in working tree** | **restore — this is the primary signal** |
| `tag.py` — mood card writer | written, **deleted in working tree** | **restore** |
| `elo.py arc` — mood interpolation | written at HEAD | restore, then rebuild properly (§5) |
| `docs/probe-findings.md` | 527 lines of real measurement | the reason this design looks like it does |

**The single most important fact: `tracks` has 843 rows. `lyrics` has 0. `moods` has 0.**

The tagger has never been run. Every quality question in the README — does the arc
sound like a gradient, does `next` hold a mood, what fraction is `known` — is
unanswerable today. That is the critical path, not new architecture.

`llm()` is verified working through the logged-in Claude Code CLI (`claude -p`),
so tagging 843 tracks costs **no separate API bill**. At `BATCH=20` that is 43 calls.

---

## 2. Phase 0 research — what I re-verified, and what I didn't

The README's research is one day old and I am not redoing it. Confirmed still true
and unchanged: Spotify `audio_features` dead (Nov 2024), AcousticBrainz frozen at
July 2022, Essentia needs local audio files, Musixmatch full text is a paid licence.

What was **new in this brief and therefore untested** — I measured all of it live.

### 2.1 Last.fm tags → V-A: this does not work on this library

The brief says build it first. On a random 25-track sample of the actual library:

| source | coverage | discriminates *within* an artist? | carries situation? |
|---|---|---|---|
| Last.fm **track** tags (`track.getTopTags`) | **2/25 — 8%** | yes | barely |
| Last.fm **artist** tags (`artist.getTopTags`) | 18/25 — 72% | **no — constant per artist** | no, pure genre |
| **LRCLIB lyrics** | **20/25 — 80%** | yes | yes |

Track-level tagging on Last.fm is far sparser than the literature suggests. The API
resolves the track correctly and returns an empty array anyway:

```
Goodbye Yellow Brick Road / Elton John  -> {"toptags": {"tag": []}}
Changes / Black Sabbath                 -> {"toptags": {"tag": []}}
```

**The brief's own canonical test case has zero Last.fm tags.** A tag-weighted VAD
average over the empty set is not a low-confidence score, it is no score.

Where tags do exist they are mostly not mood. *Someone Like You* returns
`soul(100), adele(69), piano(39), british(37), pop(14), female vocalists(14)` —
four genre tags, an artist name, a nationality and an instrument before
`beautiful(2)`. The tag mass sits on genre, decade and artist identity.

The artist-level fallback has decent coverage but is **useless for this product**:
it returns the same vector for every Black Sabbath track, so it cannot tell
*Changes* (a devastated ballad) from *Paranoid*. Mood transition is precisely a
within-artist discrimination problem.

This is the same shape as the finding already in `docs/probe-findings.md` — "tags
are bad at themes and genuinely good at genre" — now confirmed on Last.fm
specifically, which that probe had not tested.

Why this library in particular: it is 210 Hip-Hop/Rap, 97 Pop, 80 Afrobeats/Afro-Beat,
48 Worldwide, 26 Christian, and **529 of 843 tracks are 2020 or later**. Last.fm's
track-tag density is concentrated on the pre-2015 Western indie/rock canon. This
library is mostly outside it.

**Decision: Last.fm is demoted from "Signal 1, build first" to a genre/candidate
source.** Not deleted — it is genuinely good at genre, and `track.getSimilar` is
already doing real work in `sources.py`. It just cannot supply valence and arousal.

### 2.2 Lyrics: promoted to primary, and already written

80% coverage on a random sample, consistent with the 76% the earlier probe measured
on a deliberately-hard stratified sample. LRCLIB is alive, needs no key, no rate
limit. It has the canonical test case:

```
Changes / Black Sabbath — 604 chars
  "I feel unhappy, I feel so sad / I've lost the best friend that I ever had
   She was my woman, I loved her so / But it's too late now, I've let her go"
```

Unmistakable. The signal the brief expects to catch this is the one that catches it;
the signal the brief says to build first returns nothing at all for it.

`lyrics.py` already implements LRCLIB → Genius fallback with miss-caching. It works.
It just needs restoring from git.

### 2.3 NRC VAD / ANEW / MuSe

- **NRC VAD v2** (55k terms) is free for research, **commercial use requires a
  licence** from the author. If this ever ships, that is a real obligation.
- **MuSe** (90,408 songs, Zenodo/Kaggle, CC) is *derived from Last.fm tags* — so it
  inherits exactly the coverage gap in §2.1 and will be thin on a 2020s Afrobeats
  and rap library. Its Spotify-ID column is also now largely dead weight.

Both are still worth having, but **as validation, not as the scorer**: MuSe gives a
few hundred songs with independent V/A values to check our numbers against. That is
the honest use for it. I'd rather have a held-out check than a second weak scorer.

### 2.4 Audio features: blocked, not merely deferred

Essentia is the right target and cannot run here. It needs audio files; this library
is Apple Music streams. AcousticBrainz is frozen at July 2022 against a library
whose median year is 2023, and would need MBIDs, which are null for every row.
ReccoBeats exists and returns Spotify-shaped `valence`/`energy`, but its accuracy is
undocumented — I found no validation study.

**Keep the `AudioFeaturesProvider` interface and stub it, exactly as the brief says.**
But the README should say *blocked on having audio*, not *coming in phase 5*.

I'm flagging the YouTube-audio route the brief mentions: it is a ToS violation and I
am not going to build it. iTunes 30-second previews are the legitimate path and are
worth measuring when audio actually matters.

### 2.5 ytmusicapi — playlist push is fine

Current, maintained, 1.12.2 already pinned. Two auth modes: `browser.json` (paste
request headers, valid ~2 years) and OAuth (simpler, **does not work for uploads**).
`create_playlist()` works under both. Reads are unauthenticated, which is why
`sources.py` works today with no setup at all. Push needs a one-time auth step.

---

## 3. The one thing the brief gets wrong about the data model, and one thing it's missing

**Scale.** The brief specifies V-A on −1..+1. The schema, the mood anchors, the
tagger rubric and the README all use 0..10. It is a pure affine transform, so
nothing is gained by churning 843 rows and four documents. **Staying 0..10**,
documented, converting at the boundary if an external dataset needs it.

**Missing: `stance`.** The brief's structured output is
`{situation, valence, arousal, confidence}`. The existing card adds `stance` — one
of 12: `devastated`, `defiant`, `resigned`, `tender`, … That field is doing real
work the brief's schema cannot do. "Breakup" collapses *I Will Survive* and
*Someone Like You* into one bucket; they are the same situation, opposite records,
and belong on different playlists. The existing design is better here. Keeping it.

The brief's `situation` maps onto the existing closed 36-word `themes` vocabulary.
Same idea, already built, already argued for in the README.

**Fusion is premature.** The brief specifies weights (0.5/0.5, then 0.4/0.25/0.35)
and a >0.6 disagreement flag. With Last.fm demoted and audio blocked, **there is
one real signal**, and a weighted blend of one thing is just that thing. I'll build
the *provenance* columns so fusion can land the day a second signal earns its place,
and skip the blending math until then. Writing a fusion formula now would be
architecture for signals that do not exist.

---

## 4. Architecture — the two designs are complementary, not competing

The working-tree simplification and the HEAD mood engine are solving different
halves of the problem, which is why neither felt complete:

```
  CANDIDATE GENERATION            SCORING                  SELECTION
  "what songs could I play?"      "what is this song?"     "which, in what order?"

  library (843 owned) ─────┐
                           ├──►  mood card per track ──►  sustain: cluster sample
  YTM radio ───┐           │     (lyrics → LLM)           shift:   path walk
               ├─ RRF ─────┘     themes/stance/v/e        + constraints
  Last.fm ─────┘  (sources.py)   (lyrics.py + tag.py)     (engine)
   similar +                            ▲
   genre tags                           │
                              AudioFeaturesProvider (stubbed, blocked)
```

- **`sources.py` co-listening answers a question mood tags cannot**: what exists
  outside your 843 tracks. Last.fm and YTM are strong here and weak at mood.
- **Mood cards answer a question co-listening cannot**: what this song is *about*.
  The probe already proved the similarity walk can't recover theme — 4,594
  neighbours surfaced 7 library tracks, and they were the seeds.

Each source does the job it is actually good at. Nothing gets deleted.

**Module layout.** The brief asks for `sources/ scoring/ engine/ cli/`. The codebase
is ~530 lines across 4 files. A package split now is churn without benefit. Staying
flat, and splitting when `engine.py` earns its own directory — realistically when
the path solver and constraint set land in Phase 3. I'll note the split point rather
than pre-build the tree.

**Typing.** Same logic. New code — the scorer and the path engine, the parts with
unit tests — gets typed and mypy-clean. I'm not retrofitting annotations onto
working I/O code that nothing is going to change.

---

## 5. Refined phase plan

Phase 0 is done — this document. Renumbered against what actually exists:

**Phase 1 — turn the lights on. (the critical path, and it is cheap)**
Restore `lyrics.py` and `tag.py`. Merge the working tree's co-listening `elo.py`
with HEAD's mood commands instead of choosing between them. Fetch lyrics for 843
tracks, then tag all 843 — 43 batched calls through the CLI backend, no API bill.
**Deliverable: the four numbers the README admits it does not have.** Nothing below
this line can be evaluated until this runs.

**Phase 2 — validate on the test set.**
The brief's 5 named songs plus ~25 more, spanning the library's real composition
(Afrobeats, drill, Nigerian gospel — not just the Western canon, or the test set
measures the easy half). Gate: *Changes* comes out low-valence, low-arousal,
`themes=[breakup|heartbreak]`.

**Primary validation is your ear, not a dataset.** After tagging, print the 10 cards
the model is most confident about and the 10 it is least confident about, and you
react to them for five minutes. You know these 843 songs. The confidence extremes are
the highest-information slice: the top 10 tell us whether `known` means anything, and
the bottom 10 tell us whether `guessed` is failing gracefully or inventing things.
That is a better ground truth for *this* library than any published dataset.

MuSe cross-check is secondary and explicitly discounted — it is derived from Last.fm
tags, so it inherits the exact bias §2.1 just measured. Disagreement with MuSe on an
Afrobeats track is weak evidence against us. Publish the disagreements anyway, but
weight them below your reactions.

**Phase 3 — the engine (the actual product).**
Sustain = cluster sampling around a point, constrained to shared theme *and* stance.
Shift = path walk. Rebuild `arc` properly — the HEAD version is nearest-point
picking with no constraints, which is why it can hand you the same artist four times
or jump genre mid-ramp. Real constraints: no artist twice in a window, monotonic
energy ramp with a bounded per-step delta, genre coherence early, duration budget.
Pure math over a list of dicts, no network — **unit tested**, per the brief.

⚠️ **The tempo constraint cannot be built.** "No jumps > 15 BPM" needs BPM, and
§2.4 is why there is none. Substituting a bounded `energy` delta, which is the same
constraint in the axis we can actually measure. Flagging rather than faking it.

**Phase 4 — `sources.py` becomes a real candidate generator.**
Let shift mode pull in unowned tracks to fill gaps in the path, tag them on demand,
mark them `external=1`. This is where the deleted `discover.py` comes back, and
where co-listening earns its place in the mood product.

**Phase 5 — push to YouTube Music.** `create_playlist()` + fuzzy match + skip-with-
warning. Small, and genuinely satisfying once Phase 3 output is worth listening to.

**Phase 6 — audio, aimed at the hole rather than at everything.**
Interface stubbed from Phase 3. When audio becomes feasible (iTunes 30-second
previews → Essentia), **do not run it over the library.** Run it over the ~170
lyric-less tracks — the instrumentals, the lyric-less Afrobeats, the gospel — which
are the only tracks with no real signal at all today, carrying title-inference cards
marked `guessed`.

This changes the economics of the phase entirely. "Analyse 843 songs" is a project
with a rate-limit problem; "patch 170 cards" is an afternoon where preview rate
limits stop mattering. Audio is worth the least where lyrics already work and the
most where they returned nothing, so it should be pointed only at the second group.
`basis` already records which tracks those are, so the target set is a `WHERE` clause.

Commit per phase.

---

## 6. Risks I'm carrying forward

- **~20% of the library has no lyrics** — instrumentals, some Afrobeats and gospel.
  Those cards are title inference. `confidence='guessed'` marks them and
  `--known-only` filters them; it costs reach. This is a real hole, not a rounding
  error.
- **Nothing here is validated yet.** Every quality claim in the README is still
  unmeasured. Phase 1 exists to fix that before we build on top of it.
- **Lyrics truncate at 1,600 chars**, cutting roughly half of them mid-song. Probably
  fine for theme, questionable for stance. Worth an ablation once tagging works.
- **The scoring weights (`6 / 1.5 / 4`) were reasoned, not tuned.** They stay guesses
  until there is a test set to tune against.
- **LLM emotion annotation underperforms human experts on nuance** (2025 study, cited
  in the README). Treat valence/energy as useful orderings, not measurements. This
  costs less than it sounds like: the engine only ever asks *is B more energetic than
  A*, never *what is B's arousal*. Sustain sorts by distance, shift walks a monotonic
  ramp — both are rank operations. A card that is consistently half a point pessimistic
  changes no output. A card that ranks a wallow above an anthem does, which is why
  `stance` and the Phase 2 ear-check matter more than absolute calibration.
- **Genius fallback scrapes HTML** and will break on redesign. LRCLIB carries most of it.
- **NRC VAD needs a commercial licence** if this ever ships.
- **ytmusicapi is reverse-engineered InnerTube.** It works today; Google can break it.
- **Stated but not built:** no playback, no tuning, no audio.

---

## 7. What I need from you

Three calls before Phase 1 — my recommendation on each is in bold.

1. **Last.fm demoted from V-A scoring to genre/candidates.** §2.1 is the evidence.
   The alternative is building it as specified and getting a score for 8% of tracks.
2. **Keep 0..10, keep `stance`, skip fusion until a second signal exists.** §3.
3. **Merge the two `elo.py` designs rather than picking one** — co-listening becomes
   candidate generation, mood cards become scoring. §4. Nothing gets thrown away.

If those land, Phase 1 runs immediately and cheaply, and for the first time this
repo will have actual numbers in it.
