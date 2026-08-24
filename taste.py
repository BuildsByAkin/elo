"""Your library, broken down into something the ranker can use.

Three jobs, in increasing order of how much they change the output.

1. THE BREAKDOWN. Who you own most of, what you actually play, which genres
   dominate, which decades, which artists you have whole albums of. Counting,
   not judgement, so no model is involved and none should be.

2. THE SEGMENT-AWARE BOOST. The obvious way to use affinity is one global
   multiplier per artist: own forty Meek Mill tracks, Meek Mill goes up. That
   is wrong in a way that only shows up in a journey — it also promotes him in
   the Sleep block. So the artist multiplier is scaled by how well that
   artist's tags fit *this* segment. Forty Meek Mill tracks is a huge boost in
   a hip-hop block and almost nothing in a sleep block, from the same number.

3. THE LIBRARY AS A CANDIDATE SOURCE. This is the one that matters most, and it
   is not a boost at all. A multiplier can only reorder tracks the sources
   already returned. If you own forty Meek Mill tracks and YouTube Music's
   hip-hop pool happens to return none of them, every multiplier in the world
   leaves the playlist Meek-Mill-free. So the library is fetched *as a source*
   alongside radio and the mood pools: for each segment we pull the tracks you
   own whose artists fit it, and let fusion weigh them against everything else.

   Your own playlists come in the same way. Two tracks you put in a playlist
   together is a statement about what blends, made by the one person whose
   taste this is supposed to match — better evidence than any stranger's
   co-listen, and nobody else has it.
"""
import json
import re
import sys

import common

# --------------------------------------------------------------- vocabulary
#
# Four services describe genre four ways: Apple writes "Hip-Hop/Rap", Spotify
# writes "atlanta hip hop", Last.fm writes "hip-hop", and YouTube Music calls
# the pool "Hip-hop". Matching them needs the phrase collapsed before it is
# split, because tokenising "R&B" first destroys it.

_PHRASES = [
    (r"\br\s*&\s*b\b|\brnb\b|\br\s+and\s+b\b|\brhythm and blues\b", " rnb "),
    (r"\bhip[\s\-]?hop\b|\brap\b", " hiphop "),
    (r"\bdrum\s*(and|n|&)\s*bass\b|\bdnb\b", " dnb "),
    (r"\brock\s*(and|n|&)\s*roll\b", " rocknroll "),
    (r"\bsinger[\s\-]?songwriter\b", " songwriter "),
    (r"\bneo[\s\-]?soul\b", " neosoul soul "),
    (r"\btrap\s+soul\b", " trapsoul trap soul "),
    (r"\bk[\s\-]?pop\b", " kpop "),
    (r"\bj[\s\-]?pop\b", " jpop "),
    (r"\blo[\s\-]?fi\b", " lofi "),
]

# Tokens that carry no genre information, including the Last.fm junk tags that
# dominate its tag clouds and would otherwise match everything.
_STOP = {"and", "the", "of", "a", "in", "my", "your", "music", "song", "songs",
         "seen", "live", "favourites", "favorites", "awesome", "best", "good",
         "great", "love", "loved", "beautiful", "amazing", "cool", "check",
         "out", "albums", "own", "all", "artists", "under", "more", "moments"}

# Modifiers, not genres. These attach to a real genre far more often than they
# stand alone ("alternative r&b", "classic soul", "dark trap"), and scoring
# them at full weight is how Bon Iver ends up matching an r&b block: the tag
# `alternative r&b` splits into two tokens and `alternative` hits. They are
# worth a quarter of a real match on their own.
_WEAK = {"alternative", "alt", "indie", "classic", "classics", "modern",
         "contemporary", "new", "old", "school", "deep", "dark", "smooth",
         "chill", "underground", "experimental", "progressive", "hard", "soft",
         "melodic", "vocal", "female", "male", "vocalists", "instrumental",
         "acoustic", "electronic", "pop", "urban", "guitar", "roots", "soft"}

