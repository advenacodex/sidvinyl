"""Drives the bridge with a scripted fake C64 over TCP (demo library)."""
import os, socket, struct, sys, threading, time
from types import SimpleNamespace
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import fake_subsonic, sidvinyl_bridge as B, protocol as P
from subsonic import Subsonic

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def from_screen(b):
    out = ""
    for c in b:
        if 1 <= c <= 26: out += chr(c + 96)
        elif 65 <= c <= 90 or 32 <= c <= 63: out += chr(c)
        elif c == 0: out += "@"
        else: out += "?"
    return out.rstrip()

class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port)); self.s.settimeout(30)
    def send(self, *b): self.s.sendall(bytes(b))
    def recvn(self, n):
        d = b""
        while len(d) < n:
            c = self.s.recv(n - len(d))
            if not c: raise EOFError
            d += c
        return d
    def pkt(self):
        h = self.recvn(3); n = h[1] | (h[2] << 8)
        return h[0], self.recvn(n)
    def until(self, ptype):
        while True:
            t, p = self.pkt()
            if t == ptype: return p

def main():
    srv, url = fake_subsonic.start(user="demo")
    sub = Subsonic(url, "demo", "pw"); sub.ping()
    port = free_port()
    threading.Thread(target=B.serve, args=(SimpleNamespace(host="127.0.0.1", port=port), sub), daemon=True).start()
    time.sleep(0.3)
    c = Client(port)
    c.send(P.CMD_HELLO, 0, 2, 0); t, p = c.pkt(); assert t == P.PKT_HELLO and p == bytes([2, 0]); print("hello ok")
    c.send(P.CMD_LIST, 0, 0); p = c.until(P.PKT_LIST)
    sort, page, cnt, more = p[:4]; print("list:", cnt, "albums, more =", more)
    for i in range(cnt): print("  ", i, from_screen(p[4 + i * 38: 4 + (i + 1) * 38]))
    assert len(p) == 4 + cnt * 38
    c.send(P.CMD_PLAY, 0, 1)
    p = c.until(P.PKT_TRACK)
    ack, mode, no, tot, dlo, dhi, newc = p[:7]
    title = from_screen(p[7 + 1: 7 + 1 + p[7]]); artist = from_screen(p[71 + 1: 71 + 1 + p[71]]); album = from_screen(p[135 + 1: 135 + 1 + p[135]])
    print(f"track ack={ack} mode={mode} {no}/{tot} dur={dlo | dhi << 8}s cover={newc} :: {title} / {artist} / {album}")
    assert ack == 1 and newc == 1 and len(p) == 7 + 192
    t, cov = c.pkt(); assert t == P.PKT_COVER and len(cov) == 20480; print("cover bytes", len(cov))
    got = 0; sizes = set(); t0 = time.time()
    while got < 8000:
        t, d = c.pkt()
        if t == P.PKT_DIGI: got += len(d) * 2; sizes.add(len(d))
    print("digi samples", got, "packet sizes", sizes, "in %.1fs (paced)" % (time.time() - t0))
    c.send(P.CMD_VOL, 255); assert c.until(P.PKT_VOL) == bytes([11]); print("vol ok")
    c.send(P.CMD_NEXT, 2); p = c.until(P.PKT_TRACK); assert p[0] == 2 and p[2] == 2 and p[6] == 0; print("next ok -> track 2, ack 2")
    c.send(P.CMD_MODE, 1, 3, 5, 0); p = c.until(P.PKT_TRACK); assert p[0] == 3 and p[1] == 1; print("mode->SID ok")
    frames = 0
    while frames < 100:
        t, d = c.pkt()
        if t == P.PKT_SID: assert len(d) % 22 == 0; frames += len(d) // 22
    print("sid frames", frames)
    c.send(P.CMD_STOP, 4); p = c.until(P.PKT_STOP); assert p == bytes([4]); print("stop ok")
    c.send(P.CMD_PLAY, 99, 5); t, p = c.pkt(); print("error packet:", t == P.PKT_ERROR, from_screen(p))
    print("BRIDGE TEST PASSED")

if __name__ == "__main__":
    main()
