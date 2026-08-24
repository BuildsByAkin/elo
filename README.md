# elo

Say what you want to listen to. Get a playlist.

```
$ python elo.py "i am in the mood of tell your friends by the weeknd"

sustain  45 min   seed: Tell Your Friends — The Weeknd
  1. dark moody R&B     45 min  [R&B & soul  #dark r&b #trap soul #atmospheric]

dark moody R&B  (R&B & soul, 44:05)
    Acquainted                    The Weeknd        5:49  radio,similar
    CLOUDED                       Brent Faiyaz      1:51  radio,similar
    Wus Good / Curious            PARTYNEXTDOOR     3:33  radio,similar
    Needed Me                     Rihanna           3:12  radio,similar
    Solo                          Future            4:26  radio,similar
    Broken Clocks                 SZA               3:52  radio,tag
    ...
12 tracks, 44:05  (asked for 45:00)
```

```
$ python elo.py "start me on tell your friends and take me from sad to happy over 30 minutes" --push
```

## The idea

Nothing here knows what a song *means*. There is no mood model, nothing is
tagged, and no corpus is built. The question it asks is much cheaper and much
better answered: **what do people actually play around this music?**

YouTube Music and Last.fm both answer that. YouTube Music's radio queue is
Google's own "up next", derived from real play sequences. Last.fm's similar
tracks come from scrobbles. Neither is ranked by mood, and neither needs to be —
mood comes from YouTube Music's curated pools, where a human already decided
what belongs in *Sad* or *Feel good*.

## Why it fills segments instead of picking next songs

The obvious way to build a playlist from co-listening data is greedy: take the
seed, ask what people play next, pick one, ask again. That is wrong twice over.
Every pick is a separate round trip and a separate model call, so a thirty
minute playlist is ten sequential calls. Worse, each pick only sees the song
before it, so the playlist drifts — ten hops of "reasonable next song" ends up
somewhere nobody asked for. Telephone, with a bill attached.

So nothing picks a next song. It fills a segment:

| step | what happens | model calls |
|---|---|---|
| **plan** | the request becomes 1–4 mood segments, with the minutes divided between them | 1 |
| **gather** | every segment's candidates fetched at once, in parallel — the seed's radio and Last.fm neighbours, each segment's curated pool, each segment's tags | 0 |
| **weigh** | reciprocal rank fusion across the sources, then the library multiplier | 0 |
| **order** | one call per segment: here are 60 candidates, here is the brief, here is what the last block ended on — return an ordered block | 1 each |
| **push** | stitched and written to your account | 0 |

A three-segment shift is four calls, not ten, and they are the cheap kind:
judging a list, not writing one. Drift is gone for free, because the model sees
a whole block at once and the segment boundaries are the guardrails.

**The model is a quality layer, not load-bearing.** `--no-llm` skips the
ordering calls and takes the code ranking straight off the top; it still
produces a coherent playlist in about 18 seconds, because the ranking already
encodes co-listening frequency and your own library. Losing the model costs
taste, not function.

## Your library

Import from Apple Music, Spotify and YouTube Music. They merge on title+artist,
so a song you have in two services becomes one row carrying whatever each of
them knew.

```
python elo.py import apple ~/Desktop/Library.xml   # Music.app > File > Library > Export Library
python elo.py import spotify                       # browser sign-in, once
python elo.py import ytmusic                       # needs browser.json
python elo.py taste                                # the breakdown
```

| source | friction | what it gives |
|---|---|---|
| **Apple** | free, ten seconds, macOS | tracks, **genre**, year, **play count**, **skip count**, date added, rating, every playlist |
| **Spotify** | free dev app, ~3 min once | saved tracks, albums, playlists, followed + top artists, **artist genres** |
| **YouTube Music** | `ytmusicapi browser` | tracks, playlists, likes, subscriptions, videoIds |

Apple's export is by far the richest and needs no account at all — MusicKit
wants a $99/yr developer membership to tell you about a file already on your
disk. It is also the only source with **play counts**, the strongest preference
signal in the project, and the only one with a *negative* signal: a track you
skip more often than you finish is demoted everywhere.

Spotify's `audio-features` — valence, energy, danceability per track — was
deprecated in November 2024 with no replacement, so there is no mood number to
be had. That matters less than it sounds: what the ranker needs is not "this
track is 0.3 valence" but "Meek Mill is hip-hop", and the artist `genres` array
still answers that. Note that Spotify has rejected `localhost` redirect URIs
since April 2025 — it must be the loopback literal `http://127.0.0.1:8974/callback`,
and the error you get for `localhost` does not say so.

