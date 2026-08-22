# elo — probe

Give it a mood/theme word, get 5 songs **from your own library** that are *about* that
thing.

```
source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-...
python ingest.py ~/Desktop/library.txt     # -> data/elo.db
python elo.py "breakup"
```

CLI only. No playback, no Apple Music API, no UI. Phase 1 of 2.

## What I installed

- Python 3.12.13 venv at `.venv` (system Python is 3.9.6 — too old for current troi).
- `troi==2026.7.31.0` without the `nmslib` extra, and `requests==2.34.2`.
- SQLite and `plistlib` come from the standard library. No new dependencies.

## The main finding: ListenBrainz tags cannot do this

This was the thing worth checking, and the answer is a clear no.

MusicBrainz folksonomy tags are overwhelmingly **genre** tags. Theme and stance tags
exist but are applied by a handful of users to a handful of releases. From
`labs.api.listenbrainz.org/tag-similarity`, the top co-occurrence count for a tag:

| tag | top co-occurrence count |
|---|---|
| `rock` | 25,506 |
| `grief` | 100 |
| `breakup` | 70 |
| `heartbreak` | 40 |
| `loneliness` | 22 |
| `betrayal` | **6** |

Three to four orders of magnitude. And the sparsity is not evenly spread — it is
concentrated on self-releasing artists who tagged their own tracks. Running the tag
radio directly:

```
$ troi playlist lb-radio easy 'tag:(breakup)'
Gone Under                     MowMowMow
Ditch Blues                    Dick MacInnis
marceline (no autotune)        joxer
Allow Me                       JaJa Kisses
Take Care Of The Girl          Dick MacInnis
...
```

40 candidates, essentially all unknown artists. The tag is not measuring "this song is
about a breakup." It is measuring "somebody typed the word breakup into MusicBrainz."
Widening to better-populated mood tags does not rescue it — `tag:(melancholy)` returns
Enrique Iglesias' *Bailamos* as a top hit.

So: **ListenBrainz tags alone are far too coarse to tell breakup from grief.** They
cannot even reliably tell breakup from *not-breakup*. I used the LLM path you
pre-authorized.

## Phase 1: the library is the pool

`ingest.py` parses a Music.app export into `data/elo.db`, table `tracks`
(`id, title, artist, album, mbid`), deduped on `(title, artist, album)`. It accepts the
XML plist Music.app writes *or* its tab-separated export, and sniffs the byte-order mark
so UTF-16 and UTF-8 both work.

`elo.py` then does exactly one thing: load every track from SQLite, hand the list to
one Claude call, get back up to 5 picks with a reason each. The Claude
candidate-generation call from the earlier probe is **deleted** — nothing invents songs
any more. The ListenBrainz tag pool is **deleted**. The library is the only source.

Chunking exists (`CHUNK = 2500`) and did not trigger for this library. Above that it
ranks each chunk, then runs a final pass over the chunk winners.

The ranking prompt is told explicitly that returning fewer than 5 — or zero — is
correct, and not to pad. That instruction turned out to matter.

## Phase 1 results

`~/Desktop/library.txt` is a Music.app tab-separated export: **UTF-16 little-endian with
CR line terminators** and 31 columns. `wc -l` reports 0 lines for it, which is the tell.
`ingest.py` sniffs the BOM and uses `splitlines()`, so it handles UTF-16 or UTF-8 and
CR, LF or CRLF without configuration.

```
parsed   849 rows from /Users/a12/Desktop/library.txt
loaded   843 new (6 duplicates skipped)
total    843 tracks in data/elo.db
```

**843 tracks, 725 distinct artists, 0 missing artist metadata.** Heaviest artists:
YoungBoy Never Broke Again (9), Zach Bryan (6), Kanye West (4), Harry Styles (4).