# Families that should partially match each other. A shared family token is
# worth less than an exact tag match but more than nothing.
_FAMILY = {
    "hiphop": "urban", "rnb": "urban", "trap": "urban", "grime": "urban",
    "drill": "urban", "soul": "urban", "funk": "urban", "afrobeats": "urban",
    "rock": "guitar", "metal": "guitar", "punk": "guitar", "grunge": "guitar",
    "indie": "guitar", "alternative": "guitar", "shoegaze": "guitar",
    "house": "electronic", "techno": "electronic", "edm": "electronic",
    "dance": "electronic", "trance": "electronic", "dnb": "electronic",
    "electro": "electronic", "dubstep": "electronic", "garage": "electronic",
    # `quiet` and `classical` were one family once, which made folk a
    # defensible candidate for an opera block. They are both soft and that is
    # all they share.
    "acoustic": "quiet", "folk": "quiet", "songwriter": "quiet",
    "ambient": "quiet", "piano": "quiet", "lofi": "quiet", "chill": "quiet",
    "sleep": "quiet", "calm": "quiet",
    "classical": "classical", "opera": "classical", "orchestral": "classical",
    "baroque": "classical", "symphony": "classical",
    "country": "roots", "blues": "roots", "americana": "roots",
    "jazz": "roots", "gospel": "roots", "reggae": "roots",
}


def tokens(text):
    """Genre text from any of the four services -> a comparable token set."""
    s = " %s " % (text or "").lower()
    for pattern, repl in _PHRASES:
        s = re.sub(pattern, repl, s)
    words = {w for w in re.split(r"[^a-z0-9]+", s) if len(w) > 1}
    return {w for w in words if w not in _STOP}


def _expand(toks):
    return toks | {_FAMILY[t] for t in toks if t in _FAMILY}


# Words that name a kind of music, as opposed to a feeling. Used to tell
# "this block wants ambient and you own none" — where filtering the library to
# nothing is the correct answer — apart from "this block just says Sad", where
# it is a bug that silently switches personalisation off.
_GENRE_WORDS = set(_FAMILY) | set(_FAMILY.values()) | {
    "rnb", "hiphop", "dnb", "rocknroll", "songwriter", "neosoul", "trapsoul",
    "kpop", "jpop", "lofi", "disco", "soundtrack", "opera", "latin", "salsa",
    "reggaeton", "afrobeat", "amapiano", "bollywood", "gospel", "bluegrass",
    "emo", "hardcore", "synthpop", "newwave", "psychedelic", "ska", "dub"}


def names_a_genre(seg_tokens, extra_vocabulary=()):
    """Does this segment ask for a kind of music, or only for a feeling?"""
    vocab = _GENRE_WORDS | set(extra_vocabulary)
    return bool(seg_tokens & vocab)


def fit(artist_tags, segment_tokens):
    """How well a set of genre tags matches a segment, 0.0 to 1.0.

    A shared genre token is full credit; a shared modifier ("alternative",
    "dark") or a shared family ("both are guitar music") is quarter credit.
    Saturates at two real matches, because a third does not make Meek Mill any
    more hip-hop.
    """
    if not segment_tokens or not artist_tags:
        return 0.0
    a = tokens(",".join(artist_tags) if not isinstance(artist_tags, str)
               else artist_tags)
    shared = a & segment_tokens
    strong = len(shared - _WEAK)
    weak = len(shared & _WEAK)
    family = len(_expand(a) & _expand(segment_tokens)) - len(shared)
    return min(1.0, (strong + 0.25 * (weak + max(0, family))) / 2.0)


def segment_tokens(segment):
    """The words a segment is asking for, from its mood, genres and tags.

    Returns an empty set when the segment names no genre at all — a pure "Sad"
    block with no other hint. That is a real state and the caller treats it as
    "cannot discriminate", not as "nothing matches": with no genre signal every
    artist should be weighed the same rather than every artist scoring zero.
    """
    if not segment:
        return set()
    parts = [segment.get("mood", "")]
    parts += list(segment.get("genres") or [])
    parts += list(segment.get("tags") or [])
    return tokens(" ".join(parts))


