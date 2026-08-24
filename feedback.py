"""Saying "not that one", and having it mean something next time.

Everything else in this project infers what you want. This is the one channel
where you say it outright, so it is the strongest signal here and the only one
allowed to remove a track outright rather than merely reweigh it. A boost that
politely demotes a song you explicitly rejected is not a rejection, and a
listener who has to say "not that one" three times has been ignored twice.

CONTEXT, NOT BLANKET BANS

    The rule the rest of this codebase already learned the hard way applies
    here with more force. Rejecting a rap track in a sleep block does not mean
    "never play this"; it means "not here". Ban it outright and one impatient
    tap in the wrong block quietly deletes a song from your library forever,
    and you will never find out why it stopped appearing.

    So every verdict is filed against the block it happened in, and a veto is
    scoped to that mood. Reject the same track in two *different* moods and the
    scope widens on its own — twice, in unrelated contexts, is you telling us
    about the song rather than about the block.

GENERALISING WITHOUT OVERREACTING

    Track vetoes are precise and do not travel. Artists are where the useful
    generalisation lives: three rejections out of four appearances is a
    statement about the artist in that mood, and worth acting on for tracks by
    them you have never been shown. One rejection is not. Hence a minimum
    sample before the artist term does anything at all — the alternative is a
    system that flinches at noise and slowly narrows itself to nothing.
"""
import sys
import time

import common

# A track vetoed in the mood it was rejected in. In other moods a single
# rejection only demotes, because you were rejecting it *there*.
W_ELSEWHERE = 0.4
W_KEPT_HERE = 1.6
W_KEPT_ELSEWHERE = 1.2
W_ARTIST_MAX = 0.6          # a fully-rejected artist bottoms out at x0.4 here
ARTIST_MIN_SAMPLE = 3       # appearances before an artist verdict counts
VETO_ACROSS_MOODS = 2       # distinct moods before a veto goes global


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def remember(blocks, request):
    """Save the playlist just built so `no 3` has something to point at."""
    con = common.connect()
    con.execute("DELETE FROM last_playlist")
    pos = 0
    for b in blocks:
        seg = b["segment"]
        for c in b["tracks"]:
            pos += 1
            con.execute(
                "INSERT INTO last_playlist (pos,track_key,title,artist,mood,"
                "tags,request,at) VALUES (?,?,?,?,?,?,?,?)",
                (pos, c.get("key") or common.key(c["title"], c["artist"]),
                 c["title"], c["artist"], seg.get("mood", ""),
                 ",".join(seg.get("tags") or []), request, _now()))
    con.commit()
    return pos


def last():
    con = common.connect()
    return [dict(zip(("pos", "track_key", "title", "artist", "mood", "tags",
                      "request", "at"), r))
            for r in con.execute(
                "SELECT pos,track_key,title,artist,mood,tags,request,at"
                " FROM last_playlist ORDER BY pos")]


def _resolve(rows, args):
    """Accept positions (`3`, `2-4`) or a substring of the title or artist."""
    if not rows:
        sys.exit("no playlist to give feedback on — build one first")
    by_pos = {r["pos"]: r for r in rows}
    out, missing = [], []
    for a in args:
        a = a.strip()
        if not a:
            continue
        if a.lower() == "all":
            out += rows
            continue
        if "-" in a and all(p.strip().isdigit() for p in a.split("-", 1)):
            lo, hi = (int(p) for p in a.split("-", 1))
            got = [by_pos[p] for p in range(lo, hi + 1) if p in by_pos]
            out += got or []
            if not got:
                missing.append(a)
            continue
        if a.isdigit():
            if int(a) in by_pos:
                out.append(by_pos[int(a)])
            else:
                missing.append(a)
            continue
        needle = common.norm(a)
        hits = [r for r in rows
                if needle and (needle in common.norm(r["title"])
                               or needle in common.norm(r["artist"]))]
        if hits:
            out += hits
        else:
            missing.append(a)
    if missing:
        sys.exit("no track matched: %s\n  the last playlist had %d tracks; "
                 "run `python elo.py last` to see them"
                 % (", ".join(missing), len(rows)))
    seen, uniq = set(), []
    for r in out:
        if r["pos"] not in seen:
            seen.add(r["pos"])
            uniq.append(r)
    return uniq


def record(args, verdict, quiet=False):
    """File a verdict against tracks from the last playlist."""
    rows = _resolve(last(), args)
    con = common.connect()
    for r in rows:
        con.execute(
            "INSERT INTO feedback (track_key,title,artist,verdict,mood,tags,"
            "request,at) VALUES (?,?,?,?,?,?,?,?)",
            (r["track_key"], r["title"], r["artist"], verdict, r["mood"],
             r["tags"], r["request"], _now()))
    con.commit()
    if not quiet:
        word = "dropped" if verdict < 0 else "kept"
        for r in rows:
            print("  %s  %s — %s   (in the %s block)"
                  % (word, r["title"], r["artist"], r["mood"] or "?"),
                  file=sys.stderr)
    return rows


