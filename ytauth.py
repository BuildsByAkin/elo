"""YouTube Music credentials: set them up, and know when they have died.

Two auth methods exist and browser is the right one:

  browser.json  copy your request headers once. Valid about two years, no
                Google Cloud project, and it can read uploads.
  oauth.json    needs a Google Cloud OAuth client ("TVs and Limited Input
                devices"), client_id and client_secret mandatory since
                November 2024, and the resulting credentials cannot see
                uploaded tracks. More setup for strictly less access.

THE FAILURE MODE THIS FILE EXISTS FOR

    Expired credentials do not raise. YouTube Music answers a logged-out
    request with a perfectly valid page that simply has no library in it, so
    ytmusicapi parses it happily and hands back an empty list. "Your session
    died eight months ago" and "you own no music" are the same value.

    That cost this project a full import cycle before anyone noticed, so
    `health()` asks the one question that actually distinguishes them. Every
    response carries `responseContext.serviceTrackingParams[].params` with a
    `logged_in` flag, straight from YouTube, independent of page layout and
    of whatever renderer they rename next. It is `'1'` or it is not.

Note that the `Authorization: SAPISIDHASH <ts>_<hash>` header in the file is
ignored — ytmusicapi recomputes it from the SAPISID cookie on every request,
because the hash is timestamped. A fresh-looking Authorization line therefore
says nothing at all about whether the credentials still work; only the cookies
matter, and only the server can tell you.
"""
import json
import os
import re
import sys

import common

FILES = ("browser.json", "oauth.json")

HELP = """YouTube Music needs your browser's request headers — two minutes, once,
and good for about two years.

  1. open https://music.youtube.com in your browser, logged in
  2. open developer tools (F12) and pick the Network tab
  3. filter the requests for   /browse
  4. click any POST that returned 200
  5. copy the request headers:
       Firefox  right-click > Copy Value > Copy Request Headers
       Chrome   right-click > Copy > Copy as fetch (Node.js)
  6. run this and paste them, then press Ctrl-D:

         python elo.py auth ytmusic

     or save the paste to a file and run:

         python elo.py auth ytmusic headers.txt

Both the raw header list and Chrome's "Copy as fetch" blob are understood.
Nothing is uploaded; the headers are written to browser.json beside this file."""


