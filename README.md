# elo

Pick music by what it is **about** and where it leaves you.

Streaming recommenders know how a song sounds and who else played it. They do not
know it is a breakup song, and they cannot tell a breakup from a grief. elo tags
your own library with what each song is about and how it feels, then selects over
those tags.

```
source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-...

python ingest.py ~/Desktop/library.txt     # Music.app export -> data/elo.db
python lyrics.py                           # fetch + cache lyrics (once)
python tag.py                              # write a mood card per track (once)

python elo.py next "Tell Your Friends" "The Weeknd"
python elo.py arc sad hyped 20
python elo.py moods
```

## The mood card

Every track gets one, cached in SQLite. It is the whole product; the two commands
are just different ways of selecting over it.

| field | what it is |
|---|---|
| `themes` | 1–3 from a **closed** 36-word vocabulary — `breakup`, `grief`, `betrayal`, `faith`, `hustle`… |
| `stance` | 1 of 12 — `devastated`, `defiant`, `resigned`, `tender`… |
| `valence` | 0–10, desolate → elated |
| `energy` | 0–10, still → frantic |
| `summary` | one line; must quote the lyric when lyrics were available |
| `confidence` | `known` or `guessed`, self-reported and strict |

The theme vocabulary is closed on purpose. Free text drifts — *breakup*,
*heartbreak*, *the end of a relationship* — and stops being filterable. A closed
list is the difference between a tag and a description.

`stance` is the field that does the work the original pitch asked for. "Breakup"
alone collapses defiant, devastated and relieved into one list; they belong on
different playlists.

## The two commands

**`next`** holds a mood. It reads the seed's card and ranks your library by
`6 × theme overlap + 1.5 if the stance matches − 4 × distance in valence/energy`.
Subject dominates, mood-space nearness breaks ties. A seed you do not own gets
looked up and tagged once, stored with `external=1` so it never pollutes the pool.

**`arc`** moves you. It draws a straight line between two named moods, divides it
into as many steps as the minutes allow, and picks the nearest untaken track to
each point. `sad → hyped over 20 min` is a real gradient, not a hard cut.

Named moods (`python elo.py moods` shows how many of your tracks sit near each):

```
heartbroken v1.0 e3.0    numb        v3.0 e1.5    content  v7.0 e4.0
sad         v2.0 e2.5    reflective  v4.0 e2.5    happy    v8.0 e6.0
angry       v2.5 e8.5    calm        v5.5 e1.5    hyped    v8.5 e9.0
focus       v5.0 e4.0    romantic    v6.5 e3.5    chill    v6.0 e3.0
```

These anchors are judgement calls, not measurements. They are constants at the top
of `elo.py` and are meant to be argued with.

## Where the data comes from

**Lyrics — LRCLIB, then Genius.** LRCLIB (`lrclib.net`) is open, needs no key and
has no rate limit; it carries most of it. Genius has no usable public API — the
official one returns no lyric text — but its own site's search endpoint answers
unauthenticated and the song page carries the words, so it runs as a fallback for
what LRCLIB misses. Both are cached per track, including misses, so a track is
fetched exactly once ever. **This is not a crawl of all music — only what you own.**

**Mood — the model, over lyrics plus metadata.** Not a shortcut. Checked in
August 2026, there is no longer any API that returns valence and energy:

- Spotify's `audio_features` was killed in November 2024 and tightened again in
  February 2026. There is no replacement and Spotify has said there will not be.
- AcousticBrainz is free but frozen at July 2022. The median year in this library
  is 2023.
- Essentia is open source and genuinely good, but it needs **audio files**. This
  library is all Apple Music streams, so there is nothing to analyse. If that ever
  changes it becomes a fourth data source, not a replacement.
- Apple Music exposes a `hasLyrics` boolean and no text. Musixmatch's free tier
  returns 30% of a lyric; full text is a paid licence.

So text is the only route to mood for recent music, and the model is the only
thing that reads text. Worth knowing: a 2025 study found GPT-4o's emotion
annotation falls short of human experts on nuance. Treat `valence` and `energy` as
useful orderings, not measurements.

## What the earlier probe ruled out

Kept in `docs/probe-findings.md` because it is the reason the design looks like
this. Three findings carried forward:

1. **MusicBrainz theme tags cannot do this.** `rock` has 25,506 co-occurrences;
   `betrayal` has 6. The tag measures "somebody typed this word", not what the
   song is about. Dropped as a candidate source.
2. **The similarity walk is a dead end for this library.** ~4,500 co-listening
   neighbours across three popularity bands surfaced 7 library tracks, and they
   were the songs it started from. Co-listening data carries the same Western skew
   as the model, so it cannot rescue what the model cannot read.
3. **The model reads about a quarter of this library from metadata alone** — and
   the ignorance is concentrated, not spread. African, Afrobeats and Nigerian
   gospel score ~4%; tracks from 2020 on score under 15%. That number is *why*
   lyrics are fetched at all.

## Honest state

Built and runnable: ingest with durations, lyric fetch and cache, the mood card
schema, the tagger, and both commands.

**Not yet measured.** The tagger has not been run over the full library — that
needs an API key. Until it has, nothing here is quality-checked, and the numbers
below are the ones that matter and do not exist yet:

- what fraction of cards come from lyrics rather than metadata
- what fraction the model marks `known`
- whether `next` output actually holds a mood when you listen to it
- whether the `arc` gradient is audible or just numerically tidy

Lyric coverage on a stratified 100-track sample of the hardest tracks was 76%
(LRCLIB 60, Genius 16). Full-library coverage is being measured now and is running
higher, since that sample was deliberately the tracks the model already failed on.

## Known limits

- **`guessed` cards are title-matching.** For ~24% of tracks with no lyrics
  anywhere — instrumentals, Nigerian gospel, non-English releases — the card is an
  inference from the title. `--known-only` filters them out and costs you reach.
- **Truncation.** Lyrics are capped at 1,600 characters, which cuts about half of
  them mid-song. Fine for theme, possibly not for stance.
- **`arc` assumes the export has durations.** Missing ones fall back to 210s.
- **No playback.** It prints a list. Wiring it to Apple Music is unstarted.
- **The scoring weights in `next` are guesses.** `6 / 1.5 / 4` was chosen by
  reasoning, not tuning. They are constants at the top of `elo.py`.
- Genius extraction scrapes HTML and will break when Genius redesigns.
