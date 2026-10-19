"""Boot the PRG in the C64 simulator against the real bridge (demo library) and
exercise: connect, list, play (digi), mode switch (SID), next track, pause, volume,
quit to menu.  Screenshots and a digi WAV land in $OUT (default /tmp/sidvinyl_out)."""
import os, subprocess, sys, time, socket, wave
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import c64sim, patch_host

OUT = os.environ.get("OUT", "/tmp/sidvinyl_out"); os.makedirs(OUT, exist_ok=True)
K_RETURN, K_SPACE, K_DOWN = 0x0D, 0x20, 0x11
ck = lambda c: ord(c)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def start_bridge(port):
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bridge", "sidvinyl_bridge.py"), "--demo",
                          "--host", "127.0.0.1", "--port", str(port)] + (["-v"] if os.environ.get("BRIDGE_V") else []), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try: socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
        except OSError: time.sleep(0.2)
    return p

def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond: check.failed += 1
check.failed = 0

def main():
    port = free_port(); br = start_bridge(port)
    prg = os.path.join(OUT, "t.prg")
    open(prg, "wb").write(patch_host.patch(open(os.path.join(ROOT, "c64", "sidvinyl.prg"), "rb").read(), f"127.0.0.1:{port}"))
    serial = os.environ.get("TRANSPORT", "uci") == "serial"
    print("transport:", "serial (ACIA/SwiftLink)" if serial else "UCI")
    sim = c64sim.C64(prg, has_uci=not serial); sim.throttle = 0.8
    if serial: sim.attach_serial("127.0.0.1", port)
    sim.load_symbols(os.path.join(ROOT, "c64", "vinyl.lbl"))
    V = sim.var
    secs = lambda s: int(s.frame * 0 + s.hz)

    def tap(code, frames=4, shift=False):
        sim.press(*(["LSHIFT"] if shift else []), code); sim.run(max_cycles=sim.frame * frames)
        sim.release(); sim.run(max_cycles=sim.frame * 3)
    def shot(name): sim.render().save(os.path.join(OUT, name + ".png"))
    def runs(seconds): sim.run(max_cycles=int(sim.hz * seconds))
    def wait_play(limit=40):
        # serial link: cover (20 KB) + disc take ~6 s at 19200 baud before the first audio
        sim.run(until=lambda s: s.var("playing") == 1 and s.var("prebuf") == 0, max_cycles=int(sim.hz * limit))
        return V("playing") == 1
    def title():
        return bytes(sim.peek(sim.sym["LINEBUF"] if "LINEBUF" in sim.sym else 0xE900, 1 + 64))

    sim.run(max_cycles=3_000_000)
    if serial:
        check("SERIAL" in sim.screen_text(), "serial link auto-selected when no UCI answers")
    else:
        check("192.168.1.100" not in sim.screen_text() and f"127.0.0.1:{port}" in sim.screen_text(), "setup screen shows patched bridge address")
    check(V("fps") == 50, "PAL detected (fps=%d)" % V("fps"))
    shot("setup")
    tap(K_RETURN)
    sim.run(until=lambda s: s.var("list_count") > 0 and s.var("loading") == 0, max_cycles=60_000_000)
    check(V("app_state") == 1 and V("list_count") == 6, "connected, menu with 6 albums")
    shot("menu")
    tap(K_DOWN); tap(K_DOWN)
    check(V("sel") == 2, "cursor down x2 -> sel=2")
    tap(K_DOWN, 4, shift=True)    # shift+down = up
    check(V("sel") == 1, "shift+down = up -> sel=1")
    tap(ord("S"))
    sim.run(until=lambda s: s.var("loading") == 0, max_cycles=60_000_000)
    check(V("sort") == 1, "sort key cycles to sort=1")
    tap(ord("S"), ); tap(ord("S")); tap(ord("S")); tap(ord("S")); tap(ord("S"))
    sim.run(until=lambda s: s.var("loading") == 0, max_cycles=60_000_000)
    check(V("sort") == 0 and V("list_count") == 6, "sort wraps back to album A-Z")
    tap(K_DOWN, 4, shift=False)
    tap(K_RETURN)
    check(V("app_state") == 2, "RETURN enters now-playing")
    t_start = sim.seconds(); ok = wait_play(); print("      time to first audio: %.1f s emulated" % (sim.seconds() - t_start)); runs(2); shot("np_digi")
    w0 = len(sim.vol_log); runs(3)
    rate = (len(sim.vol_log) - w0) / 3        # one $D418 write per digi sample
    lo, hi = (3300, 3700) if serial else (7800, 8200)
    check(lo < rate < hi, "digi NMI rate %.0f/s (%s)" % (rate, "~3500 serial" if serial else "~8000"))
    check(V("mode") == 0 and V("playing") == 1, "digi mode playing")
    sec = V("sec_lo"); check(sec >= 3, "elapsed time counter running (%ds)" % sec)
    n = sim.vol_wav(os.path.join(OUT, "digi.wav"), rate=8000, t0=1.5)
    x = np.frombuffer(open(os.path.join(OUT, "digi.wav"), "rb").read()[44:], "<i2").astype(float)
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x)))); f = np.fft.rfftfreq(len(x), 1 / 8000)
    check(x.std() > 1500, "digi audio has signal (rms %.0f)" % x.std())
    peak = f[np.argmax(sp[5:]) + 5]; print("      dominant freq %.0f Hz" % peak)

    # ---- switch to SID chiptune mode
    nlog = len(sim.sid_log); tmark = sim.cycles
    tap(ord("M"))
    wait_play(); runs(5); shot("np_sid")
    check(V("mode") == 1, "M switches to SID mode")
    log = [e for e in sim.sid_log[nlog:] if e[0] > tmark + sim.hz]
    frames = sum(1 for e in log if e[1] == 0x18)
    dur = (sim.cycles - tmark - sim.hz) / sim.hz
    check(40 <= frames / dur <= 60, "SID frames %.1f/s (~50)" % (frames / dur))
    cur, per = {}, [set(), set(), set()]
    for c, r, v in log:
        cur[r] = v
        if r == 0x18:
            for vi in range(3):
                b = vi * 7
                if cur.get(b + 4, 0) & 1:
                    per[vi].add(round((cur.get(b, 0) | cur.get(b + 1, 0) << 8) * sim.hz / 16777216))
    print("      gated pitches per voice (Hz):", [sorted(p)[:5] for p in per])
    check(sum(1 for p in per if p) >= 2, "at least two SID voices sound")
    check(any(40 < f < 130 for f in per[0] | per[1] | per[2]), "bass pitch (~78 Hz) detected")
    check(sim.sidregs[0x18] & 15 > 0, "SID master volume set")
    t_before = V("trk_no")

    # ---- pause / resume
    tap(K_SPACE); check(V("paused") == 1, "SPACE pauses")
    shot("np_paused")
    nm = sim.sid_log.__len__(); runs(1)
    check(len(sim.sid_log) - nm < 6, "no SID writes while paused")
    tap(K_SPACE); check(V("paused") == 0, "SPACE resumes")
    runs(1)
    check(len(sim.sid_log) - nm > 30, "SID frames resume")

    # ---- volume
    v0 = V("vol_level"); tap(ord("-")); tap(ord("-")); runs(3.5)  # applied after the audio already queued in TCP drains
    check(V("vol_level") == v0 - 2, "volume down x2 (%d -> %d)" % (v0, V("vol_level")))

    # ---- next track
    tap(ord("N")); wait_play(); runs(4)
    check(V("trk_no") == t_before + 1, "N plays next track (%d -> %d)" % (t_before, V("trk_no")))
    check(V("sec_lo") <= 4 and V("playing") == 1, "new track restarted timer (%ds)" % V("sec_lo"))
    shot("np_next")
    tap(ord("P")); wait_play(); runs(3)
    check(V("trk_no") == t_before, "P goes back (%d)" % V("trk_no"))
    tap(ord("M")); wait_play(); runs(3)
    check(V("mode") == 0, "M switches back to digi")

    # ---- quit to menu
    tap(ord("Q")); runs(2)
    check(V("app_state") == 1, "Q returns to the album menu")
    shot("menu_after")
    tap(K_RETURN); wait_play(); runs(3)
    check(V("app_state") == 2 and V("playing") == 1, "second play from menu works")
    shot("np_again")
    br.kill()
    if serial:
        print("      acia stats:", sim.ser.stats)
        check(sim.ser.stats["overruns"] == 0, "no receiver overruns (flow control works)")
    print("FAILED: %d" % check.failed if check.failed else "ALL PASSED")
    sys.exit(1 if check.failed else 0)

if __name__ == "__main__":
    main()
