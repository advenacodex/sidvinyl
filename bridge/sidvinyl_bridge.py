#!/usr/bin/env python3
"""SIDVinyl bridge: Navidrome/Subsonic <-> Commodore 64 (via Ultimate UCI sockets).

    python3 sidvinyl_bridge.py --server http://navidrome:4533 --user me --password secret
    python3 sidvinyl_bridge.py --demo            # built-in fake library, no server needed

The C64 program connects to this process over TCP (default port 6464). The
bridge browses the library, converts cover art into rotating multicolour label
frames and streams either 4-bit digi audio or live SID register frames.
"""
import argparse
import collections
import io
import logging
import os
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

import audio  # noqa: E402
import c64gfx  # noqa: E402
import protocol as P  # noqa: E402
from subsonic import Subsonic, SubsonicError  # noqa: E402

log = logging.getLogger("sidvinyl")

LEAD = 0.6            # seconds of audio we may send ahead of real time
CHUNK = 0.1           # seconds per audio packet




class SidStream:
    """Decodes and analyses a track in a worker thread, ahead of playback.

    The encoder looks at the music with ~1.5 s of look-ahead, so the frames are produced
    in blocks and queued here; the session pops them at playback rate."""
    AHEAD = 30.0                      # seconds of frames to keep queued

    def __init__(self, proc, enc):
        self.proc, self.enc = proc, enc
        self.frames = collections.deque()
        self.done = False
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            while not self.stop:
                while len(self.frames) > self.enc.fps * self.AHEAD and not self.stop:
                    time.sleep(0.1)
                pcm = audio.read_pcm(self.proc, self.enc.RATE // 2)
                if len(pcm) == 0:
                    break
                self.frames.extend(self.enc.feed(pcm))
            if not self.stop:
                self.frames.extend(self.enc.flush())
        except Exception:  # noqa: BLE001
            log.exception("SID analysis failed")
        finally:
            self.done = True

    def close(self):
        self.stop = True


class Session:
    def __init__(self, conn, addr, sub):
        self.conn, self.addr, self.sub = conn, addr, sub
        self.cmdq = queue.Queue()
        self.lock = threading.Lock()
        self.cv = threading.Condition()
        self.flow = False          # serial transport: credit-based flow control
        self.credit = 0
        self.digi_rate = P.DIGI_RATES[0]
        self.alive = True
        self.fps, self.clock = 50, audio.SID_CLOCK_PAL
        self.last_seq = 0
        self.level = 12
        self.mode = P.MODE_DIGI
        self.list_ids = []
        self.songs = []
        self.qi = 0
        self.album_name = ""
        self.cover_id = None
        self.proc = None
        self.enc = None
        self.sid = None
        self.sid_sent = 0
        self.t0 = 0.0
        self.produced = 0.0
        self.playing = False

    # ---------------------------------------------------------------- io
    def send(self, ptype, payload=b""):
        data = P.packet(ptype, payload)
        with self.lock:
            self._write(data)

    def _write(self, data):
        if not self.flow:
            self.conn.sendall(data)
            return
        mv = memoryview(data)
        while len(mv):
            with self.cv:
                while self.credit <= 0:
                    log.debug("waiting for credit (%d bytes pending)", len(mv))
                    if not self.alive:
                        raise ConnectionError("closed")
                    self.cv.wait(0.5)
                n = min(len(mv), self.credit)
                self.credit -= n
            self.conn.sendall(mv[:n])
            mv = mv[n:]

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _reader(self):
        try:
            while self.alive:
                cid = self._recv(1)[0]
                n = P.CMD_LEN.get(cid)
                if n is None:
                    log.warning("unknown command %02x, dropping connection", cid)
                    break
                args = self._recv(n - 1)
                if cid == P.CMD_CREDIT:        # handled here: the main thread may be blocked in send()
                    with self.cv:
                        self.credit += args[0] * P.CREDIT_UNIT
                        self.cv.notify_all()
                    continue
                self.cmdq.put((cid, args))
        except (ConnectionError, OSError):
            pass
        finally:
            self.alive = False
            with self.cv:
                self.cv.notify_all()
            self.cmdq.put(None)

    # ---------------------------------------------------------------- main loop
    def run(self):
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            while self.alive:
                if not self.playing:
                    try:
                        item = self.cmdq.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    if item is None:
                        break
                    self._handle(*item)
                    continue
                try:
                    while True:
                        item = self.cmdq.get_nowait()
                        if item is None:
                            self.alive = False
                            break
                        self._handle(*item)
                except queue.Empty:
                    pass
                if self.alive and self.playing:
                    self._pump()
        except (ConnectionError, OSError) as e:
            log.info("client %s gone: %s", self.addr, e)
        finally:
            self.alive = False
            self._kill()
            try:
                self.conn.close()
            except OSError:
                pass
            log.info("session %s closed", self.addr)

    # ---------------------------------------------------------------- commands
    def _handle(self, cid, a):
        try:
            if cid == P.CMD_HELLO:
                self.fps, self.clock = (60, audio.SID_CLOCK_NTSC) if a[0] else (50, audio.SID_CLOCK_PAL)
                serial = a[2] == P.TRANSPORT_SERIAL
                self.flow = serial
                self.digi_rate = P.DIGI_RATES[1 if serial else 0]
                log.info("hello from %s: %s, %s", self.addr, "NTSC" if a[0] else "PAL", "serial" if serial else "UCI")
                self.send(P.PKT_HELLO, bytes([P.PROTOCOL_VERSION, 1 if serial else 0]))
                self.send(P.PKT_DISC, c64gfx.disc_blob())
            elif cid == P.CMD_LIST:
                self._list(a[0], a[1])
            elif cid == P.CMD_PLAY:
                self.last_seq = a[1]
                self._play_album(a[0])
            elif cid == P.CMD_NEXT:
                self.last_seq = a[0]
                if self.songs:
                    self._start((self.qi + 1) % len(self.songs))
            elif cid == P.CMD_PREV:
                self.last_seq = a[0]
                if self.songs:
                    self._start(max(0, self.qi - 1))
            elif cid == P.CMD_MODE:
                self.last_seq = a[1]
                self.mode = P.MODE_SID if a[0] else P.MODE_DIGI
                pos = a[2] | (a[3] << 8)
                if self.songs:
                    self._start(self.qi, start=float(pos))
            elif cid == P.CMD_VOL:
                d = a[0] - 256 if a[0] > 127 else a[0]
                self.level = max(0, min(15, self.level + d))
                self.send(P.PKT_VOL, bytes([self.level]))
            elif cid == P.CMD_STOP:
                self.last_seq = a[0]
                self._kill()
                self.playing = False
                self.send(P.PKT_STOP, bytes([self.last_seq]))
        except SubsonicError as e:
            log.warning("subsonic error: %s", e)
            self._error(str(e))
        except OSError:
            raise

    def _error(self, msg):
        self._kill()
        self.playing = False
        self.send(P.PKT_ERROR, P.to_screen(msg, P.LIST_ENTRY))
        self.send(P.PKT_STOP, bytes([self.last_seq]))

    def _list(self, sort, page):
        sort = sort if sort < len(P.SORTS) else 0
        albums = self.sub.album_list(P.SORTS[sort], offset=page * P.LIST_PAGE, size=P.LIST_PAGE + 1)
        more = 1 if len(albums) > P.LIST_PAGE else 0
        albums = albums[:P.LIST_PAGE]
        self.list_ids = [a["id"] for a in albums]
        body = b"".join(P.to_screen(f"{a.get('artist', '?')} - {a.get('name', a.get('title', '?'))}", P.LIST_ENTRY)
                        for a in albums)
        self.send(P.PKT_LIST, bytes([sort, page, len(albums), more]) + body)

    def _play_album(self, idx):
        if idx >= len(self.list_ids):
            raise SubsonicError("no such album")
        album, songs = self.sub.album(self.list_ids[idx])
        if not songs:
            raise SubsonicError("album has no tracks")
        self.songs = songs
        self.album_name = album.get("name", "")
        self.cover_id = album.get("coverArt") or songs[0].get("coverArt")
        self._start(0, new_cover=True)

    # ---------------------------------------------------------------- playback
    def _kill(self):
        if self.sid is not None:
            self.sid.close()
            self.sid = None
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            self.proc = None

    def _cover_frames(self):
        try:
            img = Image.open(io.BytesIO(self.sub.cover(self.cover_id, size=160))) if self.cover_id else None
        except Exception as e:  # noqa: BLE001
            log.info("cover unavailable (%s), using generated label", e)
            img = None
        if img is None:
            img = c64gfx.default_cover(self.album_name)
        t = time.time()
        data = c64gfx.label_frames(img)
        log.info("cover converted in %.2fs", time.time() - t)
        return data

    def _start(self, idx, start=0.0, new_cover=False):
        self._kill()
        self.qi = idx
        song = self.songs[idx]
        dur = int(song.get("duration", 0))
        log.info("track %d/%d: %s - %s (%s, from %.0fs)", idx + 1, len(self.songs), song.get("artist"),
                 song.get("title"), "SID" if self.mode == P.MODE_SID else "DIGI", start)
        pl = bytes([self.last_seq, self.mode, idx + 1, min(255, len(self.songs)), dur & 0xFF, (dur >> 8) & 0xFF,
                    1 if new_cover else 0])
        pl += P.str_field(song.get("title", "?")) + P.str_field(song.get("artist", "?")) + \
            P.str_field(song.get("album", self.album_name))
        self.send(P.PKT_TRACK, pl)
        if new_cover:
            self.send(P.PKT_COVER, self._cover_frames())
        if self.mode == P.MODE_SID:
            rate, flt = audio.ChiptuneEncoder.RATE, audio.FILTER_SID
            self.enc = audio.ChiptuneEncoder(self.fps, self.clock)
        else:
            rate, flt = self.digi_rate, audio.FILTER_DIGI
            self.enc = audio.DigiEncoder()
        self.rate = rate
        self.proc = audio.start_decoder(self.sub.stream_url(song["id"]), rate, start, flt)
        self.sid = SidStream(self.proc, self.enc) if self.mode == P.MODE_SID else None
        self.sid_sent = 0
        self.t0 = time.monotonic()
        self.produced = 0.0
        self.playing = True

    def _next_or_stop(self):
        self._kill()
        if self.qi + 1 < len(self.songs):
            self._start(self.qi + 1)
        else:
            self.playing = False
            self.send(P.PKT_STOP, bytes([self.last_seq]))

    def _pump_sid(self):
        ahead = self.sid_sent / self.fps - (time.monotonic() - self.t0)
        if ahead > LEAD:
            time.sleep(min(0.05, ahead - LEAD))
            return
        q = self.sid.frames
        out = []
        while len(out) < int(self.fps * CHUNK) and q:
            out.append(q.popleft())
        if not out:
            if self.sid.done and not q:
                self._next_or_stop()
                return
            # analysis not ready yet: keep the playback clock from running away
            self.t0 = time.monotonic() - self.sid_sent / self.fps
            time.sleep(0.02)
            return
        scale = self.level / 12.0
        self.sid_sent += len(out)
        self.send(P.PKT_SID, b"".join(f[:21] + bytes([min(15, int(round(f[21] * scale)))]) for f in out))

    def _pump(self):
        if self.sid is not None:
            return self._pump_sid()
        ahead = self.produced - (time.monotonic() - self.t0)
        if ahead > LEAD:
            time.sleep(min(0.05, ahead - LEAD))
            return
        n = int(self.rate * CHUNK)
        pcm = audio.read_pcm(self.proc, n)
        if len(pcm) == 0:
            self._next_or_stop()
            return
        self.produced += len(pcm) / self.rate
        gain = 10 ** ((self.level - 12) * 2.0 / 20.0)
        data = self.enc.encode(pcm, gain)
        if data:
            self.send(P.PKT_DIGI, data)


def serve(args, sub):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(2)
    log.info("listening on %s:%d", args.host, srv.getsockname()[1])
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
        log.info("connection from %s", addr)
        threading.Thread(target=Session(conn, addr, sub).run, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("NAVIDROME_URL"), help="Subsonic/Navidrome base URL")
    ap.add_argument("--user", default=os.environ.get("NAVIDROME_USER"))
    ap.add_argument("--password", default=os.environ.get("NAVIDROME_PASSWORD"))
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on")
    ap.add_argument("--port", type=int, default=6464)
    ap.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    ap.add_argument("--demo", action="store_true", help="use a built-in fake library")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if args.demo:
        import fake_subsonic
        _, url = fake_subsonic.start()
        args.server, args.user, args.password = url, "demo", "demo"
        log.info("demo library at %s", url)
    if not (args.server and args.user and args.password):
        ap.error("need --server, --user and --password (or NAVIDROME_URL/USER/PASSWORD), or --demo")
    sub = Subsonic(args.server, args.user, args.password, insecure=args.insecure)
    sub.ping()
    log.info("connected to %s", args.server)
    serve(args, sub)


if __name__ == "__main__":
    main()