class Learned(object):
    """Every verdict so far, indexed for the ranker."""

    def __init__(self, tracks, artists):
        self.tracks = tracks      # track key -> {mood: net verdict}
        self.artists = artists    # artist key -> {mood: [rejects, keeps]}

    def __bool__(self):
        return bool(self.tracks or self.artists)

    __nonzero__ = __bool__

    def vetoed(self, key, mood):
        """Should this track be removed outright, and why?

        Only ever for an explicit rejection: in the block it was rejected in,
        or anywhere once it has been rejected in two unrelated blocks.
        """
        by_mood = self.tracks.get(key)
        if not by_mood:
            return None
        rejected_in = [m for m, v in by_mood.items() if v < 0]
        if not rejected_in:
            return None
        if len(rejected_in) >= VETO_ACROSS_MOODS:
            return "you rejected this in %d different blocks" % len(rejected_in)
        if mood in rejected_in:
            return "you rejected this in a %s block" % mood
        return None

    def weight(self, key, artist, mood):
        """A multiplier and a reason, for everything short of a veto."""
        w, why = 1.0, []
        by_mood = self.tracks.get(key) or {}
        here = by_mood.get(mood, 0)
        elsewhere = sum(v for m, v in by_mood.items() if m != mood)
        if here > 0:
            w *= W_KEPT_HERE
            why.append("you kept this here before")
        elif elsewhere > 0:
            w *= W_KEPT_ELSEWHERE
            why.append("you kept this before")
        if here == 0 and elsewhere < 0:
            w *= W_ELSEWHERE
            why.append("you rejected this elsewhere")

        stats = (self.artists.get(common.norm(_first(artist))) or {})
        rej, keep = stats.get(mood, (0, 0))
        total = rej + keep
        if total >= ARTIST_MIN_SAMPLE and rej:
            rate = rej / float(total)
            w *= 1.0 - W_ARTIST_MAX * rate
            why.append("you drop %d of %d from this artist here" % (rej, total))
        return w, ", ".join(why)


def _first(credit):
    return str(credit or "").split(",")[0]


def load():
    con = common.connect()
    tracks, artists = {}, {}
    for key, artist, verdict, mood in con.execute(
            "SELECT track_key, artist, verdict, mood FROM feedback"):
        tracks.setdefault(key, {})
        tracks[key][mood] = tracks[key].get(mood, 0) + verdict
        ak = common.norm(_first(artist))
        if ak:
            slot = artists.setdefault(ak, {})
            rej, keep = slot.get(mood, (0, 0))
            slot[mood] = ((rej + 1, keep) if verdict < 0 else (rej, keep + 1))
    return Learned(tracks, artists)


def apply(cands, learned, segment, quiet=True):
    """Veto and reweigh in place. Returns the vetoed candidates.

    Unlike affinity, this is allowed to remove things. An explicit "not that
    one" that only demotes is not a rejection, and the track resurfacing next
    week reads as the tool ignoring you.
    """
    if not learned:
        return []
    mood = (segment or {}).get("mood", "")
    kept, dropped = [], []
    for c in cands:
        key = c.get("key") or common.key(c["title"], c["artist"])
        veto = learned.vetoed(key, mood)
        if veto:
            c["vetoed"] = veto
            dropped.append(c)
            continue
        w, why = learned.weight(key, c["artist"], mood)
        if w != 1.0:
            c["weight"] = c.get("weight", 1.0) * w
            c["rank_score"] = c.get("rank_score", c.get("rrf", 0.0)) * w
            c["aff"] = ", ".join(x for x in (c.get("aff"), why) if x)
        kept.append(c)
    cands[:] = sorted(kept, key=lambda c: -c.get("rank_score", 0.0))
    return dropped


# ------------------------------------------------------------------- report

def summary():
    con = common.connect()
    rows = list(con.execute(
        "SELECT title, artist, verdict, mood, at FROM feedback"
        " ORDER BY id DESC"))
    if not rows:
        return None
    drops = [r for r in rows if r[2] < 0]
    keeps = [r for r in rows if r[2] > 0]
    by_artist = {}
    for title, artist, verdict, mood, at in rows:
        a = _first(artist).strip()
        rej, keep = by_artist.get(a, (0, 0))
        by_artist[a] = (rej + 1, keep) if verdict < 0 else (rej, keep + 1)
    return {"drops": drops, "keeps": keeps, "recent": rows[:12],
            "artists": sorted(((r, k, a) for a, (r, k) in by_artist.items()
                               if r + k > 1), reverse=True)[:10]}


def show(s, out=sys.stdout):
    if not s:
        print("no feedback yet — build a playlist, then:  elo.py no 3 5",
              file=out)
        return
    print("%d dropped, %d kept" % (len(s["drops"]), len(s["keeps"])), file=out)
    print("\nmost recent", file=out)
    for title, artist, verdict, mood, at in s["recent"]:
        print("  %s  %-34s %-22s %s"
              % ("drop" if verdict < 0 else "keep", title[:34], artist[:22],
                 mood or "-"), file=out)
    if s["artists"]:
        print("\nby artist", file=out)
        for rej, keep, artist in s["artists"]:
            print("  %2d dropped / %2d kept   %s" % (rej, keep, artist),
                  file=out)


def forget(what=None):
    con = common.connect()
    if what in (None, "all"):
        n = con.execute("SELECT count(*) FROM feedback").fetchone()[0]
        con.execute("DELETE FROM feedback")
        con.commit()
        return n
    needle = common.norm(what)
    ids = [r[0] for r in con.execute("SELECT id, title, artist FROM feedback")
           if needle and (needle in common.norm(r[1])
                          or needle in common.norm(r[2]))]
    con.executemany("DELETE FROM feedback WHERE id=?", [(i,) for i in ids])
    con.commit()
    return len(ids)
