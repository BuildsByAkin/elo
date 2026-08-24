"""Import a Spotify library over the Web API.

WHAT IS STILL THERE, AND WHAT IS NOT

    On 27 November 2024 Spotify deprecated `audio-features`, `audio-analysis`,
    `recommendations` and `related-artists` for every app registered after that
    date, and as of 2026 there is no replacement and no reversal. That killed
    the obvious way to do this: `audio-features` returned valence, energy,
    danceability and tempo per track, which is a mood model handed to you for
    free. It is gone and nothing else offers it.

    That is less of a loss than it looks, because this project does not want a
    mood number for your library. It wants to know that you own forty Meek Mill
    tracks and that Meek Mill is hip-hop, so that the hip-hop block leans on
    him and the sleep block does not. The artist object still carries `genres`,
    which answers exactly that, and the library endpoints are all untouched.

AUTH

    Authorization Code with PKCE, which needs a client id but no client secret,
    so nothing sensitive is stored. You register a free app once; there is no
    review, no quota application, and development mode is enough for personal
    use. (Extended mode now requires 250k monthly users, which no personal tool
    will ever have and none of this needs.)

    The one trap worth knowing: since April 2025 Spotify rejects `localhost` in
    a redirect URI. It must be the loopback literal `http://127.0.0.1:PORT/…`,
    and the failure mode is a generic redirect-uri-mismatch rather than
    anything that says so.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser

import requests

import common
import library

API = "https://api.spotify.com/v1"
AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
PORT = 8974
REDIRECT = "http://127.0.0.1:%d/callback" % PORT      # never `localhost`
SCOPES = ("user-library-read playlist-read-private "
          "playlist-read-collaborative user-follow-read user-top-read")
TOKENS = os.path.join(common.HERE, "spotify.json")

SETUP = """Spotify needs a free app registration — about three minutes, once.

  1. open https://developer.spotify.com/dashboard and log in
  2. Create app.  Name and description can be anything.
  3. Redirect URI — paste EXACTLY this, it must be the loopback literal:

         %s

     Spotify stopped accepting `localhost` in April 2025 and the error you
     get for it does not say so.
  4. tick "Web API", save, then copy the Client ID
  5. put it in .env beside this file:

         SPOTIFY_CLIENT_ID=...

