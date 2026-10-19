"""Audio decoding and the two C64 encoders.

DigiEncoder      PCM 8 kHz -> packed 4-bit samples (noise-shaped) for $D418 playback.
ChiptuneEncoder  PCM 16 kHz -> 50/60 Hz SID register frames (3 voices) using
                 spectral analysis: bass tracker, two melodic voices (harmonic
                 product spectrum) and drum detection on voice 3.
"""
import collections
import math
import subprocess

import numpy as np

SID_CLOCK_PAL = 985248
SID_CLOCK_NTSC = 1022727

FILTER_DIGI = "highpass=f=50,dynaudnorm=f=200:g=9:p=0.92:m=8,alimiter=limit=0.95"
FILTER_SID = "dynaudnorm=f=250:g=7:p=0.9:m=8,alimiter=limit=0.95"


def start_decoder(source, rate, start=0.0, filters=None):
    """Spawn ffmpeg decoding `source` (URL or path) to mono s16le at `rate`."""
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", source, "-vn", "-ac", "1", "-ar", str(rate)]
    if filters:
        cmd += ["-af", filters]
    cmd += ["-f", "s16le", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 16)


def read_pcm(proc, nsamples):
    """Read exactly nsamples int16 samples (fewer at EOF). Returns np.int16 array."""
    need = nsamples * 2
    buf = b""
    while len(buf) < need:
        chunk = proc.stdout.read(need - len(buf))
        if not chunk:
            break
        buf += chunk
    return np.frombuffer(buf[: len(buf) & ~1], dtype="<i2")


# ------------------------------------------------------------------ digi
class DigiEncoder:
    """16-level (4-bit) quantiser with first-order noise shaping."""

    def __init__(self, seed=1):
        self.err = 0.0
        self.rng = np.random.default_rng(seed)
        self.spare = None            # odd sample carried to the next call

    def encode(self, pcm, gain=1.0):
        x = pcm.astype(np.float64) / 32768.0 * gain
        v = np.clip(x, -1.0, 1.0) * 7.5 + 7.5
        if self.spare is not None:
            v = np.concatenate(([self.spare], v))
            self.spare = None
        if len(v) & 1:
            self.spare = v[-1]
            v = v[:-1]
        dith = self.rng.uniform(-0.2, 0.2, len(v)).tolist()
        out = bytearray(len(v))
        err = self.err
        for i, s in enumerate(v.tolist()):
            t = s + 0.75 * err + dith[i]
            q = int(t + 0.5)
            if q < 0:
                q = 0
            elif q > 15:
                q = 15
            err = t - q
            if err > 1.5:
                err = 1.5
            elif err < -1.5:
                err = -1.5
            out[i] = q
        self.err = err
        q = np.frombuffer(bytes(out), dtype=np.uint8)
        return ((q[0::2] << 4) | q[1::2]).astype(np.uint8).tobytes()


# ------------------------------------------------------------------ chiptune
WAVE_TRI, WAVE_SAW, WAVE_PULSE, WAVE_NOISE = 0x10, 0x20, 0x40, 0x80


def midi_of(f):
    return 69.0 + 12.0 * math.log2(f / 440.0)


def hz_of(m):
    return 440.0 * 2.0 ** ((m - 69.0) / 12.0)


class _PitchTrack:
    """Quantises a noisy pitch estimate to stable semitones."""

    def __init__(self):
        self.cur = None
        self.pending = None
        self.count = 0
        self.silent = 0
        self.changed = False

    def update(self, f, confirm=2, min_gap=0.65):
        self.changed = False
        if f is None:
            self.silent += 1
            self.pending, self.count = None, 0
            if self.silent >= 2 and self.cur is not None:
                self.cur = None
            return self.cur
        self.silent = 0
        m = midi_of(f)
        if self.cur is None:
            self.cur = int(round(m))
            self.changed = True
        elif abs(m - self.cur) > min_gap:
            cand = int(round(m))
            if cand == self.pending:
                self.count += 1
            else:
                self.pending, self.count = cand, 1
            if self.count >= confirm:
                self.cur = cand
                self.pending, self.count = None, 0
                self.changed = True
        else:
            self.pending, self.count = None, 0
        return self.cur


class _Voice:
    def __init__(self, wave, ad, sr, pw):
        self.wave, self.ad, self.sr, self.pw = wave, ad, sr, pw
        self.freq = 0
        self.gate = 0
        self.low_frames = 0          # frames to hold the gate low (retrigger gap)

    def retrigger(self):
        self.low_frames = 1

    def bytes(self, pw=None):
        pwv = self.pw if pw is None else pw
        gate = 0 if self.low_frames > 0 else self.gate
        if self.low_frames > 0:
            self.low_frames -= 1
        return bytes([self.freq & 0xFF, (self.freq >> 8) & 0xFF, pwv & 0xFF, (pwv >> 8) & 0x0F,
                      (self.wave | gate) & 0xFF, self.ad, self.sr])


