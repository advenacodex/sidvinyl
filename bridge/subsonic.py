"""Minimal Subsonic / OpenSubsonic (Navidrome) API client using only the stdlib."""
import hashlib
import json
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "1.16.1"


class SubsonicError(Exception):
    pass


class Subsonic:
    def __init__(self, base_url, user, password, client="sidvinyl", insecure=False, timeout=20):
        base = base_url.rstrip("/")
        if base.endswith("/rest"):
            base = base[:-5]
        self.base = base
        self.user = user
        self.password = password
        self.client = client
        self.timeout = timeout
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    # ---------------------------------------------------------------- plumbing
    def _auth(self):
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {"u": self.user, "t": token, "s": salt, "v": API_VERSION, "c": self.client, "f": "json"}

    def url(self, endpoint, **params):
        q = self._auth()
        q.update({k: v for k, v in params.items() if v is not None})
        return f"{self.base}/rest/{endpoint}?{urllib.parse.urlencode(q)}"

    def _open(self, url):
        try:
            return urllib.request.urlopen(url, timeout=self.timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            raise SubsonicError(f"HTTP {e.code} from server") from e
        except urllib.error.URLError as e:
            raise SubsonicError(f"cannot reach server: {e.reason}") from e

    def call(self, endpoint, **params):
        with self._open(self.url(endpoint, **params)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        resp = data.get("subsonic-response", {})
        if resp.get("status") != "ok":
            err = resp.get("error", {})
            raise SubsonicError(err.get("message", "request failed"))
        return resp

    # ---------------------------------------------------------------- API
    def ping(self):
        return self.call("ping")

    def album_list(self, kind="alphabeticalByName", offset=0, size=20):
        r = self.call("getAlbumList2", type=kind, size=size, offset=offset)
        return r.get("albumList2", {}).get("album", [])

    def album(self, album_id):
        a = self.call("getAlbum", id=album_id).get("album", {})
        songs = a.get("song", [])
        songs.sort(key=lambda s: (s.get("discNumber", 1), s.get("track", 0)))
        return a, songs

    def cover(self, cover_id, size=160):
        with self._open(self.url("getCoverArt", id=cover_id, size=size)) as r:
            data = r.read()
        if data[:1] == b"{" or data[:5] == b"<?xml":
            raise SubsonicError("no cover art")
        return data

    def stream_url(self, song_id):
        return self.url("stream", id=song_id)