# ------------------------------------------------------------------- the model

class Taste(object):
    """The whole library, in the shape the ranker needs, loaded once."""

    def __init__(self, tracks, artists, tags, playlists, by_track,
                 recency=None, eras=None):
        self.tracks = tracks          # key -> dict
        self.artists = artists        # artist key -> dict (incl. 'tags')
        self.tags = tags              # tag -> share of the library, 0..1
        self.playlists = playlists    # playlist id -> {track keys}
        self.by_track = by_track      # track key -> {playlist ids}
        self.recency = recency or {}  # track key -> 0.0 oldest .. 1.0 newest
        self.eras = eras or {}        # "2010s" -> share of the library, 0..1

    def __bool__(self):
        return bool(self.tracks or self.artists)

    __nonzero__ = __bool__

    def artist(self, credit):
        """Best row across a multi-artist credit — a feature on a track by
        someone you own plenty of still counts."""
        best = None
        for part in str(credit or "").split(","):
            row = self.artists.get(common.norm(part))
            if row and (best is None or row["tracks"] > best["tracks"]):
                best = row
        return best

    def era_fit(self, year):
        """How much a release year looks like the music you keep, 0.0 to 1.0.

        Returns None — meaning "no opinion", never a penalty — when the year is
        unknown or the library has too few dated tracks to have a shape. That
        matters more here than anywhere else in this file: only YouTube Music's
        radio queue reports a release year at all. The curated pools and
        Last.fm both omit it, so on a typical six-hundred-candidate block fewer
        than one in ten rows can be judged, and the rest must be left exactly
        where they were rather than quietly pushed below the ones we could
        read.

        Adjacent decades count for half. A 2009 record is not a different
        world from a 2010 one, and a listener whose library is all 2010s should
        not have the boundary fall between two songs from the same summer.
        """
        if not self.eras:
            return None
        y = str(year or "")[:4]
        if not y.isdigit() or len(y) != 4:
            return None
        d = int(y) // 10 * 10
        best = max(self.eras.values())
        if not best:
            return None
        score = (self.eras.get("%ds" % d, 0.0)
                 + 0.5 * self.eras.get("%ds" % (d - 10), 0.0)
                 + 0.5 * self.eras.get("%ds" % (d + 10), 0.0))
        return min(1.0, score / best)

    def fresh(self, key):
        """Where this track sits on your own timeline, 0 oldest to 1 newest.

        A percentile rather than an age, because absolute decay reads a library
        that was bulk-imported in 2015 as uniformly stale and switches the
        signal off entirely. Recency is only ever meaningful relative to the
        rest of *your* library. Undated tracks get 0.5: not knowing when you
        added something is not evidence that it is old.
        """
        return self.recency.get(key, 0.5)

    def curated(self):
        """Your playlists, minus the ones too big to mean anything.

        A five-hundred-track "everything I like" list co-occurs everything with
        everything and would drown the signal in noise. Deciding two tracks
        belong together is only a statement when it was a choice.
        """
        return {pid: m for pid, m in self.playlists.items()
                if len(m) <= MAX_PLAYLIST}

    def neighbours(self, keys):
        """Tracks sharing one of your playlists with any of `keys`.

        Returns {track key: how many of your playlists it shares with them},
        excluding the seeds themselves.
        """
        if isinstance(keys, str):
            keys = [keys]
        keys = set(keys)
        curated = self.curated()
        out = {}
        for k in keys:
            for pid in self.by_track.get(k, ()):
                for other in curated.get(pid, ()):
                    if other not in keys:
                        out[other] = out.get(other, 0) + 1
        return out

    def cohesion(self, key, chosen):
        """How strongly this track lives in your playlists with what is
        already picked, 0.0 to 1.0.

        This is the generalisation of "shares a playlist with the seed". The
        seed is one track and one opinion; by the third block there are eight
        tracks on the table and the question worth asking is whether you have
        filed this candidate alongside *them*, plural.

        The count is deliberately NOT divided by how much has been picked. That
        was the first version and it was wrong in a way that defeated the whole
        point: dividing by `len(chosen)` means sharing one playlist with one
        chosen track scores a perfect 1.0, and the signal is pinned at maximum
        from the first block onward. Saturating on the raw number of
        corroborations instead is what actually makes the evidence accumulate —
        one shared filing is a hint, four is a pattern.
        """
        if not chosen or not self.by_track:
            return 0.0
        curated = self.curated()
        hits = 0
        for pid in self.by_track.get(key, ()):
            hits += len(curated.get(pid, ()) & chosen)
        return min(1.0, hits / float(COHESION_SATURATES_AT))

    def pairs(self, top=10):
        """The tracks you most often file together, for the breakdown."""
        co = {}
        for members in self.curated().values():
            ordered = sorted(members)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    co[(a, b)] = co.get((a, b), 0) + 1
        return sorted(((n, a, b) for (a, b), n in co.items() if n > 1),
                      reverse=True)[:top]