**Chunking did not trigger.** 843 track lines is roughly 12K tokens, which fits one call
comfortably. I raised `CHUNK` from 400 to 2500 to make that true: chunking a library
this size would have split it into three calls and taken a chunk-local top 5 from each,
which can crowd out a better global pick for no benefit. The two-pass path is still
there for libraries above 2500 tracks.

### `python elo.py "breakup"` — three runs

| # | run 1 | run 2 | run 3 |
|---|---|---|---|
| 1 | Somebody That I Used to Know — Gotye | Somebody That I Used to Know — Gotye | Somebody That I Used to Know — Gotye |
| 2 | F\*\*k It (I Don't Want You Back) — Eamon | Dodged a Bullet — Greg Laswell | i hate u, i love u — gnash |
| 3 | Dodged a Bullet — Greg Laswell | happier — Olivia Rodrigo | Sweetheart, What Have You Done to Us — Keaton Henson |
| 4 | Sweetheart, What Have You Done to Us — Keaton Henson | F\*\*k It (I Don't Want You Back) — Eamon | F\*\*k It (I Don't Want You Back) — Eamon |
| 5 | Good Grief — Bastille | i hate u, i love u — gnash | Dodged a Bullet — Greg Laswell |

### How good is it, honestly

**Good, and better than the earlier probe on every axis that matters.**

- **Precision is high.** All 15 slots are genuine breakup songs. No filler, no
  theme-adjacent drift.
- **Stability improved a lot.** 7 distinct songs across 15 slots, versus 11 in the
  earlier probe. Three songs — Gotye, *Dodged a Bullet*, *F\*\*k It* — appear in all
  three runs, and Gotye is #1 every time. Ordering below the top slot still moves.
- **The canon bias is gone.** No Adele, no Gloria Gaynor, no Taylor Swift. Instead:
  Greg Laswell, Keaton Henson, Eamon, gnash. That is the whole point — these are picks
  you could not get from a popularity-driven recommender, and they came out of a
  725-artist library without any tag or genre signal.
- **It reads titles as well as songs.** *Good Grief* by Bastille placed 5th in run 1.
  It is a defensible breakup pick, but the title collision with the adjacent theme is
  worth watching when Phase 2 tests grief-vs-breakup on this library.
- **The explanations remain unverified.** Nothing was added to constrain them since the
  fabrication found in the earlier probe. They read accurately here, but "reads
  accurately" is not "checked".

Residual weakness: with the pool fixed, run-to-run variance now comes only from ranking,
not from candidate sampling. That is a much smaller problem, and consensus across runs
would close most of it.

## Cross-theme test: is it reading titles or meaning?

Three runs each of `grief` and `loneliness` on the same 843-track library, against the
`breakup` runs above.

| theme | distinct songs / slots | appears in all 3 runs |
|---|---|---|
| breakup | 7 / 15 | Somebody That I Used to Know, Dodged a Bullet, F\*\*k It |
| grief | 7 / 14 | Kettering, Army Dreamers, You Were Born |
| loneliness | 9 / 13 | Tired of Being Alone |

### Songs that landed in two themes

Only two, out of ~21 distinct songs picked:

- **Good Grief — Bastille**: `breakup` run 1 (#5) and `grief` run 2 (#5). Yes, it does
  appear under grief.
- **Kettering — The Antlers**: `grief` all three runs (#1 every time) and `loneliness`
  run 1 (#3).

Neither is obviously wrong. *Good Grief* is genuinely ambiguous source material, and it
placed 5th — last — in both themes, which is the right place for an ambiguous song.
*Kettering* describes sitting alone at a hospice bedside; grief and isolation are both
truthfully in it, and it ranked #1 for grief and #3 for loneliness, which is the correct
relative ordering.

### The answer: it reads meaning, and falls back to titles when it doesn't know the song

The `grief` results settle it. The top picks were:

| song | title contains a grief word? | why it is correct |
|---|---|---|
| Kettering — The Antlers | no | *Hospice* concept album, caregiver watching a patient die |
| Army Dreamers — Kate Bush | no | a mother mourning a son killed in the army |
| **You Were Born — Cloud Cult** | **points the opposite way** | written after Craig Minowa's infant son died |
| Baptisms / Summer Skeletons — Radical Face | no | family death and mourning across *The Bastards* |

*You Were Born* is the decisive case: the title says **born**, and it was still ranked
under grief, three runs out of three, for the correct biographical reason. No
title-matcher reaches that. The same holds for breakup, where *Dodged a Bullet*,
*Sweetheart, What Have You Done to Us* and *Somebody That I Used to Know* contain no
breakup keyword.

But `loneliness` run 3 shows the failure mode plainly:

```
3. LONELY ROAD — O'Kenneth & Xlimkid
   Titled 'LONELY ROAD,' the track frames the narrator's journey as...
4. Alone — Jimmygid
   From an EP called 'Into the Lonely Verse,' this song leans into...
5. Lonely Lonely Nights — Little Julian Herrera
   An old doo-wop tune whose title and refrain...
```

Three obscure tracks the model almost certainly does not know, and all three
justifications cite the **title or the album name** rather than any lyric. It fell back
to string matching — and, usefully, **said so in the reason text**. The explanations
double as a tell: content-based reasons mean it knows the song, title-based reasons mean
it is guessing.

That maps directly onto stability. `grief` is the most stable theme (7 distinct in 14
slots, same top 3 every run) because the library's grief songs are ones the model knows.
`loneliness` is the least stable (9 distinct in 13 slots, one song common to all runs,
and one run returned only 3 picks) because "loneliness" is a diffuse mood rather than an
event, and the library's lonely-titled tracks are obscure.

**Practical read: trust the picks whose reason quotes content, discount the ones whose
reason quotes the title.** That distinction is visible in the current output and is the
cheapest available quality signal — worth making explicit rather than leaving the user
to notice.

## Coverage: how much of the library does the model actually know?

`python elo.py --coverage` sends all 843 tracks in 6 calls of 150 and asks, per track,
whether it is reasoning from real song content or inferring from the title. Verdicts are
cached in `tracks.confidence`, so `--min-confidence known` is free after one pass. A
track the model fails to return counts as `guessed` — strict by default.

```
known     209 / 843  (24.8%)
guessed   634 / 843  (75.2%)
```

*(A later re-run after a schema change scored 240/843 = 28.5%. The classification drifts
about 4 percentage points run to run; treat the figure as "roughly a quarter", not exact.)*

**It genuinely knows a quarter of this library.** Everything the earlier cross-theme
test suggested is now a number.

And the ignorance is concentrated, not spread: **541 of the 562 artists in the guessed
set have *every* track guessed.** The model does not half-know artists. It either knows
a catalogue or it does not.

### The guessed set clusters on two independent axes

**1. Region — African music is a near-total blind spot.**

| genre | known |
|---|---|
| Rock | 61.1% (11/18) |
| R&B/Soul | 59.5% (25/42) |
| Soundtrack | 53.3% (8/15) |
| Pop | 48.5% (47/97) |
| Country | 45.0% (9/20) |
| Alternative | 35.1% (26/74) |
| Hip-Hop/Rap | 17.1% (36/210) |
| Amapiano | 9.1% (1/11) |
| Afro-Pop | 9.1% (1/11) |
| Worldwide | 8.3% (4/48) |
| **Afro-Beat** | **2.7% (1/37)** |
| **Afrobeats** | **2.3% (1/43)** |
| **African** | **0.0% (0/13)** |
| **Christian** (Nigerian gospel) | **0.0% (0/26)** |

Taken together, the African / world / gospel block is **8 known out of 189 — 4.2%**.
Named artists in it: Seyi Vibez, Ayo Maff, Fireboy DML, OMAH LAY, Qdot, K1 De Ultimate,
Stanley Okorie, Tope Alabi, Sola Allyson, Labisi, Oumou Sangaré.

**2. Era — a textbook training-cutoff curve.** Median year of a known track is **2013**;
of a guessed track, **2023**. With the African/world/gospel block excluded, so this is
not the same effect twice:

| released | known |
|---|---|
| 2000-2004 | 68.2% (15/22) |
| 2005-2009 | 57.6% (19/33) |
| 2010-2014 | 54.2% (32/59) |
| 2015-2019 | 38.4% (43/112) |
| 2020-2024 | 14.4% (41/284) |
| 2025-2029 | 4.5% (4/89) |

Both axes are real and independent. A third, weaker one is catalogue depth: prolific rap
artists score badly even when famous — YoungBoy Never Broke Again is 0/9, Lil Durk 0/3.
The model knows the artist and not the tracks.

### `--min-confidence known`

Per-pick confidence now prints inline, and the flag restricts the pool to the 209
known tracks:

```
$ python elo.py "breakup"                              # 843 tracks
5. Can't Pretend — Tom Odell  [guessed]

$ python elo.py "breakup" --min-confidence known       # 209 tracks
1. Somebody That I Used to Know — Gotye  [known]
2. I Will Survive — Gloria Gaynor  [known]
3. i hate u, i love u — gnash  [known]
4. F**k It (I Don't Want You Back) — Eamon  [known]
5. happier — Olivia Rodrigo  [known]
```

The unfiltered run put one guessed pick in the top 5 and labelled it. The filtered run
is all-known — but note it surfaces *I Will Survive*, which is exactly the canon the
library was supposed to get us away from. **The filter trades discovery for
reliability**, and 75% of the library is the price.

That is the real tension this measurement exposes: the parts of this library that are
most distinctive — recent, African, deep rap catalogues — are precisely the parts the
model cannot reason about. Lyrics would fix this, and this is the number that says how
much they would buy.

## Do external sources cover the 634 guessed tracks?

40 guessed tracks, stratified: 15 African / Afrobeats / Nigerian gospel, 15 recent
(2023+) non-African, 10 deep-catalogue rap (YoungBoy, Lil Durk, Fredo Bang).

**Musixmatch could not be tested.** Its API returns 401 without an `apikey`, and the
free developer plan (~2,000 calls/day, 30% lyric snippet) requires signing up. There is
no unauthenticated path. Genius's official `api.genius.com` also 401s, but the search
endpoint its own website uses answers unauthenticated, which is enough for an existence
check. I added **LRCLIB** (`lrclib.net`, open, no auth) as a substitute lyrics source —
it was not on your list, but Musixmatch was blocked and the qualitative test needs real
lyric text.

| track | bucket | MB | Genius | LRCLIB |
|---|---|---|---|---|
| Gbegesi Not — Damo K | african | — | hit | — |
| Adulthood Anthem — Ladé | african | — | hit | hit |
| Azul — DJ Sumbody | african | — | — | hit |
| Oshimiriatata — Faith Captain | african | — | — | — |
| Special Live Release Pt.1 — Chief Ebenezer | african | — | — | — |
| ON GOD — Shatta Wale | african | — | hit | hit |
| B'ola (Honour) — Sunmisola Agbebi | african | — | — | hit |
| Imnandi lento — Mellow & Sleazy | african | — | hit | — |
| Laho — Shallipopi | african | hit | hit | hit |
| SINCE COVID19 — K code | african | — | — | — |
| Proud Fvck Boys — Tulenkey | african | — | — | hit |
| Destroy Myself Just For You — Montell Fish | african | hit | hit | hit |
| Junction — Stay Shun | african | — | hit | hit |
| Oghenedo — Damo K | african | — | — | hit |
| Egwu — Chike & MohBad | african | — | hit | hit |
| Sunshine (Western AF) — The Red Clay Strays | recent | — | — | hit |
| Kusho Bani — Cassper Nyovest | recent | — | hit | hit |
| Thee Person — Pardison Fontaine | recent | hit | hit | — |
| Bass Boat — Zach Bryan | recent | hit | hit | hit |
| Murdaside (ScouseMix) — Mazza_l20 | recent | — | hit | hit |
| Christmas Amapiano — Bluenax | recent | — | — | — |
| Growin' Pains — honestav | recent | hit | hit | hit |
| No Statements — ScarLip | recent | hit | hit | hit |
| ALL WHITE — MAF Teeski | recent | — | — | — |
| For The Team — Fresh G | recent | — | hit | — |
| Can't Find the Man — Ashley Singh | recent | — | hit | hit |
| Thang For You — Rylo Rodriguez | recent | — | hit | hit |
| Love Me — JMSN | recent | hit | hit | hit |
| Merry Christmas, i miss you — Alex Crichton | recent | hit | hit | hit |
| Conundrum — Wale | recent | hit | hit | hit |
| Difference Is — Lil Durk | deeprap | — | hit | hit |
| Red Eye — YoungBoy NBA | deeprap | hit | hit | hit |
| Say Please — Fredo Bang | deeprap | hit | hit | hit |
| Through the Storm — YoungBoy NBA | deeprap | hit | hit | hit |
| Expedite This Letter — Lil Durk | deeprap | hit | hit | hit |
| Cross Me — YoungBoy NBA | deeprap | — | hit | hit |
| My'ya — YoungBoy NBA | deeprap | hit | hit | hit |
| Emo Rockstar — YoungBoy NBA | deeprap | hit | hit | hit |
| Top — Fredo Bang | deeprap | — | hit | — |
| Bitch Let's Do It — YoungBoy NBA | deeprap | hit | hit | hit |

### Hit rates

| bucket | model knew | MusicBrainz | Genius | LRCLIB | Genius ∪ LRCLIB |
|---|---|---|---|---|---|
| african | ~4% | **13%** | 53% | 67% | **80%** |
| recent | ~14% | 47% | 80% | 73% | **87%** |
| deeprap | 0% | 70% | **100%** | 90% | **100%** |
| **total** | 24.8% | **40%** | 75% | 75% | **88%** |

### The answer: MusicBrainz shares the blind spot, lyrics sources do not

**MusicBrainz has the same bias, worse.** 13% on African music versus 70% on deep-catalogue
rap. It is an editor-maintained Western-leaning database, so it under-covers exactly what
the model under-covers. **Seeding Phase 2 discovery from MusicBrainz IDs would inherit
the blind spot from both directions at once** — which is the single most important thing
this test found, and it argues for reordering Phase 2.

**Lyrics sources break the correlation.** Genius ∪ LRCLIB reaches 88% overall and 80% on
the African bucket — the bucket where the model scores 4% and MusicBrainz 13%. Deep rap
goes from 0% model knowledge to 100% lyric availability. The two sources are
complementary rather than redundant: Genius wins on rap, LRCLIB wins on African music,
and the union beats both.

Five tracks had nothing anywhere: two Nigerian gospel (Faith Captain, Chief Ebenezer),
one Amapiano Christmas compilation, and two very small rap releases.

### Qualitative test: does a lyric fix the guess?

Five tracks the model had marked `guessed`, classified twice — once from title and
artist alone, once with LRCLIB lyrics attached.

| track | without lyrics | with lyrics |
|---|---|---|
| **Red Eye** — YoungBoy NBA | Struggle/paranoia — *"street life and fame"* `guessed` | **Grief** and street loyalty — *"loss and grief for fallen friends"* `known` |
| **Conundrum** — Wale | Self-reflection — *"fame, insecurity"* `guessed` | **Emotional detachment** — *"unable to love or commit despite intimacy"* `known` |
| **Egwu** — Chike & MohBad | Celebration/Dance — *"celebrate love and joy"* `guessed` | Music's universal power `known` |
| **Growin' Pains** — honestav | Growing up `guessed` | Growing pains/nostalgia — *"disconnected from old friends"* `known` |
| **Laho** — Shallipopi | Wealth and success `guessed` | Braggadocio/fame `guessed` |

**Four of five flipped `guessed` → `known`, and three had materially wrong themes
corrected.** Two of those corrections land directly on elo's use case:

- *Red Eye* is a **grief** song. Without lyrics it was filed as generic street paranoia
  and would never have surfaced under `grief`. This is the product thesis in one row.
- *Conundrum* is about **inability to commit** — a relationship theme. Without lyrics it
  was "fame and insecurity" and would never have surfaced under a breakup query.

*Laho* stayed `guessed` even with lyrics — the text is largely Nigerian Pidgin and Edo,
and the model declined to claim confidence. The strictness holds up.

## The similarity walk

`python elo.py "breakup" --walk --min-confidence known`

Verified against the live API before coding, because the published docs are thin here:

- The endpoint the walk needs is **`GET api.listenbrainz.org/1/lb-radio/artist/{artist_mbid}`**,
  with `mode`, `max_similar_artists`, `max_recordings_per_artist`, `pop_begin`, `pop_end`.
  Troi's own LB Radio docs describe only the prompt DSL and **do not document
  `pop_begin`/`pop_end` at all** — I read them out of `troi/recording_search_service.py`
  in the installed package and confirmed them against the live endpoint.
- Troi's mode-to-band mapping is `easy=(0,33)`, `medium=(33,66)`, `hard=(66,100)`, where
  0 is the most-listened tier. `POP_BEGIN` / `POP_END` are constants at the top of
  `walk.py`.
- **Rate limiting is real and honoured.** The endpoint returns `X-RateLimit-Limit: 30`,
  `X-RateLimit-Remaining` and `X-RateLimit-Reset-In`. `walk._throttle` reads
  `Remaining` and sleeps `Reset-In + 1` when it drops to 2 or fewer, rather than
  blind-sleeping between calls.
- The walk returns `recording_mbid` **without a title**, so titles come from a second
  batched call to `labs.api.listenbrainz.org/recording-mbid-lookup`.

### Seed resolution needed two fallbacks

`acr-lookup` requires an exact artist credit, and MusicBrainz stores collaborations
under the joint credit — *Somebody That I Used to Know* is credited to
**"Gotye feat. Kimbra"**, not "Gotye". Two of five seeds silently failed until the
resolver learned to retry on the bare title and then fall back to fuzzy
`recording-search` → `recording-mbid-lookup`. With that in place, 5/5 seeds resolve.
Worth flagging because the same exact-match assumption will bite Phase 2's bulk MBID
resolution.

### Results

Three popularity bands, same five seeds, ~4,500 cached edges across 255 similar artists:

| band | pool | matched library | of those **guessed** | of those **African** |
|---|---|---|---|---|
| `medium` 33-66 | 1,676 | 3 | **0** | **0** |
| all 0-100 | 1,374 | 6 | **0** | **0** |
| `hard` 66-100 | 1,413 | 7 | **1** | **0** |

### Blunt answer: it re-found what we already had

**The walk did not reach the guessed set.** Across roughly 4,500 pool recordings and
three popularity bands, it surfaced at most 7 library tracks, of which **exactly one**
was in the 603-track guessed set — and that one is *Bass Boat* by Zach Bryan, who was
himself a seed artist. **Zero African tracks, in every band.** The expectation of ~0 is
confirmed.

Worse, the matches are barely novel. Several are the seeds themselves or other tracks by
the seed artists (*F\*\*k It* — Eamon, *Something in the Orange* — Zach Bryan,
*i hate u, i love u* — gnash, *Can't Catch Me Now* — Olivia Rodrigo). The genuinely new
finds across all three bands amount to about ten tracks — Hozier, Harry Styles, Clean
Bandit, Drake, Shaggy, Rainbow Kitten Surprise, Radical Face — and **every one of them
is already in the `known` set.** The walk's reach is precisely the region the LLM could
already reason about.

The intersection rate is about **0.4%** of pool recordings. Widening the popularity band
does not help; the `hard` tail performs marginally better than `medium`, which is noise
at this scale.

This is the same finding the source-coverage test predicted, now demonstrated end to
end: **the ListenBrainz similarity graph is built from listening data with the same
Western skew as the model's training data.** Tracks that are missing from one are
missing from the other. Similarity cannot rescue the 71% of this library the model
cannot read, because those tracks are not meaningfully in the graph either.

For discovery of music the user does *not* own, the walk may still be useful — that is
untested here, since this test deliberately intersected back against the library. But as
a way to reach the guessed set, it is a dead end.

### The cached graph

Edges persist in `similar`, keyed by `(seed_artist_mbid, recording_mbid)` and tagged with
the `mode`/`pop_begin`/`pop_end` they were fetched under, so re-runs reuse them and a
changed band refetches. Currently **4,594 edges, 4,531 distinct recordings, 255 similar
artists, 6 seed artists** — the database is 1.6 MB. Re-running the walk with a warm cache
makes no network calls to the radio endpoint at all.

## Phase 2 preview: where ListenBrainz earns its place

It was wrong as a candidate source. It has three other jobs, all tested against the live
API:

- **Identity / join layer — the one the product cannot exist without.**
  `apple-music-id-from-mbid` returns Apple Music track IDs for a recording MBID
  (*Silver Springs* → `1441359423`). With `acr-lookup` going the other way, that is a
  round trip between the user's library and a canonical ID. There is no other free
  source for this.
- **Recall expansion.** `similar-recordings` returns ~27 co-listening neighbours per
  seed. Tested on *Silver Springs*, it returns Stevie Nicks solo material, Billy Joel's
  *Vienna* and CCR's *Fortunate Son* — that is artist adjacency and co-listening
  behaviour, **not** theme. So it widens the pool and the model still has to re-filter
  it. That is the right split: the model supplies precision, ListenBrainz supplies
  recall.
- **Genre as a second axis.** Tags are bad at themes and genuinely good at genre
  (`rock` = 25,506). "Sad breakup songs, but country" wants theme from the model and
  genre from the tags — each doing what it is actually good at.

## What this means for the actual product

The earlier probe had the model supplying candidates and ListenBrainz filtering them.
Phase 1 inverts that correctly: the library supplies candidates, the model does only the
theme judgement — the part it is demonstrably good at. Phase 2 adds the third leg,
ListenBrainz similarity for music you do *not* own, kept in a separate code path and a
separate output section.

## What doesn't work

- `tag:(<theme>)` as a candidate source. Produced 0 of 20 picks across four themes;
  removed.
- The one-line explanations. In the earlier probe one was outright fabricated
  (*Alone Again (Naturally)* justified with *Eleanor Rigby*'s lyrics). Nothing has been
  added to constrain them, so treat them as unverified.
- `mbid` is a column in `tracks` and is null for every row. Phase 2 fills it.
- 75.2% of the library. The model does not know those tracks and says so; ranking over
  them is title-matching. See the coverage section.
- Emotional stance. "Breakup" still collapses defiant, devastated and relieved into one
  list. Unbuilt.
- Themes narrower than a word or two. The prompt is passed through roughly as-is.
- No caching. Each run is one LLM call plus SQLite; about 5-10 seconds.
- `nmslib` is not installed, so troi's fuzzy local-collection matching is unavailable.
  Irrelevant here — nothing touches a local collection.
- Requires `ANTHROPIC_API_KEY`. There is no offline mode and no useful degraded mode —
  without the key there is nothing left, as the tag table above explains.