class ChiptuneEncoder:
    RATE = 16000

    def __init__(self, fps=50, clock=SID_CLOCK_PAL):
        self.fps = float(fps)
        self.clock = clock
        self.vol_scale = 1.0
        self.k = 0                                  # frame counter
        self.buf = np.zeros(0, dtype=np.float32)
        self.base = 0                               # absolute index of buf[0]
        self.win_m = np.hanning(2048).astype(np.float64)
        self.win_b = np.hanning(4096).astype(np.float64)
        self.norm_m = self.win_m.sum() / 2.0
        self.norm_b = self.win_b.sum() / 2.0
        self.hz_m = np.fft.rfftfreq(4096, 1.0 / self.RATE)     # 3.9 Hz bins
        self.bin = self.RATE / 4096.0
        self.ref = 0.02                                          # slow peak follower (amplitude)
        self.prev_amp = None
        self.flux_hist = {b: collections.deque(maxlen=60) for b in ("lo", "mid", "hi")}
        self.tr_bass = _PitchTrack()
        self.tr_lead = _PitchTrack()
        self.tr_harm = _PitchTrack()
        self.v1 = _Voice(WAVE_PULSE, 0x00, 0xF1, 0x0900)         # bass
        self.v2 = _Voice(WAVE_PULSE, 0x04, 0xD1, 0x0800)         # lead
        self.v3 = _Voice(WAVE_SAW, 0x04, 0xB1, 0x0400)           # harmony / drums
        self.drum_q = collections.deque()
        self.drum_cool = 0
        self.vol = 8.0
        self.harm_wave = WAVE_SAW
        self.v3_is_drum = False
        self.last_rms_db = -90.0

    # -- helpers ------------------------------------------------------------
    def _reg(self, f):
        return int(max(0, min(65535, round(f * 16777216.0 / self.clock))))

    def feed(self, pcm_int16):
        """Add PCM and return a list of finished 22-byte SID frames."""
        self.buf = np.concatenate((self.buf, pcm_int16.astype(np.float32) / 32768.0))
        frames = []
        while True:
            end = int(round((self.k + 1) * self.RATE / self.fps))
            if end - self.base > len(self.buf):
                break
            frames.append(self._frame(end))
            self.k += 1
            keep = max(0, (end - self.base) - 4096)
            if keep > 8192:
                self.buf = self.buf[keep:]
                self.base += keep
        return frames

    def _window(self, end, n):
        e = end - self.base
        s = e - n
        seg = self.buf[max(0, s):e]
        if len(seg) < n:
            seg = np.concatenate((np.zeros(n - len(seg), dtype=np.float32), seg))
        return seg.astype(np.float64)

    # -- analysis -------------------------------------------------------------
    def _hps(self, amp, lo_hz, hi_hz, mask=None):
        """Harmonic product spectrum pick. Returns (freq_hz, level) or (None, 0)."""
        lm = np.log(amp + 1e-5)
        if mask is not None:
            lm = np.where(mask, np.log(1e-5), lm)
        kmin = int(lo_hz / self.bin)
        kmax = min(int(hi_hz / self.bin), (len(amp) - 1) // 3)
        k = np.arange(kmin, kmax)
        score = lm[k] + lm[2 * k] + lm[3 * k]
        i = int(np.argmax(score))
        kk = int(k[i])
        # prefer sub-octave if it is nearly as good (avoid octave-up errors)
        if kk // 2 >= kmin:
            k2 = kk // 2
            lo2 = lm[k2] + lm[2 * k2] + lm[3 * k2]
            if lo2 > score[i] - 0.9 and amp[k2] > 0.4 * amp[kk]:
                kk = k2
        a = amp[kk]
        lo = max(0, kk - 40)
        floor = float(np.median(amp[lo: kk + 40]))
        if a < max(0.004, 0.025 * self.ref) or a < 3.0 * floor:
            return None, 0.0
        d = self._parab(lm, kk)
        return (kk + d) * self.bin, float(a)

    @staticmethod
    def _parab(lm, k):
        if k <= 0 or k >= len(lm) - 1:
            return 0.0
        a, b, c = lm[k - 1], lm[k], lm[k + 1]
        den = a - 2 * b + c
        if den == 0:
            return 0.0
        return float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))

    def _bass(self, end):
        x = self._window(end, 4096)
        mag = np.abs(np.fft.rfft(x * self.win_b)) / self.norm_b
        kmin, kmax = int(38 / self.bin), int(230 / self.bin)
        seg = mag[kmin:kmax]
        i = int(np.argmax(seg))
        k = kmin + i
        a = mag[k]
        if k // 2 >= kmin and mag[k // 2] > 0.5 * a:
            k //= 2
            a = mag[k]
        floor = float(np.median(mag[max(0, k - 60): k + 60]))
        if a < max(0.004, 0.03 * self.ref) or a < 3.0 * floor:
            return None, 0.0
        lm = np.log(mag + 1e-5)
        return (k + self._parab(lm, k)) * self.bin, float(a)

    def _flux(self, amp):
        if self.prev_amp is None:
            self.prev_amp = amp
            return {"lo": 0.0, "mid": 0.0, "hi": 0.0}
        diff = np.maximum(0.0, amp - self.prev_amp)
        self.prev_amp = amp
        b = self.bin

        def band(a, z):
            return float(diff[int(a / b): int(z / b)].sum())
        return {"lo": band(50, 160), "mid": band(1500, 4000), "hi": band(5000, 7800)}

    def _onset(self, name, value, floor):
        h = self.flux_hist[name]
        thr = floor
        if len(h) >= 10:
            arr = np.fromiter(h, dtype=float)
            thr = max(floor, arr.mean() + 2.2 * arr.std())
        h.append(value)
        return value > thr

    # -- frame -------------------------------------------------------------------
    def _frame(self, end):
        xm = self._window(end, 2048)
        amp = np.abs(np.fft.rfft(xm * self.win_m, 4096)) / self.norm_m
        rms = float(np.sqrt(np.mean(self._window(end, 1024) ** 2)) + 1e-9)
        self.last_rms_db = 20 * math.log10(rms)
        self.ref = max(self.ref * 0.9985, float(amp.max()))
        silent = self.last_rms_db < -58

        # bass + two melodic voices
        fb, _ = (None, 0.0) if silent else self._bass(end)
        fl, _ = (None, 0.0) if silent else self._hps(amp, 180.0, 1500.0)
        fh = None
        if fl is not None:
            mask = np.zeros(len(amp), dtype=bool)
            for h in range(1, 9):
                c = fl * h / self.bin
                w = 2.0 + 0.02 * c
                mask[max(0, int(c - w)): int(c + w) + 1] = True
            fh, _ = self._hps(amp, 200.0, 2400.0, mask=mask)

        mb = self.tr_bass.update(fb)
        ml = self.tr_lead.update(fl)
        mh = self.tr_harm.update(fh)

        # drums
        flux = self._flux(amp)
        lo = self._onset("lo", flux["lo"], 0.06 * self.ref * 20)
        mid = self._onset("mid", flux["mid"], 0.10 * self.ref * 20)
        hi = self._onset("hi", flux["hi"], 0.04 * self.ref * 20)
        if self.drum_cool > 0:
            self.drum_cool -= 1
        elif not silent and not self.drum_q:
            if lo and not hi:
                self.drum_q.extend([("n", 0x2400), ("t", hz_of(34)), ("t", hz_of(30)), ("t", hz_of(26)), ("t", hz_of(24))])
                self.drum_cool = 5
            elif hi and mid:
                self.drum_q.extend([("n", 0x6000), ("n", 0x5000), ("n", 0x4000), ("n", 0x3000)])
                self.drum_cool = 4
            elif hi:
                self.drum_q.extend([("n", 0xB000), ("n", 0xA000)])
                self.drum_cool = 3

        # voice 1: bass
        v1 = self.v1
        if mb is None:
            v1.gate = 0
        else:
            v1.freq = self._reg(hz_of(mb))
            v1.wave = WAVE_TRI if hz_of(mb) < 70 else WAVE_PULSE
            if self.tr_bass.changed and v1.gate:
                v1.retrigger()
            elif self.tr_bass.changed:
                v1.retrigger()
            v1.gate = 1
        # voice 2: lead
        v2 = self.v2
        if ml is None:
            v2.gate = 0
        else:
            v2.freq = self._reg(hz_of(ml))
            if self.tr_lead.changed:
                v2.retrigger()
            v2.gate = 1
        pw2 = int(0x0800 + 0x0500 * math.sin(2 * math.pi * 1.4 * self.k / self.fps))

        # voice 3: drums or harmony
        v3 = self.v3
        if self.drum_q:
            kind, val = self.drum_q.popleft()
            first = not self.v3_is_drum
            self.v3_is_drum = True
            v3.ad, v3.sr = 0x00, 0xF0
            if kind == "n":
                v3.wave, v3.freq = WAVE_NOISE, int(val)
            else:
                v3.wave, v3.freq = WAVE_TRI, self._reg(val)
            v3.gate = 1
            if first:
                v3.retrigger()
        else:
            if self.v3_is_drum:
                self.v3_is_drum = False
                v3.retrigger()
                v3.ad, v3.sr = 0x04, 0xB1
            if mh is None:
                v3.gate = 0
            else:
                f = hz_of(mh)
                v3.freq = self._reg(f)
                v3.wave = WAVE_TRI if f > 600 else WAVE_SAW
                if self.tr_harm.changed:
                    v3.retrigger()
                v3.gate = 1

        # master volume follows loudness (fast up, slow down)
        target = float(np.clip((self.last_rms_db + 44.0) / 38.0 * 11.0 + 4.0, 0.0, 15.0))
        if silent:
            target = 0.0
        if target > self.vol:
            self.vol = target
        else:
            self.vol = max(target, self.vol - 0.7)
        vol = int(round(self.vol * self.vol_scale))
        vol = max(0, min(15, vol))

        return v1.bytes() + v2.bytes(pw2) + v3.bytes() + bytes([vol])