def load():
    con = common.connect()
    tags_by_artist = {k: [t for t in v.split(",") if t]
                      for k, v in con.execute("SELECT key,tags FROM artist_tags")}
    tracks = {}
    for row in con.execute(
            "SELECT key,title,artist,album,genre,year,seconds,plays,skips,"
            "rating,liked,added,video_id FROM library_tracks"):
        tracks[row[0]] = dict(zip(
            ("key", "title", "artist", "album", "genre", "year", "seconds",
             "plays", "skips", "rating", "liked", "added", "video_id"), row))
    artists = {}
    for row in con.execute(
            "SELECT key,name,tracks,albums,playlists,plays,liked,subscribed"
            " FROM library_artists"):
        a = dict(zip(("key", "name", "tracks", "albums", "playlists", "plays",
                      "liked", "subscribed"), row))
        a["tags"] = tags_by_artist.get(a["key"], [])
        artists[a["key"]] = a

    # Tag shares are weighted by how many of that artist's tracks you have, so
    # one album by a jazz artist does not read as loudly as sixty rap tracks.
    weight = {}
    for a in artists.values():
        for i, t in enumerate(a["tags"][:6]):
            weight[t] = weight.get(t, 0.0) + a["tracks"] / float(i + 1)
    total = sum(weight.values()) or 1.0
    tags = {t: w / total for t, w in
            sorted(weight.items(), key=lambda kv: -kv[1])}

    playlists, by_track = {}, {}
    for pid, k in con.execute(
            "SELECT playlist_id, track_key FROM library_playlist_tracks"):
        playlists.setdefault(pid, set()).add(k)
        by_track.setdefault(k, set()).add(pid)
    return Taste(tracks, artists, tags, playlists, by_track,
                 _recency(tracks), _eras(tracks))


