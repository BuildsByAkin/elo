#!/usr/bin/env python3
"""elo — say what you want to listen to, get a playlist.

    python elo.py "i'm in the mood of tell your friends by the weeknd"
    python elo.py "take me from sad to happy, 30 minutes" --push
    python elo.py "wake me up on my drive to work, 20 min, afrobeats"

Two model calls' worth of taste on top of other people's listening. Nothing in
here has an opinion about what a song means; it asks YouTube Music and Last.fm
what gets played around it, weighs the answers against your own library in
code, and only then asks a model to order the shortlist.

    plan.py     the request      -> segments                (1 call)
    sources.py  a segment        -> candidates              (0 calls)
    apple.py    a Music.app export  -> your library         (0 calls)
    spotify.py  the Spotify API     -> your library         (0 calls)
    library.py  the store all three import into             (0 calls)
    taste.py    your library     -> candidates and weights  (0 calls)
    blend.py    a segment's pool -> an ordered block        (1 call each)
    push.py     the playlist     -> your YouTube Music account

Subcommands:
    elo.py "<request>"            build a playlist  (default)
    elo.py no 3 5 / yes 1         that one was wrong / that one was right
    elo.py again                  rebuild the last request with that applied
    elo.py last                   the last playlist, numbered
    elo.py feedback / forget      what has been learned, and undoing it
    elo.py pushes                 playlists elo has created in your account
    elo.py auth                   which services are connected
    elo.py auth ytmusic [file]    paste your YouTube Music headers
    elo.py import apple <file>    Music.app > File > Library > Export Library
    elo.py import spotify         browser sign-in, once
    elo.py import ytmusic         needs browser.json
    elo.py taste                  the breakdown of what you imported
    elo.py moods                  the mood and genre pools available
"""
import argparse
import json
import os
import sys
import time

import common


def blend_default_owned():
    import blend
    return blend.MAX_OWNED


def cmd_moods():
    import sources
    cats = sources.mood_categories()
    if not cats:
        sys.exit("could not read the mood categories")
    for section, items in cats.items():
        print("\n%s" % section)
        for c in items:
            print("  %s" % c["title"])


IMPORT_HELP = """usage: elo.py import <apple|spotify|ytmusic> [file]

  apple    python elo.py import apple ~/Desktop/Library.xml
           Music.app > File > Library > Export Library...  (ten seconds,
           no account, and the only source that carries play counts)
  spotify  python elo.py import spotify
           opens a browser once; needs a free SPOTIFY_CLIENT_ID in .env
  ytmusic  python elo.py import ytmusic
           needs browser.json — see `ytmusicapi browser`

Import as many as you use. They merge on title+artist, so a song you have in
two services becomes one row carrying whatever each of them knew."""


def cmd_import(args):
    import library
    if not args:
        sys.exit(IMPORT_HELP)
    source = args[0]
    if source not in library.SOURCES:
        sys.exit(IMPORT_HELP)
    library.load(source, args[1] if len(args) > 1 else None)
    import taste
    taste.show(taste.profile())


def cmd_taste():
    import taste
    taste.show(taste.profile(), out=sys.stdout)


USAGE_AUTH = """usage: elo.py auth [ytmusic [headers-file]]

  elo.py auth                    which services are connected
  elo.py auth ytmusic            paste your headers, then Ctrl-D
  elo.py auth ytmusic FILE       read the paste from a file

Spotify authorises itself on `elo.py import spotify`; Apple needs no
credentials at all."""


FEEDBACK_HELP = """usage:
  elo.py no 3 5        drop tracks 3 and 5 of the last playlist
  elo.py no 2-4        a range
  elo.py no "sicko"    match on title or artist instead
  elo.py yes 1         the opposite: you want more like this
  elo.py again         rebuild the same request with that applied
  elo.py last          the last playlist, numbered
  elo.py feedback      everything learned so far
  elo.py forget [x]    undo one track's verdicts, or all of them

A rejection is scoped to the block it happened in — dropping a rap track from
a sleep block teaches "not here", not "never". Reject the same track in two
different blocks and it is dropped everywhere."""


def cmd_verdict(args, verdict):
    import feedback
    if not args:
        sys.exit(FEEDBACK_HELP)
    feedback.record(args, verdict)
    print("  run `elo.py again` to rebuild with that applied",
          file=sys.stderr)


def cmd_last():
    import feedback
    rows = feedback.last()
    if not rows:
        sys.exit("no playlist yet — build one first")
    print("%s\n" % rows[0]["request"], file=sys.stderr)
    for r in rows:
        print("  %2d  %-42s %-24s %s"
              % (r["pos"], r["title"][:42], r["artist"][:24], r["mood"]))


def cmd_feedback():
    import feedback
    feedback.show(feedback.summary())


