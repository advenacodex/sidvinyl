"""Tiny fake Subsonic server for demos and tests (no Navidrome needed).

Serves a handful of synthetic albums: generated cover art and short audio
tracks (chords + bass + beat) rendered with ffmpeg's lavfi sources.
"""
import hashlib
import io
import json
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageDraw

ALBUMS = [
    ("Neon Static", "The Pixelated", 1987, ["Boot Sequence", "Raster Rain", "Sprite Dance"]),
    ("Kind of Blue Screen", "Miles Per Frame", 1959, ["So What If", "Blue in Green Screen"]),
    ("Dark Side of the Moon Base", "Pink Floppy", 1973, ["Speak to Me", "Breathe", "Time"]),
    ("Músic for 18 Sprites", "Steve Reich-Rom", 1976, ["Pulses", "Section I", "Section II"]),
    ("Ñandú Beats", "Los Núcleos", 2001, ["Año Nuevo", "Canción de cuna"]),
    ("Zeta", "Zzyzx", 2020, ["Last Track"]),
]
SONG_LEN = 24.0


def _album_id(i):
    return f"al-{i}"


def _songs(i):
    name, artist, year, titles = ALBUMS[i]
    return [{"id": f"so-{i}-{j}", "title": t, "artist": artist, "album": name, "track": j + 1,
             "duration": int(SONG_LEN), "coverArt": f"cv-{i}", "discNumber": 1}
            for j, t in enumerate(titles)]


def _cover_png(cid, size):
    h = hashlib.md5(cid.encode()).digest()
    img = Image.new("RGB", (size, size), (h[0], h[1], h[2]))
    d = ImageDraw.Draw(img)
    for r in range(size // 2, 0, -max(4, size // 14)):
        k = (r * 7 + h[3]) % 200 + 40
        d.ellipse((size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r), fill=((h[4] + k) % 256, (h[5] + k // 2) % 256, (h[6] + r) % 256))
    d.polygon([(size * 0.2, size * 0.8), (size * 0.5, size * 0.15), (size * 0.8, size * 0.8)], fill=(255, 220, 40))
    d.rectangle((size * 0.4, size * 0.55, size * 0.6, size * 0.8), fill=(20, 20, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _audio(song_id):
    h = hashlib.md5(song_id.encode()).digest()
    root = 110 * 2 ** ((h[0] % 7) / 12.0)
    # chord stack + bass + kick/hat pattern, ~SONG_LEN seconds
    inputs = []
    exprs = []
    notes = [root * 4, root * 4 * 1.26, root * 4 * 1.5, root * 8]
    for f in notes:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency={f:.2f}:duration={SONG_LEN}:sample_rate=44100"]
    inputs += ["-f", "lavfi", "-i", f"sine=frequency={root/2:.2f}:duration={SONG_LEN}:sample_rate=44100"]
    inputs += ["-f", "lavfi", "-i", f"anoisesrc=d={SONG_LEN}:c=white:r=44100:a=0.4"]
    flt = ("[0][1][2][3]amix=inputs=4:normalize=0,volume=0.20,tremolo=f=4:d=0.5[ch];"
           "[4]volume=0.45[bs];"
           "[5]highpass=f=6000,volume='if(lt(mod(t,0.25),0.03),1,0)':eval=frame,volume=0.5[hat];"
           "[ch][bs][hat]amix=inputs=3:normalize=0")
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"] + inputs + ["-filter_complex", flt, "-ac", "2", "-f", "mp3", "-"]
    return subprocess.run(cmd, stdout=subprocess.PIPE, check=True).stdout


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeSubsonic/1"

    def log_message(self, *a):
        pass

    def _json(self, payload):
        body = json.dumps({"subsonic-response": dict(status="ok", version="1.16.1", **payload)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        ep = u.path.rsplit("/", 1)[-1]
        if q.get("u") != self.server.user:
            body = json.dumps({"subsonic-response": {"status": "failed", "version": "1.16.1",
                               "error": {"code": 40, "message": "Wrong username or password"}}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if ep == "ping":
            self._json({})
        elif ep == "getAlbumList2":
            typ = q.get("type", "alphabeticalByName")
            idx = list(range(len(ALBUMS)))
            if typ == "alphabeticalByName":
                idx.sort(key=lambda i: ALBUMS[i][0].lower())
            elif typ == "alphabeticalByArtist":
                idx.sort(key=lambda i: ALBUMS[i][1].lower())
            elif typ == "newest":
                idx.sort(key=lambda i: -ALBUMS[i][2])
            off, size = int(q.get("offset", 0)), int(q.get("size", 20))
            out = [{"id": _album_id(i), "name": ALBUMS[i][0], "artist": ALBUMS[i][1], "year": ALBUMS[i][2],
                    "coverArt": f"cv-{i}", "songCount": len(ALBUMS[i][3])} for i in idx[off:off + size]]
            self._json({"albumList2": {"album": out}})
        elif ep == "getAlbum":
            i = int(q["id"].split("-")[1])
            n, a, y, t = ALBUMS[i]
            self._json({"album": {"id": q["id"], "name": n, "artist": a, "year": y, "song": _songs(i)}})
        elif ep == "getCoverArt":
            png = _cover_png(q["id"], int(q.get("size", 160)))
            self.send_response(200); self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png))); self.end_headers(); self.wfile.write(png)
        elif ep == "stream":
            data = self.server.cache.get(q["id"])
            if data is None:
                data = self.server.cache[q["id"]] = _audio(q["id"])
            self.send_response(200); self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()


def start(user="demo", host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.user = user
    srv.cache = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://{host}:{srv.server_address[1]}"


if __name__ == "__main__":
    s, url = start(port=4533)
    print("fake subsonic at", url, "(user: demo, any password)")
    threading.Event().wait()