### Counting, not prompting

Working out that you own thirty-four songs by an artist and should therefore
hear them first is *counting*. The model never sees a "the user likes this
artist" instruction it might weigh or ignore — by the time the shortlist
reaches it, the ordering already reflects your library.

### The library is a source, not just a weight

This is the part that actually changes playlists. A multiplier can only reorder
tracks the sources already returned — if you own forty Meek Mill tracks and
YouTube Music's hip-hop pool happens to return none of them, every multiplier
in the world leaves the playlist Meek-Mill-free. So `taste.library_pool` runs
*alongside* radio and the curated pools, injecting the tracks you own that fit
each block, and fusion weighs them against everything else.

### Your playlists are co-occurrence data

Two tracks you filed together is a statement about what blends, made by the one
person whose taste this is supposed to match. It is used two ways.

As a **source**, `taste.playlist_pool` is rebuilt for every block against
everything picked so far — not fetched once from the seed. Block one asks "what
do you play with this song"; block three asks "what do you play with these
eight". As a **weight**, `cohesion` scores each candidate on how much of the
current playlist it already shares your shelves with.

Cohesion saturates on the *number of corroborations*, not on the share of what
is picked. Dividing by `len(chosen)` was the first version and it defeated the
point: sharing one playlist with one chosen track scored a perfect 1.0, pinning
the signal at maximum from the first block on. Counting instead makes the
evidence accumulate — one shared filing is a hint, four is a pattern:

```
picked so far          cohesion(Ima Boss)
Dreams and Nightmares        0.25
+ HUMBLE.                    0.50
+ B.M.F.                     0.75
+ Nonstop                    1.00
a set of folk tracks         0.00
```

Playlists over 300 tracks are ignored throughout. A "everything I like" dump
co-occurs everything with everything; deciding two tracks belong together is
only a statement when it was a choice.

### Recency

`Date Added` as a **percentile on your own timeline**, not an absolute age. A
library bulk-imported in 2015 would read as uniformly stale under exponential
decay and the signal would switch itself off; recency is only ever meaningful
relative to the rest of *your* library. Ties share a percentile, so the
thousand tracks a migration stamped with one date land in the middle together
instead of being ordered by accident. Undated tracks sit at 0.5 — not knowing
when you added something is not evidence that it is old.

It is two-sided, ×0.65 to ×1.35: what you saved last month is evidence about
who you are now, and what you saved in 2019 and never played since is evidence
too, pointing the other way. On the test library that separates two tracks with
near-identical play counts by more than 2×.

### Era

Every other signal here needs the track or the artist to already be in your
library. A release year is a fact about the *record*, so era is the one weight
that reaches music you have never heard — among fifty unknown radio tracks it
can prefer the ones from the decade you actually live in.

The catch is coverage. Measured across a real block:

| source | candidates | with a release year |
|---|---|---|
| YouTube Music radio | 50 | **50 (100%)** |
| YouTube Music curated playlists | 80 | 0 |
| YouTube Music mood shelves | 50 | 0 |
| Last.fm similar | 50 | 0 |

So under a tenth of a six-hundred-candidate block can be judged numerically. An
unknown year is therefore **neutral, never a penalty** — demoting nine tenths
of the pool because one source is chattier than the others would be an
accident, not a preference.

The other nine tenths are handled in words. The listener's decade profile goes
into the ordering prompt as one line — `this listener's library is 2010s 71%,
2020s 29%` — and the model applies its own knowledge of when records came out
to the candidates the data cannot reach. It is the cheapest possible fix: a
sentence in a prompt that was being sent anyway.

Adjacent decades count for half, so a 2009 record is not a different world from
a 2010 one and the boundary never falls between two songs from the same summer.
Below twelve dated tracks there is no distribution, only noise, and the signal
switches itself off rather than letting a handful of years declare an era
preference on your behalf.

### Weights are conditioned on the block

The obvious design is one global multiplier per artist. It is wrong in a way
that only shows up in a journey: it promotes Meek Mill in the *sleep* block
too. So every term is scaled by how well the track's genre fits *this* segment.

| evidence | weight, when the genre fits | when it does not |
|---|---|---|
| you liked the track | ×2.6 | ×1.4 |
| you have the track | ×2.2 | ×1.3 |
| you have *n* songs by the artist | up to ×1.9 | up to ×1.2 |
| you played it *n* times | up to ×1.6 | up to ×1.15 |
| you file it with what is already picked | up to ×1.8 | up to ×1.8 |
| when you added it, on your own timeline | ×0.65 – ×1.35 | ×0.65 – ×1.35 |
| it comes from a decade you live in | ×0.75 – ×1.25 | ×0.75 – ×1.25 |
| you skip it more than you finish it | ×0.6 | ×0.6 |

