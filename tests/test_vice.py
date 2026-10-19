"""Run the real PRG in VICE (x64sc, headless via Xvfb) against the real bridge.

The player talks to the bridge through VICE's emulated SwiftLink/ACIA (-acia1 ... -rsdev1
host:port).  Keys are injected with xdotool, state is read through VICE's binary monitor
(memory get), and the SID output is captured with -sounddev wav.

Needs: x64sc (VICE 3.x), Xvfb, xdotool, ImageMagick `import`, and the C64 ROM images in
$VICE_ROMS (a directory with kernal/basic/chargen .bin files; defaults to /tmp/vsys/C64
with the usual VICE system dir layout in /tmp/vsys).
"""
import os, signal, socket, struct, subprocess, sys, time, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import patch_host  # noqa: E402

OUT = os.environ.get("OUT", "/tmp/sidvinyl_vice"); os.makedirs(OUT, exist_ok=True)
SYSDIR = os.environ.get("VICE_SYSDIR", "/tmp/vsys")
DISPLAY = ":97"
MONPORT = 6510


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Monitor:
    """Minimal VICE binary-monitor client (memory get).

    A monitor connection must NOT stay open: while a client is attached VICE drops the X keyboard
    events, so every read opens and closes its own connection."""
    def __init__(self, port):
        self.port = port

    @staticmethod
    def _frame(s):
        h = b""
        while len(h) < 12:
            c = s.recv(12 - len(h))
            if not c: raise EOFError
            h += c
        blen = struct.unpack("<I", h[2:6])[0]
        body = b""
        while len(body) < blen:
            body += s.recv(blen - len(body))
        return h[6], h[7], struct.unpack("<I", h[8:12])[0], body

    def _cmd(self, ctype, body=b""):
        s = None
        for _ in range(100):
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=5); break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("cannot reach VICE binary monitor")
        try:
            s.sendall(struct.pack("<BBIIB", 2, 2, len(body), 1, ctype) + body)
            while True:
                rtype, err, rid, data = self._frame(s)
                if rid == 1:
                    return rtype, err, data
        finally:
            s.close()

    def mem(self, addr, n=1):
        body = struct.pack("<BHHBH", 0, addr, addr + n - 1, 0, 0)
        _, err, data = self._cmd(0x01, body)
        ln = struct.unpack("<H", data[:2])[0]
        return data[2:2 + ln]

    def quit(self):
        try: self._cmd(0xBB)
        except Exception: pass


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond: check.failed += 1
check.failed = 0