def cmd_forget(args):
    import feedback
    n = feedback.forget(args[0] if args else None)
    print("forgot %d verdict%s" % (n, "" if n == 1 else "s"), file=sys.stderr)


def cmd_again(argv):
    """Rebuild the last request. The point of the loop, made visible."""
    import feedback
    rows = feedback.last()
    if not rows or not rows[0]["request"]:
        sys.exit("nothing to rebuild — ask for a playlist first")
    return rows[0]["request"]


def cmd_auth(args):
    import ytauth
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE_AUTH + "\n\n" + ytauth.HELP)
        return
    if not args:
        ytauth.status(out=sys.stdout)
        return
    if args[0] != "ytmusic":
        sys.exit(USAGE_AUTH)
    if len(args) > 1:
        if args[1] in ("-h", "--help"):
            print(USAGE_AUTH + "\n\n" + ytauth.HELP)
            return
        src = os.path.expanduser(args[1])
        if not os.path.exists(src):
            sys.exit("no such file: %s\n\n%s" % (src, ytauth.HELP))
        with open(src) as fh:
            ytauth.setup(fh.read())
    else:
        ytauth.setup()


def cmd_build(args):
    import blend
    import plan
    import push
    import taste as T

    started = time.time()
    spec = plan.parse(args.request)
    print(plan.describe(spec), file=sys.stderr)
    print("  %s\n" % spec.get("reason", ""), file=sys.stderr)
    if args.dry_run:
        print(json.dumps(spec, indent=2))
        return

    taste = T.load()
    if not taste:
        print("nothing imported — every candidate weighs the same. "
              "run `python elo.py import apple <file>` to personalise.\n",
              file=sys.stderr)

    tracks, blocks = blend.build(spec, taste, use_llm=not args.no_llm,
                                 wide=args.wide,
                                 max_per_artist=args.max_per_artist,
                                 max_owned=args.max_owned)
    if not tracks:
        sys.exit("nothing came back — try naming a song or a genre")

    blend.show(blocks, taste=taste)
    print("built in %.1fs" % (time.time() - started), file=sys.stderr)

    import feedback
    feedback.remember(blocks, args.request)
    print("  not right?  elo.py no 3 5   ·   keep one?  elo.py yes 1   ·   "
          "then  elo.py again", file=sys.stderr)

    if args.json:
        out = []
        for t in tracks:
            row = dict(t)
            row["sources"] = sorted(t["sources"])   # a set will not serialise
            out.append(row)
        print(json.dumps({"plan": spec, "tracks": out}, indent=2))

    if args.push:
        title = args.title or spec.get("title") or args.request[:90]
        push.create(tracks, title, description=spec.get("reason", "")[:280],
                    request=args.request, new=args.new, quiet=True)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "moods":
        return cmd_moods()
    if argv and argv[0] == "auth":
        return cmd_auth(argv[1:])
    if argv and argv[0] == "import":
        return cmd_import(argv[1:])
    if argv and argv[0] in ("taste", "library"):
        return cmd_taste()
    if argv and argv[0] in ("no", "drop"):
        return cmd_verdict(argv[1:], -1)
    if argv and argv[0] in ("yes", "keep", "more"):
        return cmd_verdict(argv[1:], +1)
    if argv and argv[0] == "last":
        return cmd_last()
    if argv and argv[0] == "feedback":
        return cmd_feedback()
    if argv and argv[0] in ("pushes", "pushed"):
        import push
        return push.show()
    if argv and argv[0] == "forget":
        return cmd_forget(argv[1:])
    if argv and argv[0] == "again":
        # Replace the subcommand with the request it stands for and fall
        # through, so `again` accepts every flag a fresh build does.
        argv = [cmd_again(argv)] + argv[1:]

    p = argparse.ArgumentParser(
        prog="elo", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("request", nargs="+",
                   help='what you want to hear, in plain English')
    p.add_argument("--push", action="store_true",
                   help="create the playlist in your YouTube Music account")
    p.add_argument("--title", help="playlist name (default: the model's)")
    p.add_argument("--new", action="store_true",
                   help="always create a new playlist; by default a second "
                        "push of the same title updates the one elo already "
                        "made rather than piling up copies")
    p.add_argument("--no-llm", action="store_true",
                   help="skip the ordering calls; take the code ranking as-is")
    p.add_argument("--wide", type=int, default=2, metavar="N",
                   help="expand from N of the strongest candidates (0 to skip)")
    p.add_argument("--max-per-artist", type=int, default=2, metavar="N")
    p.add_argument("--max-owned", type=float, default=blend_default_owned(),
                   metavar="F",
                   help="max share of each block that may be music you already"
                        " own (default 0.6; 1.0 to disable)")
    p.add_argument("--json", action="store_true", help="also dump JSON")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and stop before gathering")
    args = p.parse_args(argv)
    args.request = " ".join(args.request)
    cmd_build(args)


if __name__ == "__main__":
    main()