def _eras(tracks, minimum=12):
    """Your library's decade distribution, as shares summing to 1.

    Below `minimum` dated tracks there is no distribution, only noise, and a
    handful of years should not be allowed to declare an era preference on a
    listener's behalf. An empty result switches the whole signal off.
    """
    counts = {}
    for t in tracks.values():
        y = str(t.get("year") or "")[:4]
        if y.isdigit() and len(y) == 4:
            d = "%ds" % (int(y) // 10 * 10)
            counts[d] = counts.get(d, 0) + 1
    total = sum(counts.values())
    if total < minimum:
        return {}
    return {d: n / float(total) for d, n in counts.items()}


def _recency(tracks):
    """Rank every dated track on your own timeline, 0.0 oldest to 1.0 newest.

    Ties share a percentile, which is what makes this survive the shape most
    real libraries have: a bulk import puts thousands of tracks on one date,
    and they should all land in the middle together rather than being ordered
    by whatever the dictionary happened to do.
    """
    dated = sorted((t["added"], k) for k, t in tracks.items() if t["added"])
    if len(dated) < 2:
        return {}
    out, i, n = {}, 0, len(dated)
    while i < len(dated):
        j = i
        while j < len(dated) and dated[j][0] == dated[i][0]:
            j += 1
        share = (i + j - 1) / 2.0 / (n - 1)     # midpoint rank of the tie
        for _, k in dated[i:j]:
            out[k] = share
        i = j
    return out


def invalidate():
    con = common.connect()
    con.execute("DELETE FROM meta WHERE k='profile'")
    con.commit()


# -------------------------------------------------------------------- boost

W_LIKED = 2.6
W_OWNED = 2.2
W_SUBSCRIBED = 1.3
W_ARTIST_MAX = 0.9          # a heavily-owned, well-fitting artist tops out here
W_PLAYS_MAX = 0.6           # and a heavily-played track adds this on top
W_COHESION = 0.8            # you file it with what is already on the playlist
W_RECENT = 0.35             # two-sided: newest x1.35, oldest x0.65
W_ERA = 0.25                # two-sided: your decade x1.25, a foreign one x0.75
W_SKIPPED = 0.6             # you skip it more than you finish it
FIT_FLOOR = 0.25            # what an off-genre owned artist still keeps
ARTIST_SATURATES_AT = 10
PLAYS_SATURATE_AT = 40
COHESION_SATURATES_AT = 4   # shared filings before the evidence is conclusive
MAX_PLAYLIST = 300          # bigger than this is a dumping ground, not a choice


def track_fit(track, artist_row, seg):
    """How well one owned track fits a segment.

    Prefers the track's own genre, which Apple ships per track, over the
    artist's aggregate tags — an artist with a jazz record and a rap record
    should not have both count equally in both blocks.
    """
    if not seg:
        return 1.0                       # no signal: decline to discriminate
    if track and track.get("genre"):
        return fit(track["genre"], seg)
    return fit(artist_row["tags"], seg) if artist_row else 0.0


def boost(cands, taste, segment=None, chosen=None):
    """Apply affinity in place, conditioned on the segment. Never filters.

    Multiplicative on the fused rank so it scales with how well-placed a track
    already was: it promotes a strong candidate you have a connection to above
    an equally strong one you do not, and cannot drag a track the sources
    barely returned up to the top on taste alone.

    The genre-fit terms are scaled by fit, not just the artist term. That is a
    correction to the obvious design: gating only the artist multiplier leaves
    the track multipliers — owned, liked, played four hundred times —
    completely segment-blind, and they are the big ones. A liked,
    heavily-played rap track would then still tower over everything in a sleep
    block, having merely lost a little of its artist bonus. Fit has to reach
    all the way down or it does not really reach at all.

    Recency and cohesion are deliberately NOT scaled by fit, because neither is
    a claim about genre. When you added something is a fact about your
    timeline, and filing two tracks in a playlist together is a statement about
    those two tracks; a segment being about sleep does not make either less
    true. They are also both small, so an off-genre track cannot ride them back
    into a block that fit has already ruled out.
    """
    seg = segment_tokens(segment)
    chosen = set(chosen or ())
    for c in cands:
        w, why = 1.0, []
        k = c.get("key") or common.key(c["title"], c["artist"])
        track = taste.tracks.get(k)
        a = taste.artist(c["artist"])
        f = track_fit(track, a, seg) if (track or a) else 0.0
        # How much of a bonus survives when the genre is wrong for this block.
        keep = FIT_FLOOR + (1.0 - FIT_FLOOR) * f

        if track:
            base = W_LIKED if (track["liked"] or track["rating"] >= 80) \
                else W_OWNED
            w *= 1.0 + (base - 1.0) * keep
            why.append("liked" if base == W_LIKED else "in library")
            if track["plays"]:
                w *= 1.0 + W_PLAYS_MAX * keep * min(
                    track["plays"], PLAYS_SATURATE_AT) / float(
                        PLAYS_SATURATE_AT)
                why.append("%d plays" % track["plays"])
            # The only negative signal any service gives us. Skipping something
            # more often than you finish it is a judgement, and it is yours —
            # and unlike the positive signals it is NOT scaled by fit, because
            # disliking a song is true in every block.
            if track["skips"] > max(1, track["plays"]):
                w *= W_SKIPPED
                why.append("you skip it")
            # When you added it, relative to the rest of your library. Two
            # sided on purpose: what you saved last month is evidence about
            # who you are now, and what you saved in 2014 and never played
            # since is evidence too, pointing the other way.
            r = taste.fresh(k)
            if r != 0.5:
                w *= 1.0 + W_RECENT * (r - 0.5) * 2.0
                why.append("added recently" if r > 0.7 else
                           ("an old save" if r < 0.3 else ""))
            if not c.get("video_id") and track["video_id"]:
                c["video_id"] = track["video_id"]
            if not c.get("secs") and track["seconds"]:
                c["secs"] = track["seconds"]

        if a and a["tracks"]:
            depth = min(a["tracks"], ARTIST_SATURATES_AT) / float(
                ARTIST_SATURATES_AT)
            w *= 1.0 + W_ARTIST_MAX * depth * keep
            why.append("%d by %s" % (a["tracks"], a["name"]))
        if seg and (track or a) and f < 0.25:
            why.append("off-genre")
        if a and a["subscribed"]:
            w *= W_SUBSCRIBED
            why.append("subscribed")

        coh = taste.cohesion(k, chosen)
        if coh:
            w *= 1.0 + W_COHESION * coh
            why.append("you file it with these")

        # Era is the only signal here that reaches music you do not own. Every
        # other term needs the track or the artist to already be in your
        # library; a release year is a fact about the record, so a stranger's
        # song can be judged on whether it comes from the decade you live in.
        # `None` means unknown, which is neutral — the sources that omit the
        # year outnumber the one that reports it by ten to one, and demoting
        # everything they return would be an accident, not a preference.
        era = taste.era_fit(c.get("year") or (track or {}).get("year"))
        if era is not None:
            w *= 1.0 + W_ERA * (era - 0.5) * 2.0
            if era > 0.8:
                why.append("your era")
            elif era < 0.2:
                why.append("outside your era")

        c["weight"] = w
        c["cohesion"] = coh
        c["aff"] = ", ".join(x for x in why if x)
        c["rank_score"] = c.get("rrf", 0.0) * w
    cands.sort(key=lambda c: -c["rank_score"])
    return cands


# ---------------------------------------------------- the library as a source

def library_pool(taste, segment, limit=60):
    """Owned tracks that fit this segment, as candidates rather than a boost.

    Ranked by how much you have of the artist, how well they fit, and how much
    you have played the track. Returned in the same shape as any other source
    so fusion can weigh it against radio and the curated pools — it competes,
    it does not override.
    """
    if not taste.tracks:
        return []
    seg = segment_tokens(segment)
    # A block can name a feeling and no genre at all — "Sad", no tags. Nothing
    # in the library will match those words, so filtering on fit would drop
    # every track and quietly switch personalisation off for that block. But
    # "Sleep, ambient" naming a genre this listener owns none of SHOULD filter
    # to nothing. The two look identical from the library's side — both
    # intersect it emptily — so the question has to be asked of a genre
    # vocabulary instead: did this segment name a kind of music at all?
    vocab = set()
    for a in taste.artists.values():
        if a["tags"]:
            vocab |= tokens(",".join(a["tags"]))
    actionable = names_a_genre(seg, vocab)
    scored = []
    for k, t in taste.tracks.items():
        a = taste.artist(t["artist"])
        depth = min(a["tracks"], ARTIST_SATURATES_AT) / 10.0 if a else 0.0
        f = track_fit(t, a, seg) if actionable else 0.5
        # Liking a track does NOT buy it a place in a block it does not fit.
        # An earlier version let liked tracks skip this check, which is how a
        # sleep block filled up with the rap the listener happens to love most.
        if actionable and f <= 0.0:
            continue
        plays = min(t["plays"], PLAYS_SATURATE_AT) / float(PLAYS_SATURATE_AT)
        scored.append((f * 2.0 + depth + plays + 0.5 * t["liked"]
                       + (taste.fresh(k) - 0.5), k, t))
    scored.sort(key=lambda s: -s[0])

    out = []
    for i, (_, k, t) in enumerate(scored[:limit]):
        out.append({"title": t["title"], "artist": t["artist"],
                    "album": t["album"], "video_id": t["video_id"],
                    "secs": t["seconds"], "rank": i + 1, "score": None,
                    "source": "library", "key": k})
    return out


def playlist_pool(taste, chosen, limit=40):
    """Tracks you have filed alongside everything already on the playlist.

    Recomputed per block rather than fetched once from the seed, so it widens
    as the playlist fills: block one asks "what do you play with this song",
    block three asks "what do you play with these eight". Ranked by how many of
    your playlists back the connection, then by recency — a pairing you made
    last month beats the same pairing made in 2014.
    """
    if not taste.by_track or not chosen:
        return []
    shared = taste.neighbours(chosen)
    ranked = sorted(shared.items(),
                    key=lambda kv: (-kv[1], -taste.fresh(kv[0])))
    out = []
    for k, n in ranked:
        t = taste.tracks.get(k)
        if not t:
            continue
        out.append({"title": t["title"], "artist": t["artist"],
                    "album": t["album"], "video_id": t["video_id"],
                    "secs": t["seconds"], "rank": len(out) + 1, "score": None,
                    "source": "yours", "key": k})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ breakdown

def era_hint(taste, floor=0.15):
    """One line describing the listener's decades, for the ordering prompt.

    The numeric era weight can only judge the tracks that arrive with a year,
    and only YouTube Music's radio does that — under a tenth of a block's
    candidates. The model, however, knows roughly when records came out. So the
    same preference is stated once in words and applied by the reader to the
    nine tenths the data cannot reach. It is the cheapest possible fix: a
    sentence in a prompt that was being sent anyway.
    """
    if not taste or not taste.eras:
        return ""
    big = [(d, s) for d, s in sorted(taste.eras.items(), key=lambda kv: -kv[1])
           if s >= floor]
    if not big:
        return ""
    return ", ".join("%s %d%%" % (d, round(s * 100)) for d, s in big)


def profile(taste=None, top=12):
    """The counted breakdown, for `elo.py taste`."""
    taste = load() if taste is None else taste
    con = common.connect()
    n_tracks = len(taste.tracks)
    if not n_tracks:
        return None

    by_tracks = sorted(taste.artists.values(), key=lambda a: -a["tracks"])
    by_plays = sorted((a for a in taste.artists.values() if a["plays"]),
                      key=lambda a: -a["plays"])
    by_playlist = sorted((a for a in taste.artists.values() if a["playlists"]),
                         key=lambda a: -a["playlists"])
    played = sorted((t for t in taste.tracks.values() if t["plays"]),
                    key=lambda t: -t["plays"])

    decades = {}
    for t in taste.tracks.values():
        y = t["year"][:4]
        if y.isdigit() and len(y) == 4:
            decades["%ds" % (int(y) // 10 * 10)] = decades.get(
                "%ds" % (int(y) // 10 * 10), 0) + 1

    # Who you have been adding lately, weighted by how recent rather than
    # counted over an arbitrary last-N window.
    recent_artists = {}
    for k, t in taste.tracks.items():
        r = taste.fresh(k)
        if r <= 0.75:
            continue
        a = taste.artist(t["artist"])
        if a:
            recent_artists[a["name"]] = recent_artists.get(a["name"], 0) + 1
    dated = [t for t in taste.tracks.values() if t["added"]]
    span = ("%s to %s" % (min(t["added"] for t in dated),
                          max(t["added"] for t in dated))) if dated else ""

    sources = {}
    for row in con.execute("SELECT sources FROM library_tracks"):
        for s in (row[0] or "").split(","):
            if s:
                sources[s] = sources.get(s, 0) + 1

    return {
        "tracks": n_tracks,
        "artists": len(taste.artists),
        "liked": sum(1 for t in taste.tracks.values() if t["liked"]),
        "playlists": len(taste.playlists),
        "sources": sources,
        "total_plays": sum(t["plays"] for t in taste.tracks.values()),
        "tagged": sum(1 for a in taste.artists.values() if a["tags"]),
        "top_by_tracks": [(a["name"], a["tracks"], ", ".join(a["tags"][:3]))
                          for a in by_tracks[:top]],
        "top_by_plays": [(a["name"], a["plays"]) for a in by_plays[:top]],
        "top_by_playlists": [(a["name"], a["playlists"])
                             for a in by_playlist[:top]],
        "top_tracks": [(t["title"], t["artist"], t["plays"])
                       for t in played[:top]],
        "tags": list(taste.tags.items())[:top],
        "decades": sorted(decades.items()),
        "recent": sorted(recent_artists.items(), key=lambda kv: -kv[1])[:8],
        "dated": len(dated),
        "span": span,
        "pairs": [(n, taste.tracks[a]["title"], taste.tracks[a]["artist"],
                   taste.tracks[b]["title"], taste.tracks[b]["artist"])
                  for n, a, b in taste.pairs(top)
                  if a in taste.tracks and b in taste.tracks],
        # Pairs need a track to appear in two of your playlists *together*,
        # which is striking when it happens and absent in most libraries. Per
        # track it is dense and always available, so report that too.
        "anchors": sorted(
            ((len(pids), taste.tracks[k]["title"], taste.tracks[k]["artist"])
             for k, pids in taste.by_track.items()
             if k in taste.tracks and len(pids) > 1), reverse=True)[:top],
    }


def show(p, out=sys.stderr):
    if not p:
        print("nothing imported yet — run: python elo.py import apple <file>",
              file=out)
        return
    src = ", ".join("%s %d" % (k, v) for k, v in sorted(p["sources"].items()))
    print("\n%d tracks · %d artists · %d liked · %d playlists   [%s]"
          % (p["tracks"], p["artists"], p["liked"], p["playlists"], src),
          file=out)
    if p["total_plays"]:
        print("%d plays recorded" % p["total_plays"], file=out)
    print("%d of %d artists have genre tags" % (p["tagged"], p["artists"]),
          file=out)

    if p["tags"]:
        print("\nyour sound", file=out)
        for tag, share in p["tags"]:
            bar = "#" * max(1, int(share * 60))
            print("  %-22s %5.1f%%  %s" % (tag[:22], share * 100, bar),
                  file=out)
    print("\nmost of                        ", file=out)
    for name, n, tags in p["top_by_tracks"]:
        print("  %4d  %-28s %s" % (n, name[:28], tags), file=out)
    if p["top_by_plays"]:
        print("\nmost played", file=out)
        for name, n in p["top_by_plays"]:
            print("  %4d  %s" % (n, name), file=out)
    if p["top_tracks"]:
        print("\ntop tracks", file=out)
        for title, artist, n in p["top_tracks"]:
            print("  %4d  %-38s %s" % (n, title[:38], artist[:26]), file=out)
    if p["top_by_playlists"]:
        print("\nmost playlisted", file=out)
        for name, n in p["top_by_playlists"]:
            print("  %4d  %s" % (n, name), file=out)
    if p["anchors"]:
        print("\nacross the most of your playlists", file=out)
        for n, title, artist in p["anchors"]:
            print("  %4d  %-38s %s" % (n, title[:38], artist[:26]), file=out)
    if p["pairs"]:
        print("\nyou file these two together, repeatedly", file=out)
        for n, at, aa, bt, ba in p["pairs"]:
            print("  %2d playlists  %-26s + %s"
                  % (n, "%s — %s" % (at[:14], aa[:10]),
                     "%s — %s" % (bt[:14], ba[:10])), file=out)
    if p["decades"]:
        print("\ndecades   " + "  ".join("%s %d" % d for d in p["decades"]),
              file=out)
    if p["dated"]:
        print("added     %d dated, %s" % (p["dated"], p["span"]), file=out)
    if p["recent"]:
        print("lately    " + ", ".join("%s (%d)" % r for r in p["recent"]),
              file=out)


def main():
    show(profile(), out=sys.stdout)


if __name__ == "__main__":
    main()
