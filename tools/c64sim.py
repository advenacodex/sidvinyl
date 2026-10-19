"""Minimal C64 + Ultimate Command Interface simulator (py65) used to test the
SIDVinyl client against the real Python bridge.

Models: 64K RAM + banking ($01), char ROM (generated stand-in glyphs), VIC-II
raster IRQ + register file + renderer (text / multicolour bitmap / sprites),
CIA1 keyboard matrix, CIA2 timer A NMI, SID $D418 capture, UCI over real TCP.
"""
import os, socket, sys, wave
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from py65.devices.mpu6502 import MPU

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bridge"))
from c64gfx import PALETTE  # noqa: E402

PAL_HZ, NTSC_HZ = 985248, 1022727


def make_char_rom():
    """4 KiB stand-in: set 1 (upper/gfx) then set 2 (lower/upper) at $800."""
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 9)
    rom = bytearray(4096)

    def glyph(ch):
        im = Image.new("L", (8, 8), 0)
        ImageDraw.Draw(im).text((0, -2), ch, fill=255, font=font)
        a = np.array(im) > 90
        return bytes(int("".join("1" if v else "0" for v in row), 2) for row in a)

    def ascii_for(code, lower):
        c = code & 0x7F
        if c == 0: return "@"
        if 1 <= c <= 26: return chr(c + (96 if lower else 64))
        if 27 <= c <= 31: return "[£]↑←"[c - 27]
        if 32 <= c <= 63: return chr(c)
        if 65 <= c <= 90: return chr(c) if lower else chr(c)
        return "▒"
    for base, lower in ((0, False), (0x800, True)):
        for code in range(128):
            g = glyph(ascii_for(code, lower)) if code != 32 else bytes(8)
            rom[base + code * 8: base + code * 8 + 8] = g
            rom[base + 1024 + code * 8: base + 1024 + code * 8 + 8] = bytes(b ^ 0xFF for b in g)
    return bytes(rom)


class Mem:
    def __init__(self, sim):
        self.sim = sim
        self.ram = bytearray(65536)

    def __getitem__(self, a):
        if 0xD000 <= a < 0xE000:
            p = self.ram[1] & 7
            if p & 3:
                if p & 4: return self.sim.io_read(a)
                return self.sim.charrom[a - 0xD000]
        return self.ram[a]

    def __setitem__(self, a, v):
        if 0xD000 <= a < 0xE000:
            p = self.ram[1] & 7
            if p & 3 and p & 4:
                return self.sim.io_write(a, v)
        self.ram[a] = v

    def __len__(self): return 65536


