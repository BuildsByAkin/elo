"""The pipeline: a request in, songs from all music out, what you own ranked up.

    request -> expand into human tags + a target mood card
            -> fan out to Last.fm (wide) and the model (capped)
            -> dedupe, note which candidates several sources agree on
            -> shortlist, fetch lyrics, write mood cards (cached forever)
            -> rank on mood fit, with a boost for tracks you already own

The library is a priority signal, never a gate. Cards persist, so the corpus
builds itself out of use: the first query pays for the whole shortlist, later
queries mostly hit cache.
"""
import json
import sys

import common
import lyrics as L
import sources as S
import tag as T

SHORTLIST = 60      # candidates that get lyrics + a mood card per query
OWNED_BOOST = 1.2   # how much owning a track is worth, in score points
AGREE_BONUS = 0.6   # per extra source that independently surfaced it


def shortlist(cands, n):
    """Cheap pre-rank before spending money: cross-source agreement, then
    ownership, then whatever a source already ranked highly."""
    return sorted(cands, key=lambda c: (-c["hits"], not c["owned"],
                                        -c.get("weight", 1.0)))[:n]


def ensure_cards(con, cands):
    """Fetch lyrics and write mood cards for anything not already tagged."""
    have = {r[0] for r in con.execute("SELECT track_id FROM moods")}
    todo = [c for c in cands if c["id"] not in have]
    print("cards: %d cached, %d to build" % (len(cands) - len(todo), len(todo)),
          file=sys.stderr)
    if not todo:
        return
    L.fetch(con, [{"id": c["id"], "title": c["title"], "artist": c["artist"]}
                  for c in todo], pause=0.15)
    ids = [c["id"] for c in todo]
    rows = con.execute(
        "SELECT id, title, artist, album, genre, year FROM tracks WHERE id IN"
        " (%s)" % ",".join("?" * len(ids)), ids).fetchall()
    tracks = [dict(zip(("id", "title", "artist", "album", "genre", "year"), r))
              for r in rows]
    lyr = L.load(con, ids)
    for i in range(0, len(tracks), T.BATCH):
        batch = tracks[i:i + T.BATCH]
        texts = {t["id"]: (lyr.get(t["id"], ("none", ""))[1] or "")[:T.LYRIC_CAP]
                 for t in batch}
        out = common.llm(
            T.RUBRIC + "\n\nTHEMES: " + ", ".join(T.THEMES) +
            "\nSTANCES: " + ", ".join(T.STANCES) + "\n\n" +
            "\n\n".join(T.block(t, texts[t["id"]]) for t in batch), T.SCHEMA)
        ok = {t["id"] for t in batch}
        T.save(con, [(c["id"], json.dumps(c["themes"]), c["stance"],
                      c["valence"], c["energy"], c["summary"],
                      "lyrics" if texts.get(c["id"]) else "metadata",
                      c["confidence"]) for c in out["cards"] if c["id"] in ok])
        print("  tagged %d/%d" % (min(i + T.BATCH, len(tracks)), len(tracks)),
              file=sys.stderr)


def run(con, query, n=10, owned_only=False, known_only=False):
    import elo                                  # score() lives with the CLI

    print("expanding %r" % query, file=sys.stderr)
    tags, model_songs, target = S.expand(query)
    print("  tags   : %s" % ", ".join(tags), file=sys.stderr)
    print("  target : %s | %s | v%.1f e%.1f"
          % ("/".join(target["themes"]), target["stance"],
             target["valence"], target["energy"]), file=sys.stderr)

    pools = [S.lastfm_tag(t, 200) for t in tags]
    wide = sum(len(p) for p in pools)
    pools.append(model_songs)
    cands = S.cap_model(S.dedupe(pools))
    cands = S.persist(con, cands)
    owned = sum(1 for c in cands if c["owned"])
    print("  pool   : %d from Last.fm + %d from the model -> %d unique, %d owned"
          % (wide, len(model_songs), len(cands), owned), file=sys.stderr)

    if owned_only:
        cands = [c for c in cands if c["owned"]]
    short = shortlist(cands, SHORTLIST)
    ensure_cards(con, short)

    by_id = {c["id"]: c for c in short}
    cards = [c for c in elo.cards(con, external=True) if c["id"] in by_id]
    if known_only:
        cards = [c for c in cards if c["confidence"] == "known"]
    if not cards:
        sys.exit("nothing survived — try a broader request")

    def total(c):
        m = by_id[c["id"]]
        return (elo.score(target, c) + (OWNED_BOOST if m["owned"] else 0.0)
                + AGREE_BONUS * (m["hits"] - 1))

    ranked = sorted(cards, key=lambda c: -total(c))[:n]
    print("\nTARGET  %s | %s | valence %.1f  energy %.1f\n"
          % ("/".join(target["themes"]), target["stance"],
             target["valence"], target["energy"]))
    for i, c in enumerate(ranked, 1):
        m = by_id[c["id"]]
        mark = "●" if m["owned"] else "○"
        elo.show(c, "%s %d. " % (mark, i))
        print("      %.2f  %s%s" % (total(c), "in your library, "
                                    if m["owned"] else "",
                                    "/".join(sorted(m["sources"]))))
    print("\n● in your library   ○ new to you")