Then:  python elo.py import spotify""" % REDIRECT


# --------------------------------------------------------------------- auth

class _Catcher(http.server.BaseHTTPRequestHandler):
    """Receives the one redirect Spotify makes back to us."""
    code = None
    error = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (q.get("code") or [None])[0]
        _Catcher.error = (q.get("error") or [None])[0]
        body = ("<h2>elo</h2><p>%s</p><p>You can close this tab.</p>" %
                ("Authorised." if _Catcher.code else
                 "Failed: %s" % _Catcher.error)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass                                   # keep the console clean


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode(
        ).rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _save(tok):
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    with open(TOKENS, "w") as fh:
        json.dump(tok, fh)
    os.chmod(TOKENS, 0o600)
    return tok


def _client_id():
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    if not cid:
        sys.exit(SETUP)
    return cid


def authorise():
    """Open a browser once, catch the redirect, store a refresh token."""
    cid = _client_id()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    url = AUTH + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
        "scope": SCOPES, "code_challenge_method": "S256",
        "code_challenge": challenge, "state": state})

    server = http.server.HTTPServer(("127.0.0.1", PORT), _Catcher)
    threading.Thread(target=server.handle_request, daemon=True).start()
    print("opening your browser to authorise elo...\n  %s" % url,
          file=sys.stderr)
    webbrowser.open(url)

    for _ in range(300):                       # five minutes
        if _Catcher.code or _Catcher.error:
            break
        time.sleep(1)
    server.server_close()
    if _Catcher.error or not _Catcher.code:
        sys.exit("Spotify did not authorise: %s\n\n%s"
                 % (_Catcher.error or "no code returned", SETUP))

    r = requests.post(TOKEN, data={
        "grant_type": "authorization_code", "code": _Catcher.code,
        "redirect_uri": REDIRECT, "client_id": cid,
        "code_verifier": verifier}, timeout=30)
    if not r.ok:
        sys.exit("token exchange failed (%d): %s\n\n%s"
                 % (r.status_code, r.text[:300], SETUP))
    return _save(r.json())


def token():
    """A valid access token, refreshing or re-authorising as needed."""
    tok = None
    if os.path.exists(TOKENS):
        try:
            tok = json.load(open(TOKENS))
        except ValueError:
            tok = None
    if tok and tok.get("expires_at", 0) > time.time():
        return tok["access_token"]
    if tok and tok.get("refresh_token"):
        r = requests.post(TOKEN, data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": _client_id()}, timeout=30)
        if r.ok:
            fresh = r.json()
            # Spotify does not always return a new refresh token; keep the old.
            fresh.setdefault("refresh_token", tok["refresh_token"])
            return _save(fresh)["access_token"]
        print("  refresh failed, re-authorising", file=sys.stderr)
    return authorise()["access_token"]


# --------------------------------------------------------------------- read

def get(path, access, **params):
    url = path if path.startswith("http") else API + path
    for attempt in range(4):
        r = requests.get(url, headers={"Authorization": "Bearer " + access},
                         params=params or None, timeout=30)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "2")) + 1
            print("  rate limited, waiting %ds" % wait, file=sys.stderr)
            time.sleep(wait)
            continue
        if r.status_code == 401:
            access = token()
            continue
        if not r.ok:
            print("  %s -> %d %s" % (url, r.status_code, r.text[:120]),
                  file=sys.stderr)
            return {}
        return r.json()
    return {}


def paged(path, access, limit=50, cap=10000, **params):
    """Walk a paged collection, following `next` rather than counting."""
    out, url = [], None
    params = dict(params, limit=limit)
    while len(out) < cap:
        page = get(url or path, access, **({} if url else params))
        items = page.get("items")
        if items is None:
            break
        out += items
        url = page.get("next")
        if not url:
            break
    return out[:cap]


def _track_row(t, liked=0, added=""):
    if not t or not t.get("name") or t.get("is_local"):
        return None
    artists = ", ".join(a["name"] for a in (t.get("artists") or [])
                        if a.get("name"))
    album = (t.get("album") or {})
    return {"title": t["name"], "artist": artists,
            "album": album.get("name") or "",
            "genre": "", "year": str(album.get("release_date") or "")[:4],
            "seconds": int((t.get("duration_ms") or 0) / 1000),
            "plays": 0, "skips": 0, "rating": 0, "liked": liked,
            "added": (added or "")[:10], "source": "spotify"}


def load(quiet=False):
    access = token()
    me = get("/me", access)
    if not quiet and me.get("display_name"):
        print("spotify: authorised as %s" % me["display_name"], file=sys.stderr)

    rows, seen = [], set()

    def add(row):
        if not row:
            return
        k = common.key(row["title"], row["artist"])
        if k in seen:
            return
        seen.add(k)
        rows.append(row)

    saved = paged("/me/tracks", access)
    for item in saved:
        add(_track_row(item.get("track"), liked=1, added=item.get("added_at")))

    albums = paged("/me/albums", access)
    for item in albums:
        al = item.get("album") or {}
        for t in ((al.get("tracks") or {}).get("items") or []):
            t.setdefault("album", {"name": al.get("name"),
                                   "release_date": al.get("release_date")})
            add(_track_row(t, added=item.get("added_at")))

    # Top tracks are not "owned", but they are the single best statement of
    # what you actually listen to, and Spotify gives no play counts otherwise.
    tops = []
    for term in ("short_term", "medium_term", "long_term"):
        tops += (get("/me/top/tracks", access, limit=50,
                     time_range=term).get("items") or [])
    for t in tops:
        add(_track_row(t))
    top_keys = {common.key(t["name"],
                           ", ".join(a["name"] for a in (t.get("artists") or [])))
                for t in tops if t.get("name")}

    playlists = []
    for pl in paged("/me/playlists", access):
        if not pl.get("id"):
            continue
        items = paged("/playlists/%s/tracks" % pl["id"], access, limit=100,
                      cap=1000)
        keys = []
        for item in items:
            row = _track_row(item.get("track"), added=item.get("added_at"))
            if not row:
                continue
            add(row)
            k = common.key(row["title"], row["artist"])
            if k not in keys:
                keys.append(k)
        if keys:
            playlists.append((pl.get("name") or pl["id"], keys))

    followed = ((get("/me/following", access, type="artist",
                     limit=50).get("artists") or {}).get("items") or [])
    top_artists = []
    for term in ("short_term", "medium_term", "long_term"):
        top_artists += (get("/me/top/artists", access, limit=50,
                            time_range=term).get("items") or [])

    n_tracks = library.upsert_tracks(rows)
    n_pl = library.upsert_playlists("spotify", playlists)
    library.mark_top(top_keys)
    library.mark_subscribed([a["name"] for a in followed if a.get("name")])

    # Genres live on the artist, not the track. Everyone we already met from
    # following or top-artists is free; the rest cost one batched call per 50.
    tagged = {}
    for a in followed + top_artists:
        if a.get("name") and a.get("genres"):
            tagged[common.norm(a["name"])] = (a["name"], a["genres"])
    ids = []
    for t in saved:
        for a in ((t.get("track") or {}).get("artists") or []):
            if a.get("id") and common.norm(a.get("name") or "") not in tagged:
                ids.append(a["id"])
    ids = list(dict.fromkeys(ids))[:1000]
    for i in range(0, len(ids), 50):
        for a in (get("/artists", access,
                      ids=",".join(ids[i:i + 50])).get("artists") or []):
            if a and a.get("name") and a.get("genres"):
                tagged[common.norm(a["name"])] = (a["name"], a["genres"])
    n_tags = library.upsert_artist_tags(
        [(name, genres) for name, genres in tagged.values()], "spotify")

    summary = {"source": "spotify", "tracks": n_tracks, "playlists": n_pl,
               "artists_tagged": n_tags, "followed": len(followed),
               "top_tracks": len(top_keys)}
    if not quiet:
        print("spotify: %d tracks, %d playlists, %d artists tagged, "
              "%d followed, %d top tracks"
              % (n_tracks, n_pl, n_tags, len(followed), len(top_keys)),
              file=sys.stderr)
    return summary


def main():
    load()


if __name__ == "__main__":
    main()