Fit has to reach the *track* terms, not just the artist term — gating only the
artist multiplier leaves "liked" and "played 140 times" segment-blind, and
those are the big ones. Measured on a hip-hop-heavy test library, the same
track scores ×5.07 in a hip-hop block and ×1.97 in a sleep block.

Three rules stay unconditional, and each for the same reason: none of them is a
claim about genre. Skipping something is a judgement that holds in every block.
When you added it is a fact about your timeline. Filing two tracks together is
a statement about those two tracks. A block being about sleep makes none of
them less true — and all three are small enough that an off-genre track cannot
ride them back into a block fit has already ruled out.

The one thing that is never unconditional is liking. Liking a track never buys
it into a block it does not fit — an earlier version exempted liked tracks from
the genre filter, which is how a sleep block filled up with the rap the
listener happens to love most.

### Discovery cap

With the library as both a source and a boost, a big library will happily fill
every block with music you already have, which is not a playlist — it is
shuffle with extra steps. At most 60% of each block may be tracks you own
(`--max-owned`, `1.0` to disable). The cap yields when holding it would leave a
block unable to fill its minutes: a block of your own songs beats a block of
two songs.

With nothing imported every multiplier is 1.0 and no library candidates are
injected, which degrades the tool to "well-recommended music by strangers"
rather than breaking it.

## Saying "not that one"

Playlists come out numbered, and the numbers are the handle:

```
$ elo.py "hip hop bangers, 20 minutes"
   1 * HUMBLE.        Kendrick Lamar   2:57   instant hard-hitting opener
   2 * Going Bad      Meek Mill        4:47   trap bounce, raises heat
   3   DNA.           Kendrick Lamar   3:06   aggressive Kendrick bars
   4 * 0 to 100       Drake            4:57   Drake flex, keeps tempo up
   5 * R.I.C.O.       Meek Mill        4:54   hardcore trap grit

$ elo.py no 2 4        # or  no 2-4  ·  no "sicko"  ·  yes 1
$ elo.py again
  dropped 2 you rejected before: Going Bad, 0 to 100
```

This is the only channel where you state a preference outright rather than
having it inferred, so it is the only one allowed to **remove** a candidate
instead of demoting it. A boost that politely lowers a song you explicitly
rejected is not a rejection, and a listener who has to say "not that one"
three times has been ignored twice.

### Scoped to the block, not the world

Rejecting a rap track in a *sleep* block does not mean "never play this" — it
means "not here". Ban it outright and one impatient tap in the wrong block
quietly deletes a song from your library forever, and you never find out why
it stopped appearing. So every verdict is filed against the block it happened
in:

| | in the block you rejected it | in any other block |
|---|---|---|
| rejected once | **removed** | ×0.4 |
| rejected in two different blocks | **removed** | **removed** |
| kept | ×1.6 | ×1.2 |

The widening is automatic and it is the listener's own doing: twice, in
unrelated contexts, is you telling us about the song rather than about the
block.

### Generalising without flinching

Track vetoes are precise and do not travel. Artists are where the useful
generalisation lives — three rejections out of four appearances says something
about that artist *in that mood*, and is worth applying to their tracks you
have never been shown. One rejection says nothing. So the artist term needs at
least three appearances before it does anything at all; the alternative is a
system that overreacts to noise and slowly narrows itself to a handful of
songs.

```
1 dropped / 3 kept  ->  x0.85
2 dropped / 2 kept  ->  x0.70
3 dropped / 1 kept  ->  x0.55
4 dropped / 0 kept  ->  x0.40
```

`elo.py feedback` shows everything learned; `elo.py forget "sicko"` or
`elo.py forget all` undoes it.

## Setup

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

`.env`, beside this file:

```
LASTFM_API_KEY=...        # https://www.last.fm/api/account/create
ANTHROPIC_API_KEY=...     # optional, see below
```

The model calls go through the Claude Code CLI you are already logged into, so
there is no separate bill. Set `ELO_BACKEND=api` to use the API instead — that
gets schema enforcement server-side, which is stricter, bills separately, and
runs about twice as fast because there is no CLI start-up per call.
`ELO_MODEL` / `ELO_API_MODEL` pick the model.

The API enforces a subset of JSON Schema. Measured against the live endpoint:
`enum`, `minLength`, `minItems`, nested objects and optional properties are
accepted; `minimum`, `maximum` and `maxItems` return a 400. `common._relax`
strips those three and restates each as a line in the prompt, so a bound the
server will not enforce stays a bound the model is told about rather than
silently disappearing.