class C64:
    def __init__(self, prg_path, pal=True, keymap_src=None, has_uci=True):
        self.has_uci = has_uci
        self.pal = pal
        self.hz = PAL_HZ if pal else NTSC_HZ
        self.line_cyc = 63 if pal else 65
        self.lines = 312 if pal else 263
        self.frame = self.line_cyc * self.lines
        self.charrom = make_char_rom()
        self.mem = Mem(self)
        self.cpu = MPU(memory=self.mem)
        data = open(prg_path, "rb").read()
        load = data[0] | data[1] << 8
        self.mem.ram[load:load + len(data) - 2] = data[2:]
        self.mem.ram[1] = 0x37
        self.cpu.pc = 0x080D
        self.cpu.sp = 0xFF
        self.cpu.p |= 4
        self.cpu.processorCycles = 0
        self.vic = bytearray(64)
        self.vic_flags = 0
        self.rast_cmp = 0
        self.colram = bytearray(1024)
        self.cia1_pra = 0xFF
        self.keys = set()          # pressed (col,row)
        self.cia2_latch = 0
        self.cia2_cra = 0
        self.cia2_mask = 0
        self.cia2_flags = 0
        self.next_ta = None
        self.next_rast = None
        self.vol_log = []          # (cycle, $D418 value)
        self.sid_log = []          # (cycle, reg, value)
        self.sidregs = bytearray(32)
        self.uci = Uci()
        self.ser = None            # ACIA 6551 (SwiftLink layout at $DE00), attach with attach_serial()
        self.next_ser = None
        self.keytab = self._load_keytab()
        self.instr = 0
        self.irq_count = self.nmi_count = 0
        self._sched_raster()

    def attach_serial(self, host, port, baud=19200):
        self.ser = Acia(host, port, baud, self.hz)
        self.next_ser = 0

    # --- keyboard -----------------------------------------------------
    def _load_keytab(self):
        import re
        src = open(os.path.join(HERE, "..", "c64", "ui.s")).read()
        m = re.search(r"keytab:\n(.*?)\n\ns_title", src, re.S)
        vals = [int(x, 16) for x in re.findall(r"\$([0-9A-Fa-f]{2})", m.group(1))]
        assert len(vals) == 64
        return vals

    def press(self, *codes):
        """codes: PETSCII values as the keytab holds them; 'LSHIFT' adds shift."""
        self.keys = set()
        for c in codes:
            if c == "LSHIFT": self.keys.add((1, 7)); continue
            i = self.keytab.index(c)
            self.keys.add((i // 8, i % 8))

    def release(self): self.keys = set()

    # --- time ----------------------------------------------------------
    @property
    def cycles(self): return self.cpu.processorCycles

    def line(self): return (self.cycles % self.frame) // self.line_cyc

    def _sched_raster(self):
        c = self.cycles
        base = c - c % self.frame
        t = base + self.rast_cmp * self.line_cyc
        if t <= c: t += self.frame
        self.next_rast = t

    # --- I/O -------------------------------------------------------------
    def io_read(self, a):
        if a < 0xD400:
            r = a & 0x3F
            if r == 0x11: return (self.vic[0x11] & 0x7F) | ((self.line() >> 1) & 0x80)
            if r == 0x12: return self.line() & 0xFF
            if r == 0x19:
                pend = 0x80 if (self.vic_flags & self.vic[0x1A] & 0x0F) else 0
                return self.vic_flags | 0x70 | pend
            return self.vic[r] if r < 47 else 0xFF
        if a < 0xD800: return self.sidregs[a & 0x1F] if (a & 0x1F) >= 0x19 else 0
        if a < 0xDC00: return 0xF0 | self.colram[a - 0xD800]
        if a < 0xDD00:
            r = a & 0x0F
            if r == 0:
                return 0xFF
            if r == 1:
                v = 0xFF
                for col, row in self.keys:
                    if not (self.cia1_pra >> col) & 1: v &= ~(1 << row)
                return v & 0xFF
            return 0
        if a < 0xDE00:
            r = a & 0x0F
            if r == 0x0D:
                v = self.cia2_flags | (0x80 if self.cia2_flags & self.cia2_mask else 0)
                self.cia2_flags = 0
                return v
            if r == 0: return 0x03
            if r == 0x0E: return self.cia2_cra & ~0x10
            return 0
        if a >= 0xDF00:
            return self.uci.read(a & 0xFF) if self.has_uci else 0xFF
        if self.ser is not None:
            return self.ser.read(a & 3, self)
        return 0xFF

    def io_write(self, a, v):
        if a < 0xD400:
            r = a & 0x3F
            if r == 0x11: self.vic[0x11] = v; self.rast_cmp = (self.rast_cmp & 0xFF) | ((v & 0x80) << 1); self._sched_raster(); return
            if r == 0x12: self.rast_cmp = (self.rast_cmp & 0x100) | v; self._sched_raster(); return
            if r == 0x19: self.vic_flags &= ~v & 0x0F; return
            if r < 47: self.vic[r] = v
            return
        if a < 0xD800:
            r = a & 0x1F
            self.sidregs[r] = v
            self.sid_log.append((self.cycles, r, v))
            if r == 0x18: self.vol_log.append((self.cycles, v))
            return
        if a < 0xDC00: self.colram[a - 0xD800] = v & 15; return
        if a < 0xDD00:
            if (a & 0x0F) == 0: self.cia1_pra = v
            return
        if a < 0xDE00:
            r = a & 0x0F
            if r == 4: self.cia2_latch = (self.cia2_latch & 0xFF00) | v
            elif r == 5: self.cia2_latch = (self.cia2_latch & 0x00FF) | (v << 8)
            elif r == 0x0D:
                if v & 0x80: self.cia2_mask |= v & 0x1F
                else: self.cia2_mask &= ~v & 0x1F
            elif r == 0x0E:
                was = self.cia2_cra & 1
                self.cia2_cra = v
                if v & 1 and not was or v & 0x10:
                    self.next_ta = self.cycles + self.cia2_latch + 1
                if not v & 1: self.next_ta = None
            return
        if a >= 0xDF00:
            self.uci.write(a & 0xFF, v)
        elif self.ser is not None:
            self.ser.write(a & 3, v, self)

    # --- execution ---------------------------------------------------------
    def run(self, max_cycles=None, until=None, poll=2000):
        import time as _t
        cpu = self.cpu
        thr = getattr(self, 'throttle', None)
        if thr and not hasattr(self, '_w0'): self._w0 = _t.time() - self.cycles / self.hz / thr
        end = None if max_cycles is None else cpu.processorCycles + max_cycles
        n = 0
        while True:
            cpu.step()
            n += 1
            c = cpu.processorCycles
            if self.next_rast is not None and c >= self.next_rast:
                self.next_rast += self.frame
                if self.vic[0x1A] & 1: self.vic_flags |= 1
            if self.next_ta is not None and c >= self.next_ta:
                self.next_ta += self.cia2_latch + 1
                self.cia2_flags |= 1
                if self.cia2_mask & 1:
                    cpu.nmi(); self.nmi_count += 1
            if self.next_ser is not None and c >= self.next_ser:
                self.next_ser = self.ser.tick(self, c)
            if self.vic_flags & self.vic[0x1A] & 1 and not cpu.p & 4:
                cpu.irq(); self.irq_count += 1
            if n % poll == 0:
                if end is not None and c >= end: return False
                if until is not None and until(self): return True
                self.uci.pump()
                if self.ser is not None: self.ser.pump()
                if thr:
                    ahead = c / self.hz / thr - (_t.time() - self._w0)
                    if ahead > 0: _t.sleep(ahead)

    def load_symbols(self, lbl):
        self.sym = {}
        for line in open(lbl):
            parts = line.split()
            if len(parts) == 3: self.sym[parts[2].lstrip('.')] = int(parts[1], 16)

    def var(self, name, n=1):
        v = self.peek(self.sym[name], n)
        return v[0] if n == 1 else v

    def seconds(self): return self.cycles / self.hz

    # --- inspection ----------------------------------------------------------
    def peek(self, a, n=1): return bytes(self.mem.ram[a:a + n])

    def screen_text(self):
        base = ((self.vic[0x18] >> 4) & 15) * 0x400
        out = []
        for r in range(25):
            row = ""
            for c in range(40):
                ch = self.mem.ram[base + r * 40 + c] & 0x7F
                if ch == 0: row += "@"
                elif 1 <= ch <= 26: row += chr(96 + ch)
                elif 32 <= ch < 96: row += chr(ch)
                else: row += "#"
            out.append(row.rstrip())
        return "\n".join(out)

    def render(self, border=True):
        v, ram = self.vic, self.mem.ram
        pal = PALETTE.astype(np.uint8)
        img = np.zeros((200, 320, 3), np.uint8)
        bg = v[0x21] & 15
        img[:] = pal[bg]
        d18, ctl1, ctl2 = v[0x18], v[0x11], v[0x16]
        smatrix = ((d18 >> 4) & 15) * 0x400
        if ctl1 & 0x10:
            if ctl1 & 0x20:
                bm = (d18 & 8) * 0x400
                mc = ctl2 & 0x10
                for r in range(25):
                    for c in range(40):
                        sc = ram[smatrix + r * 40 + c]; cc = self.colram[r * 40 + c]
                        for y in range(8):
                            b = ram[bm + r * 320 + c * 8 + y]
                            if mc:
                                cols = (bg, sc >> 4, sc & 15, cc)
                                for px in range(4):
                                    img[r * 8 + y, c * 8 + px * 2: c * 8 + px * 2 + 2] = pal[cols[(b >> (6 - px * 2)) & 3]]
                            else:
                                for px in range(8):
                                    img[r * 8 + y, c * 8 + px] = pal[(sc >> 4) if (b >> (7 - px)) & 1 else (sc & 15)]
            else:
                cb = (d18 & 14) * 0x400
                for r in range(25):
                    for c in range(40):
                        ch = ram[smatrix + r * 40 + c]; fg = self.colram[r * 40 + c]
                        if 0x1000 <= cb < 0x2000: rows = self.charrom[cb - 0x1000 + ch * 8: cb - 0x1000 + ch * 8 + 8]
                        else: rows = ram[cb + ch * 8: cb + ch * 8 + 8]
                        for y in range(8):
                            b = rows[y]
                            for px in range(8):
                                if (b >> (7 - px)) & 1: img[r * 8 + y, c * 8 + px] = pal[fg]
            en = v[0x15]
            for s in range(7, -1, -1):
                if not (en >> s) & 1: continue
                x = v[s * 2] | (256 if (v[0x10] >> s) & 1 else 0)
                y = v[s * 2 + 1]
                xe = (v[0x1D] >> s) & 1; ye = (v[0x17] >> s) & 1
                ptr = ram[smatrix + 0x3F8 + s]
                col = pal[v[0x27 + s] & 15]
                for sy in range(21):
                    b3 = ram[ptr * 64 + sy * 3: ptr * 64 + sy * 3 + 3]
                    for sx in range(24):
                        if (b3[sx // 8] >> (7 - sx % 8)) & 1:
                            for dy in range(1 + ye):
                                for dx in range(1 + xe):
                                    px = x - 24 + sx * (1 + xe) + dx
                                    py = y - 50 + sy * (1 + ye) + dy
                                    if 0 <= px < 320 and 0 <= py < 200: img[py, px] = col
        im = Image.fromarray(img)
        if border:
            full = Image.new("RGB", (320 + 64, 200 + 64), tuple(pal[v[0x20] & 15]))
            full.paste(im, (32, 32)); im = full
        return im.resize((im.width * 2, im.height * 2), Image.NEAREST)

    def vol_wav(self, path, rate=44100, t0=0.0, t1=None):
        """Sample-and-hold of the $D418 writes (digi playback) -> mono WAV."""
        if not self.vol_log: return 0
        cyc = np.array([c for c, _ in self.vol_log], dtype=np.int64)
        val = np.array([v & 15 for _, v in self.vol_log], dtype=np.float64)
        t1 = t1 if t1 is not None else cyc[-1] / self.hz
        ts = np.arange(int(t0 * rate), int(t1 * rate)) / rate
        idx = np.searchsorted(cyc, (ts * self.hz).astype(np.int64), side="right") - 1
        idx = np.clip(idx, 0, len(val) - 1)
        s = (val[idx] - 7.5) / 7.5
        s -= np.convolve(s, np.ones(2000) / 2000, mode="same")   # remove DC drift
        pcm = (np.clip(s, -1, 1) * 30000).astype("<i2")
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm.tobytes())
        return len(pcm)


class Uci:
    """Ultimate command interface at $DF1C-$DF1F, TCP target only, real sockets."""
    def __init__(self):
        self.state = 0
        self.cmd = bytearray()
        self.data = bytearray()
        self.status = bytearray()
        self.socks = {}
        self.next_id = 0
        self.stats = {"cmds": 0, "read_bytes": 0, "write_bytes": 0}

    def pump(self): pass

    def read(self, r):
        if r == 0x1C:
            return ((0x80 if self.data else 0) | (0x40 if self.status else 0) | (self.state << 4))
        if r == 0x1D: return 0xC9
        if r == 0x1E: return self.data.pop(0) if self.data else 0
        if r == 0x1F: return self.status.pop(0) if self.status else 0
        return 0xFF

    def write(self, r, v):
        if r == 0x1D:
            self.cmd.append(v)
        elif r == 0x1C:
            if v & 1: self.push()
            if v & 2: self.state = 0; self.data.clear(); self.status.clear()
            if v & 4: self.state = 0; self.data.clear(); self.status.clear(); self.cmd.clear()
            if v & 8: pass

    def respond(self, data=b"", status="00,OK"):
        self.data = bytearray(data); self.status = bytearray(status.encode())
        self.state = 2

    def push(self):
        c = bytes(self.cmd); self.cmd.clear()
        self.stats["cmds"] += 1
        if len(c) < 2 or c[0] != 3:
            return self.respond(status="02,UNKNOWN TARGET")
        op = c[1]
        try:
            if op == 0x07:     # open tcp: port lo hi, host\0
                port = c[2] | c[3] << 8
                host = c[4:].split(b"\0")[0].decode()
                s = socket.create_connection((host, port), timeout=5)
                s.setblocking(False)
                sid = self.next_id; self.next_id += 1
                self.socks[sid] = s
                return self.respond(bytes([sid]))
            if op == 0x10:     # read
                s = self.socks[c[2]]; want = c[3] | c[4] << 8
                try:
                    d = s.recv(want)
                    if not d: return self.respond(b"\0\0", "01,CONNECTION CLOSED BY HOST")
                except BlockingIOError:
                    d = b""
                self.stats["read_bytes"] += len(d)
                return self.respond(bytes([len(d) & 255, len(d) >> 8]) + d)
            if op == 0x11:     # write
                s = self.socks[c[2]]; payload = c[3:]
                s.setblocking(True); s.sendall(payload); s.setblocking(False)
                self.stats["write_bytes"] += len(payload)
                return self.respond()
            if op == 0x09:
                self.socks.pop(c[2]).close()
                return self.respond()
        except OSError as e:
            return self.respond(status=f"02,{e}")
        return self.respond(status="02,UNSUPPORTED")


class Acia:
    """6551 ACIA, SwiftLink layout: DATA/STATUS/CMD/CTRL at $DE00-$DE03.

    Receive: single-byte RDR, one byte per 10 bit-times; a byte arriving while RDRF is
    still set is lost (overrun) -- exactly what the flow control must prevent.
    The TCP connection is opened when DTR is first enabled (like VICE's rsdev socket).
    """
    def __init__(self, host, port, baud, hz):
        self.host, self.port = host, port
        self.cpb = int(10 * hz / baud)
        self.sock = None
        self.rxq = bytearray()
        self.rdr = 0; self.rdrf = False; self.over = False; self.irq = False
        self.cmd = 0; self.ctrl = 0
        self.tx_free = 0
        self.stats = {"rx": 0, "tx": 0, "overruns": 0}
        self.txlog = bytearray()

    def _open(self):
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=5)
            self.sock.setblocking(False)

    def pump(self):
        if self.sock is None: return
        try:
            d = self.sock.recv(4096)
            if d: self.rxq += d
        except BlockingIOError:
            pass

    def read(self, r, sim):
        if r == 0:
            self.rdrf = False; self.over = False
            return self.rdr
        if r == 1:
            v = (0x08 if self.rdrf else 0) | (0x04 if self.over else 0) | (0x80 if self.irq else 0) | \
                (0x10 if sim.cycles >= self.tx_free else 0)
            self.irq = False
            return v
        if r == 2: return self.cmd
        return self.ctrl

    def write(self, r, v, sim):
        if r == 0:
            if self.sock is not None:
                self.sock.setblocking(True); self.sock.sendall(bytes([v])); self.sock.setblocking(False)
            self.stats["tx"] += 1
            self.txlog.append(v)
            self.tx_free = sim.cycles + self.cpb
        elif r == 1:               # programmed reset
            self.rdrf = False; self.over = False; self.irq = False; self.cmd = 0x02
        elif r == 2:
            self.cmd = v
            if v & 1: self._open()
        else:
            self.ctrl = v

    def tick(self, sim, now):
        """Deliver the next received byte; returns the cycle of the next delivery slot."""
        if self.rxq and self.cmd & 1:
            b = self.rxq.pop(0)
            self.stats["rx"] += 1
            if self.rdrf:
                self.over = True; self.stats["overruns"] += 1
            else:
                self.rdr = b; self.rdrf = True
                if not self.cmd & 2:           # receiver interrupt enabled -> NMI
                    self.irq = True
                    sim.cpu.nmi(); sim.nmi_count += 1
        return now + self.cpb