def main():
    syms = {}
    for line in open(os.path.join(ROOT, "c64", "vinyl.lbl")):
        p = line.split()
        if len(p) == 3: syms[p[2].lstrip(".")] = int(p[1], 16)

    bport = free_port()
    bridge = subprocess.Popen([sys.executable, os.path.join(ROOT, "bridge", "sidvinyl_bridge.py"), "--demo", "--host", "127.0.0.1",
                               "--port", str(bport)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try: socket.create_connection(("127.0.0.1", bport), timeout=0.2).close(); break
        except OSError: time.sleep(0.2)
    prg = os.path.join(OUT, "t.prg")
    open(prg, "wb").write(patch_host.patch(open(os.path.join(ROOT, "c64", "sidvinyl.prg"), "rb").read(), f"127.0.0.1:{bport}"))
    wav = os.path.join(OUT, "vice_audio.wav")
    if os.path.exists(wav): os.remove(wav)

    xvfb = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", "1024x768x24"], stderr=subprocess.DEVNULL)
    env = dict(os.environ, DISPLAY=DISPLAY, HOME=os.path.join(OUT, "home"))
    os.makedirs(env["HOME"], exist_ok=True)
    time.sleep(1.5)
    rom = os.path.join(SYSDIR, "C64")
    vice = subprocess.Popen(
        ["x64sc", "-directory", SYSDIR, "-kernal", f"{rom}/kernal-901227-03.bin", "-basic", f"{rom}/basic-901226-01.bin",
         "-chargen", f"{rom}/chargen-901225-01.bin",
         "-acia1", "-acia1mode", "1", "-acia1base", "0xDE00", "-acia1irq", "0", "-myaciadev", "0",
         "-rsdev1", f"127.0.0.1:{bport}", "-rsdev1baud", "19200",
         "-sounddev", "wav", "-soundarg", wav, "-soundoutput", "1",
         "-binarymonitor", "-binarymonitoraddress", f"ip4://127.0.0.1:{MONPORT}",
         "+confirmonexit", "-autostartprgmode", "1", "-autostart", prg],
        env=env, stdout=open(os.path.join(OUT, "vice.log"), "w"), stderr=subprocess.STDOUT)
    time.sleep(12)                          # connecting the binary monitor during autostart stalls the boot
    mon = Monitor(MONPORT)
    V = lambda name, n=1: (mon.mem(syms[name], n)[0] if n == 1 else mon.mem(syms[name], n))

    def xd(*a): return subprocess.run(["xdotool", *a], env=env, capture_output=True, text=True).stdout.strip()
    wid = ""
    for _ in range(50):
        wid = xd("search", "--onlyvisible", "--class", "x64sc").split("\n")[0]
        if wid: break
        time.sleep(0.3)
    xd("windowfocus", wid)

    def key(k, hold=0.15):
        xd("windowfocus", wid)                 # no window manager: give the VICE window X input focus explicitly
        xd("key", "--delay", str(int(hold * 1000)), k); time.sleep(0.2)

    def shot(name):
        subprocess.run(["import", "-window", wid, os.path.join(OUT, name + ".png")], env=env)

    def wait_for(fn, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if fn(): return True
            time.sleep(1.0)      # every monitor read pauses VICE briefly; >1 Hz makes the ACIA lose bytes
        print("      (timeout waiting for %s) state: %s" % (what, {n: V(n) for n in ("app_state", "playing", "prebuf", "paused", "mode", "acia_ovr", "list_count", "loading")})); return False

    t_audio = {}
    try:
        wait_for(lambda: V("fps") in (50, 60), 60, "boot")
        print("      boot state:", V("app_state"), V("fps"), V("transport"))
        check(V("transport") == 1, "VICE: no UCI -> serial link selected automatically")
        check(V("fps") == 50, "VICE: PAL detected")
        shot("v_setup")
        key("Return")
        ok = wait_for(lambda: V("list_count") > 0 and V("loading") == 0, 40, "album list")
        check(ok and V("app_state") == 1 and V("list_count") == 6, "VICE: connected through the emulated ACIA, 6 albums listed")
        shot("v_menu")
        t0 = time.time(); key("Return")
        ok = wait_for(lambda: V("playing") == 1 and V("prebuf") == 0, 90, "first audio")
        check(ok, "VICE: digi playback starts (%.1f s after RETURN)" % (time.time() - t0))
        check(V("dsel") == 1 and V("mode") == 0, "VICE: bridge selected the 3.5 kHz serial digi profile")
        t_audio["digi_start"] = time.time()
        time.sleep(6); shot("v_digi")
        s1 = V("sec_lo"); time.sleep(3); s2 = V("sec_lo")
        check(s2 - s1 in (2, 3, 4), "VICE: elapsed time advances in real time (%d -> %d)" % (s1, s2))
        t_audio["digi_end"] = time.time()
        key("m")
        ok = wait_for(lambda: V("mode") == 1 and V("playing") == 1 and V("prebuf") == 0, 30, "SID mode")
        check(ok, "VICE: M switches to SID chiptune mode")
        t_audio["sid_start"] = time.time()
        time.sleep(7); shot("v_sid")
        t_audio["sid_end"] = time.time()
        key("space"); time.sleep(0.5)
        check(V("paused") == 1, "VICE: SPACE pauses")
        shot("v_paused"); key("space"); time.sleep(0.5)
        check(V("paused") == 0, "VICE: SPACE resumes")
        n0 = V("trk_no"); key("n")
        ok = wait_for(lambda: V("trk_no") == n0 + 1 and V("playing") == 1 and V("prebuf") == 0, 30, "next track")
        check(ok, "VICE: N plays the next track (%d -> %d)" % (n0, V("trk_no")))
        key("q")
        ok = wait_for(lambda: V("app_state") == 1, 15, "menu")
        check(ok, "VICE: Q returns to the album menu")
        key("Return")
        ok = wait_for(lambda: V("app_state") == 2 and V("playing") == 1 and V("prebuf") == 0, 60, "replay")
        check(ok, "VICE: second play from the menu works")
        time.sleep(3); shot("v_again")
    finally:
        mon.quit()
        try: vice.wait(timeout=10)
        except Exception: vice.kill()
        xvfb.terminate(); bridge.kill()
        print("      bridge log tail:", bridge.stdout.read().decode().strip().splitlines()[-3:])

    # ---- analyse what the emulated SID actually produced
    if os.path.exists(wav) and os.path.getsize(wav) > 1000:
        w = wave.open(wav); rate = w.getframerate(); ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float)
        if ch > 1: x = x.reshape(-1, ch).mean(axis=1)
        t0 = t_audio.get("digi_start", 0)

        def seg(a, b):
            return x[int((a - base) * rate): int((b - base) * rate)]
        # the wav starts when VICE starts; estimate its origin from the amount of audio recorded
        base = t_audio["sid_end"] + 0 - len(x) / rate - 0   # approx: last audio sample ~ time of kill
        base = time.time() - len(x) / rate - 3              # kill happened a few seconds after sid_end
        for name in ("digi", "sid"):
            if f"{name}_start" in t_audio:
                a = seg(t_audio[f"{name}_start"] + 1, t_audio[f"{name}_end"] - 0.5)
                if len(a) > rate:
                    sp = np.abs(np.fft.rfft(a * np.hanning(len(a)))); f = np.fft.rfftfreq(len(a), 1 / rate)
                    sp[f < 30] = 0
                    print(f"      {name} segment: rms {a.std():.0f}, peak freq {f[np.argmax(sp)]:.0f} Hz")
                    check(a.std() > 200, f"VICE audio: {name} segment has signal (rms {a.std():.0f})")
    else:
        check(False, "VICE audio capture produced a wav")
    print("FAILED: %d" % check.failed if check.failed else "ALL PASSED")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