def path():
    """The credentials file, wherever it is, or None."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in FILES:
        for base in (os.getcwd(), here):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
    return None


def client(need_auth=True):
    try:
        from ytmusicapi import YTMusic
    except ImportError:
        sys.exit("ytmusicapi is not installed — pip install ytmusicapi")
    if not need_auth:
        return YTMusic()
    p = path()
    if not p:
        sys.exit("No YouTube Music credentials found.\n\n" + HELP)
    return YTMusic(p)


def _logged_in(yt):
    """Ask YouTube directly whether this session is signed in.

    One cheap browse call. Returns True, False, or None if the response did
    not carry the flag at all, which is a different thing from a `0` and
    should not be reported as an expiry.
    """
    try:
        r = yt._send_request("browse", {"browseId": "FEmusic_liked_playlists"})
    except Exception as e:
        return None, str(e)[:120]
    for group in (r.get("responseContext") or {}).get(
            "serviceTrackingParams") or []:
        for p in group.get("params") or []:
            if p.get("key") == "logged_in":
                return p.get("value") == "1", ""
    return None, "no logged_in flag in the response"


def health(quiet=True):
    """{ok, why, path, account}. The only honest answer about these creds."""
    p = path()
    out = {"ok": False, "why": "", "path": p, "account": None}
    if not p:
        out["why"] = "no credentials file — run: python elo.py auth ytmusic"
        return out
    if p.endswith("browser.json"):
        try:
            with open(p) as fh:
                raw = json.load(fh)
        except ValueError:
            out["why"] = "%s is not valid JSON — re-run the setup" % p
            return out
        keys = {k.lower() for k in raw}
        if "cookie" not in keys:
            out["why"] = ("%s has no Cookie header, so it was never "
                          "authenticated — you probably copied an "
                          "unauthenticated request" % p)
            return out
        cookie = next(v for k, v in raw.items() if k.lower() == "cookie")
        if "SAPISID" not in cookie:
            out["why"] = ("the Cookie header has no SAPISID, so it is a "
                          "logged-out session — copy the headers again while "
                          "signed in")
            return out

    try:
        yt = client()
    except SystemExit as e:
        out["why"] = str(e)
        return out
    ok, err = _logged_in(yt)
    if ok is None:
        out["why"] = "could not tell: %s" % err
        return out
    if not ok:
        out["why"] = ("YouTube Music says this session is signed out — the "
                      "cookies have expired. Re-run: python elo.py auth "
                      "ytmusic")
        return out
    out["ok"] = True
    try:
        info = yt.get_account_info() or {}
        out["account"] = info.get("accountName")
    except Exception:
        pass                      # nice to have, never the deciding factor
    return out


def _from_fetch(text):
    """Chrome's "Copy as fetch (Node.js)" is JSON, not a header list.

    It is also the option Chrome puts in front of people, so it is what most
    pastes actually are. Pulling the headers object out of it beats telling
    someone they chose the wrong menu item.
    """
    m = re.search(r'"headers"\s*:\s*\{', text)
    if not m:
        return None
    start = m.end() - 1
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    try:
        headers = json.loads(text[start:i + 1])
    except ValueError:
        return None
    return "\n".join("%s: %s" % (k, v) for k, v in headers.items())


def setup(headers_raw=None, quiet=False):
    """Write browser.json from pasted headers, then prove it works."""
    import ytmusicapi

    if headers_raw is None:
        print("Paste the request headers, then press Ctrl-D:\n",
              file=sys.stderr)
        headers_raw = sys.stdin.read()
    headers_raw = (headers_raw or "").strip()
    if not headers_raw:
        sys.exit("nothing pasted.\n\n" + HELP)

    converted = _from_fetch(headers_raw)
    if converted:
        if not quiet:
            print("  read Chrome's \"Copy as fetch\" format", file=sys.stderr)
        headers_raw = converted

    dest = os.path.join(common.HERE, "browser.json")
    backup = None
    if os.path.exists(dest):
        # Never destroy working credentials because a paste was malformed.
        backup = dest + ".bak"
        os.replace(dest, backup)
    try:
        ytmusicapi.setup(filepath=dest, headers_raw=headers_raw)
    except Exception as e:
        if backup:
            os.replace(backup, dest)
        sys.exit("could not read those headers: %s\n\n%s" % (str(e)[:200],
                                                            HELP))
    os.chmod(dest, 0o600)

    state = health()
    if not state["ok"]:
        if backup:
            os.replace(backup, dest)
            print("kept your previous browser.json", file=sys.stderr)
        else:
            os.remove(dest)
        sys.exit("those headers did not authenticate: %s\n\n%s"
                 % (state["why"], HELP))
    if backup:
        os.remove(backup)
    if not quiet:
        who = (" as %s" % state["account"]) if state["account"] else ""
        print("youtube music: authenticated%s -> %s" % (who, dest),
              file=sys.stderr)
    return state


def require(action="that"):
    """Fail loudly and early rather than half-doing the work."""
    state = health()
    if not state["ok"]:
        sys.exit("YouTube Music is not authenticated, so %s cannot run.\n  %s"
                 % (action, state["why"]))
    return state


def status(out=sys.stderr):
    """One line per service, for `elo.py auth`."""
    import library
    done = library.imports()

    state = health()
    who = (" (%s)" % state["account"]) if state.get("account") else ""
    print("ytmusic   %s%s" % ("connected" + who if state["ok"]
                              else "NOT connected", ""), file=out)
    if not state["ok"]:
        print("            %s" % state["why"], file=out)
    if "ytmusic" in done:
        d = done["ytmusic"]
        print("            last import %s: %d tracks, %d playlists"
              % (d.get("at", "?"), d.get("tracks", 0), d.get("playlists", 0)),
              file=out)

    tok = os.path.join(common.HERE, "spotify.json")
    has_id = bool(os.environ.get("SPOTIFY_CLIENT_ID"))
    print("spotify   %s" % ("connected" if os.path.exists(tok) else
                            ("SPOTIFY_CLIENT_ID set, not yet authorised"
                             if has_id else "NOT connected")), file=out)
    if not os.path.exists(tok) and not has_id:
        print("            put SPOTIFY_CLIENT_ID in .env — see "
              "`python elo.py import spotify`", file=out)
    if "spotify" in done:
        d = done["spotify"]
        print("            last import %s: %d tracks, %d playlists"
              % (d.get("at", "?"), d.get("tracks", 0), d.get("playlists", 0)),
              file=out)

    if "apple" in done:
        d = done["apple"]
        print("apple     last import %s: %d tracks, %d playlists"
              % (d.get("at", "?"), d.get("tracks", 0), d.get("playlists", 0)),
              file=out)
    else:
        print("apple     nothing imported — Music.app > File > Library > "
              "Export Library", file=out)


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(HELP)
        return
    if args:
        setup(open(os.path.expanduser(args[0])).read())
    else:
        setup()


if __name__ == "__main__":
    main()