Reading your YouTube Music library and creating playlists need credentials.
Everything else — radio, mood pools, Last.fm, Apple import — works
unauthenticated.

1. open <https://music.youtube.com> in your browser, logged in
2. developer tools → Network, filter for `/browse`, click a POST that returned 200
3. copy the request headers — Firefox: *Copy Value → Copy Request Headers*; Chrome: *Copy → Copy as fetch (Node.js)*
4. `python elo.py auth ytmusic`, paste, Ctrl-D

Both formats are understood — Chrome's "Copy as fetch" is a JSON blob rather
than a header list, and it is the option Chrome puts in front of you, so it is
unwrapped rather than rejected. `python elo.py auth` shows what is connected.

**Expired credentials do not raise, and that is the trap.** YouTube Music
answers a signed-out request with a perfectly valid page that simply has no
library in it, so ytmusicapi parses it happily and returns an empty list.
"Your session died months ago" and "you own no music" are the same value, and
an import that trusts it will cheerfully replace real data with nothing.

So `ytauth.health()` asks the one question that separates them: every response
carries a `logged_in` flag in `responseContext.serviceTrackingParams`, straight
from YouTube, independent of page layout and of whatever renderer they rename
next. `import ytmusic` and `--push` both check it first and refuse loudly.

Two related things worth knowing. The `Authorization: SAPISIDHASH` line in
`browser.json` is ignored — ytmusicapi recomputes it from the SAPISID cookie on
every request because the hash is timestamped, so a fresh-looking Authorization
header says nothing about whether the credentials work. And a paste that turns
out to be malformed or signed-out never overwrites working credentials: the old
file is kept aside, the new one is validated against the live flag, and the old
one is put back if it fails.

## Commands

```
elo.py "<request>"           build a playlist
       --push                create it in your YouTube Music account
       --title NAME          playlist name (default: the model picks one)
       --no-llm              skip the ordering calls, take the code ranking
       --wide N              expand from N of the strongest candidates (default 2)
       --max-per-artist N    default 2
       --max-owned F         max share of a block that may be music you own (0.6)
       --dry-run             print the plan and stop
       --json                also dump machine-readable output

elo.py no 3 5 | no 2-4 | no "sicko"      that one was wrong
elo.py yes 1                             that one was right
elo.py again                 rebuild the last request with feedback applied
elo.py last                  the last playlist, numbered
elo.py feedback              everything learned so far
elo.py forget [x]            undo one track's verdicts, or all of them
elo.py auth                  which services are connected
elo.py auth ytmusic [file]   paste your YouTube Music headers
elo.py import apple <file>   Music.app > File > Library > Export Library
elo.py import spotify        browser sign-in, once
elo.py import ytmusic        needs browser.json
elo.py taste                 the breakdown of what you imported
elo.py moods                 the 38 pools available

python plan.py "<request>"   just the plan
python -m unittest discover -s tests
```

## Files

```
elo.py       the CLI
plan.py      request -> segments                     (1 model call)
sources.py   YouTube Music radio, curated pools, Last.fm similar + tags
apple.py     a Music.app export -> your library      (0 calls)
spotify.py   the Spotify API    -> your library      (0 calls)
ytauth.py    YouTube Music credentials, and knowing when they have died
library.py   the store all three import into, and the merge rules
taste.py     your library -> candidates and weights  (0 calls)
feedback.py  "not that one", scoped to the block it was said in
blend.py     gather, weigh, order, stitch            (1 call per segment)
push.py      write the playlist to YouTube Music
common.py    env, cache, title matching, the model call
```

## Notes on the sources

`get_watch_playlist` and the mood/genre pages have no official API. ytmusicapi
reaches them by POSTing to the internal InnerTube endpoints the web player
uses. That is reverse-engineered, not sanctioned, and can break whenever Google
changes a payload. Last.fm is official, keyed and stable, and goes blind on a
lot of catalogue — which is why both are used, and why every source failure
costs a slot in the shortlist rather than the request.

One consequence is already visible: ytmusicapi's `get_mood_playlists` works on
the eleven mood categories and raises on all twenty-seven genre ones, because a
genre page leads with a shelf of songs rather than playlists. `sources._shelves`
walks the page itself instead. It is a smaller bet than it sounds — the endpoint
underneath is already unofficial, and the local parser skips shelves it does not
recognise instead of raising on them. The genre pages turn out to be the better
data anyway: fifty songs directly on the page plus a hundred and forty-five
playlists, against a mood page's few dozen playlists and no songs.
