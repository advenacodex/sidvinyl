"""Buffered chiptune encoder: PCM (16 kHz mono) -> 22-byte SID register frames.

Unlike a frame-by-frame tracker this one keeps a look-ahead buffer (LOOKAHEAD frames)
and decides each block with the future in view:

  * bass / lead / harmony pitch: harmonic-sum salience on a 1/3-semitone grid, then a
    Viterbi path over time (stable notes, vibrato kept, octave jumps penalised);
  * the three tracks are found one after another, each masking the harmonics of the
    previous ones, so they do not double up;
  * pitch tracks are cut into notes; a re-attack (amplitude jump) retriggers the gate,
    legato pitch changes do not;
  * dynamics: the voice envelope runs with attack = decay = 0 and the sustain nibble is
    rewritten every frame with the (non-increasing) loudness of the note, so the SID
    follows the real amplitude contour instead of a fixed ADSR;
  * waveform per note from the measured harmonic profile (sine-like -> triangle,
    odd harmonics -> pulse, bright -> sawtooth);
  * drums: onset detection with local adaptive thresholds in three bands, played on
    voice 3 as noise / triangle sweeps shaped by per-frame sustain levels.

Frame layout (22 bytes): voice1 (7) voice2 (7) voice3 (7) volume, each voice being
freq lo/hi, pw lo/hi, control, AD, SR.  Voice 1 = bass, 2 = lead, 3 = harmony / drums.
"""
import math

import numpy as np

RATE = 16000
SID_CLOCK_PAL = 985248
SID_CLOCK_NTSC = 1022727

WAVE_TRI, WAVE_SAW, WAVE_PULSE, WAVE_NOISE = 0x10, 0x20, 0x40, 0x80

STEPS = 3                 # pitch grid resolution: steps per semitone
LOOKAHEAD = 75            # frames of future context per block (1.5 s at 50 fps)
BLOCK_FIRST = 50          # first block is short so playback starts quickly
BLOCK = 400
HALF = 2048               # samples of context needed either side of a frame centre
WIN_M, NFFT_M = 1536, 4096      # melodic analysis
WIN_B, NFFT_B = 4096, 8192      # bass analysis
BASS_BINS = 720
WIN_S = 512                     # transient analysis


def midi_of(f):
    return 69.0 + 12.0 * math.log2(f / 440.0)


def hz_of(m):
    return 440.0 * 2.0 ** ((np.asarray(m, dtype=np.float64) - 69.0) / 12.0)


# ------------------------------------------------------------------ pitch grid
class _Grid:
    """Harmonic comb matrix mapping spectral peaks to salience of candidate pitches."""

    def __init__(self, lo_midi, hi_midi, nfft, nharm, nbins, vib_steps=3):
        self.midi = np.arange(lo_midi, hi_midi + 1e-9, 1.0 / STEPS)
        self.hz = hz_of(self.midi)
        self.n = len(self.midi)
        self.bin_hz = RATE / nfft
        c = np.zeros((self.n, nbins), np.float32)
        for h in range(1, nharm + 1):
            b = h * self.hz / self.bin_hz
            rows = np.nonzero(b < nbins - 2)[0]
            i = np.floor(b[rows]).astype(int)
            fr = (b[rows] - i).astype(np.float32)
            w = 1.0 / math.sqrt(h)
            c[rows, i] += w * (1.0 - fr)
            c[rows, i + 1] += w * fr
        self.C = np.ascontiguousarray(c.T)               # (nbins, n)
        # Viterbi transition scores, state 0 = unvoiced
        d = np.abs(np.arange(self.n)[:, None] - np.arange(self.n)[None, :]).astype(np.float32)
        cost = np.where(d <= vib_steps, 0.12 * d, 0.5 + 0.1 * np.minimum(d, 12))
        cost[d >= 8] = 1.6
        t = np.zeros((self.n + 1, self.n + 1), np.float32)
        t[1:, 1:] = -cost
        t[0, 1:] = t[1:, 0] = -1.6
        self.T = t


def _viterbi(emis, trans, s0):
    n, s = emis.shape
    ar = np.arange(s)
    delta = trans[s0] + emis[0]
    psi = np.zeros((n, s), np.int32)
    for f in range(1, n):
        cand = delta[:, None] + trans
        p = cand.argmax(0)
        psi[f] = p
        delta = cand[p, ar] + emis[f]
    path = np.empty(n, np.int32)
    path[-1] = int(delta.argmax())
    for f in range(n - 1, 0, -1):
        path[f - 1] = psi[f, path[f]]
    return path


def _peaks(mag, width):
    """sqrt-compress and subtract the local mean: keeps spectral peaks only."""
    x = np.sqrt(mag)
    n = x.shape[1]
    c = np.concatenate((np.zeros((x.shape[0], 1), x.dtype), np.cumsum(x, axis=1)), axis=1)
    hi = np.minimum(np.arange(n) + width // 2 + 1, n)
    lo = np.maximum(np.arange(n) - width // 2, 0)
    mean = (c[:, hi] - c[:, lo]) / (hi - lo)
    return np.maximum(x - mean, 0.0)


def _amps(mag, f_hz, bin_hz, nh):
    """Peak magnitude near each of the first nh harmonics of f_hz (0 where f_hz is nan)."""
    n, nb = mag.shape
    ar = np.arange(n)
    ok = np.isfinite(f_hz)
    f = np.where(ok, f_hz, 0.0)
    out = np.zeros((n, nh), np.float32)
    for h in range(nh):
        b = np.clip(np.rint(f * (h + 1) / bin_hz).astype(int), 1, nb - 3)
        m = np.maximum(np.maximum(mag[ar, b - 1], mag[ar, b]), mag[ar, b + 1])
        out[:, h] = np.where(ok & (f * (h + 1) / bin_hz < nb - 3), m, 0.0)
    return out


def _mask(peaks, f_hz, bin_hz, hs, w):
    n, nb = peaks.shape
    ok = np.isfinite(f_hz)
    f = np.where(ok, f_hz, 0.0)
    for h in hs:
        b = np.rint(f * h / bin_hz).astype(int)
        rows = np.nonzero(ok & (b + w < nb) & (b - w >= 0))[0]
        for off in range(-w, w + 1):
            peaks[rows, b[rows] + off] = 0.0


def _median3(x):
    if len(x) < 3:
        return x.copy()
    p = np.concatenate(([x[0]], x, [x[-1]]))
    return np.median(np.stack((p[:-2], p[1:-1], p[2:])), axis=0)


def _median5(x):
    if len(x) < 5:
        return x.copy()
    p = np.concatenate(([x[0], x[0]], x, [x[-1], x[-1]]))
    return np.median(np.stack([p[i:i + len(x)] for i in range(5)]), axis=0)


class _Note:
    __slots__ = ("s", "e", "retrig", "cont", "wave", "pw")

    def __init__(self, s, e, retrig, cont):
        self.s, self.e, self.retrig, self.cont = s, e, retrig, cont
        self.wave, self.pw = WAVE_PULSE, 0x0800


# drum shapes at 50 fps: (kind, value, level fraction); 'n' = noise freq reg, 't' = triangle Hz
DRUMS = {
    "kick": [("n", 0x2800, 1.0), ("t", 120, 1.0), ("t", 78, 0.9), ("t", 55, 0.8), ("t", 42, 0.6), ("t", 34, 0.4)],
    "snare": [("n", 0x7800, 1.0), ("n", 0x6000, 0.9), ("n", 0x5000, 0.7), ("n", 0x4000, 0.5), ("n", 0x3400, 0.3)],
    "hat": [("n", 0xC800, 0.8), ("n", 0xB800, 0.4)],
}


class ChiptuneEncoder:
    """feed() PCM, get finished frames; flush() at end of stream."""
    RATE = RATE

    def __init__(self, fps=50, clock=SID_CLOCK_PAL):
        self.fps = float(fps)
        self.clock = clock
        self.sc = self.fps / 50.0
        self.vol_scale = 1.0           # kept for API compatibility; the bridge scales the volume byte
        self.k = 0                     # next frame to emit
        self.buf = np.zeros(2 * HALF, np.float32)
        self.base = -2 * HALF          # absolute sample index of buf[0]
        self.win_m = np.hanning(WIN_M).astype(np.float32)
        self.win_b = np.hanning(WIN_B).astype(np.float32)
        self.win_s = np.hanning(WIN_S).astype(np.float32)
        self.g_bass = _Grid(26, 55, NFFT_B, 6, BASS_BINS)
        self.g_mel = _Grid(48, 93, NFFT_M, 8, NFFT_M // 2 + 1)
        self.state = {"b": 0, "l": 0, "h": 0}
        self.carry = {n: {"gate": False, "midi": None, "held": 15, "last": (WAVE_PULSE, 0, 0x0800, 0, 0x03)}
                      for n in ("b", "l", "h")}
        self.mx_tail = np.zeros(0)
        self.flux_tail = np.zeros((0, 3))
        self.drum_pending = []          # (wave, freq, nibble) frames still to play after the last block
        self.last_event = -100
        self.last_rms_db = -90.0

    # -- helpers --------------------------------------------------------------
    def fr(self, x):
        return max(1, int(round(x * self.sc)))

    def _reg(self, hz):
        return int(max(0, min(65535, round(hz * 16777216.0 / self.clock))))

    def _centre(self, k):
        return int(round((k + 0.5) * RATE / self.fps))

    # -- streaming interface -----------------------------------------------------
    def feed(self, pcm_int16):
        self.buf = np.concatenate((self.buf, pcm_int16.astype(np.float32) / 32768.0))
        out = []
        while True:
            n = BLOCK_FIRST if self.k == 0 else BLOCK
            nt = n + self.fr(LOOKAHEAD)
            if self.base + len(self.buf) < self._centre(self.k + nt - 1) + HALF:
                break
            out += self._process(n, nt)
        return out

    def flush(self):
        total = self.base + len(self.buf)                       # real samples (index 0 = first sample)
        out = []
        self.buf = np.concatenate((self.buf, np.zeros(2 * HALF + 4096, np.float32)))
        while True:
            left = int(total * self.fps / RATE) - self.k
            if left <= 0:
                break
            if left > BLOCK + self.fr(LOOKAHEAD):
                out += self._process(BLOCK, BLOCK + self.fr(LOOKAHEAD))
            else:
                out += self._process(left, left)
        return out

    def encode_all(self, pcm_int16):
        return self.feed(pcm_int16) + self.flush()

    # -- block ---------------------------------------------------------------------
    def _stft(self, ks, win, nfft, nbins=None):
        n = len(win)
        starts = np.array([self._centre(k) for k in ks]) - self.base - n // 2
        seg = np.lib.stride_tricks.sliding_window_view(self.buf, n)[starts]
        x = np.abs(np.fft.rfft(seg * win, nfft, axis=1)) / (win.sum() / 2.0)
        if nbins:
            x = x[:, :nbins]
        return x.astype(np.float32), seg

    def _track(self, mag, grid, width, thr, name, silent):
        pk = _peaks(mag, width)
        return pk, self._viterbi_pk(pk, grid, thr, name, silent)

    def _viterbi_pk(self, pk, grid, thr, name, silent):
        s = pk @ grid.C
        # peakiness: salience against the frame's mean salience (noise/drums sit near 2.5, tones 7-20)
        r = np.maximum(s, 1e-6) / (s.mean(1, keepdims=True) + 1e-6)
        emis = np.zeros((len(pk), grid.n + 1), np.float32)
        emis[:, 1:] = np.clip(2.0 * np.log2(r / thr), -3.0, 5.0)
        emis[:, 1:][s < 0.03] = -3.0
        emis[silent, 1:] = -6.0
        path = _viterbi(emis, grid.T, self.state[name])
        v = path > 0
        i = np.clip(path - 1, 1, grid.n - 2)
        ar = np.arange(len(pk))
        a, b, c = s[ar, i - 1], s[ar, i], s[ar, i + 1]
        den = a - 2 * b + c
        d = np.where(den < 0, np.clip(0.5 * (a - c) / np.where(den < 0, den, -1.0), -0.5, 0.5), 0.0)
        midi = np.where(v, grid.midi[i] + d / STEPS, np.nan)
        return {"path": path, "midi": midi, "voiced": v}

    def _process(self, nc, nt):
        k0, fps = self.k, self.fps
        ks = np.arange(k0, k0 + nt)
        xm, seg = self._stft(ks, self.win_m, NFFT_M)
        xb, _ = self._stft(ks, self.win_b, NFFT_B, BASS_BINS)
        rms = np.sqrt(np.mean(seg.astype(np.float64) ** 2, axis=1)) + 1e-9
        db = 20 * np.log10(rms)
        self.last_rms_db = float(db[0])
        silent = db < -58.0

        # --- pitch tracks: bass, then lead, then harmony, each masking the previous ones
        pkb, tb = self._track(xb, self.g_bass, 41, 4.5, "b", silent)
        fb = hz_of(tb["midi"])
        pkm = _peaks(xm, 41)
        _mask(pkm, fb, self.g_mel.bin_hz, range(1, 7), 4)
        tl = self._viterbi_pk(pkm, self.g_mel, 4.5, "l", silent)
        fl = hz_of(tl["midi"])
        _mask(pkm, fl, self.g_mel.bin_hz, range(1, 11), 4)
        th = self._viterbi_pk(pkm, self.g_mel, 5.5, "h", silent)
        fh = hz_of(th["midi"])

        # --- amplitudes and loudness reference
        ab = _amps(xb, fb, self.g_bass.bin_hz, 4)
        al = _amps(xm, fl, self.g_mel.bin_hz, 4)
        ah = _amps(xm, fh, self.g_mel.bin_hz, 4)
        amp = {n: np.sqrt((a ** 2).sum(1)) for n, a in (("b", ab), ("l", al), ("h", ah))}
        mx = np.maximum.reduce(list(amp.values()))
        hist = np.concatenate((self.mx_tail, mx))
        w = 2 * self.fr(50) + 1
        sw = np.lib.stride_tricks.sliding_window_view(np.pad(hist, (w // 2, w // 2), mode="edge"), w).max(1)
        ref = np.maximum(sw[len(self.mx_tail):], 0.02)
        end = len(self.mx_tail) + nc
        self.mx_tail = hist[max(0, end - 100):end]

        voices = {}
        for name, tr, f, a, ah_ in (("b", tb, fb, amp["b"], ab), ("l", tl, fl, amp["l"], al), ("h", th, fh, amp["h"], ah)):
            lvl = _median3(np.clip(a / ref, 0.0, 1.0) ** 0.7)
            v = tr["voiced"] & ~silent & (lvl > 0.05)
            voices[name] = {"midi": np.where(v, tr["midi"], np.nan), "v": v, "lvl": lvl, "h": ah_}

        plans = {}
        for name in ("b", "l", "h"):
            plans[name] = self._plan(name, voices[name], nc, k0)

        self._drums(plans["h"], nc, nt, k0, silent)
        for name in ("b", "l", "h"):
            self._finish(plans[name], name, nc)

        frames = []
        for f in range(nc):
            out = bytearray()
            for name in ("b", "l", "h"):
                p = plans[name]
                out += bytes([p["freq"][f] & 255, p["freq"][f] >> 8, p["pw"][f] & 255, (p["pw"][f] >> 8) & 15,
                              (p["wave"][f] | p["gate"][f]) & 255, p["ad"][f], p["sr"][f]])
            out.append(15)
            frames.append(bytes(out))
        for name in ("b", "l", "h"):
            p = plans[name]
            c = self.carry[name]
            c["gate"] = bool(p["gate"][nc - 1])
            c["last"] = (p["wave"][nc - 1], p["freq"][nc - 1], p["pw"][nc - 1], p["ad"][nc - 1], p["sr"][nc - 1])
            c["held"] = p["sr"][nc - 1] >> 4
            c["midi"] = p["midi_last"]
        for name, tr in (("b", tb), ("l", tl), ("h", th)):
            self.state[name] = int(tr["path"][nc - 1])
        # trim consumed audio
        self.k = k0 + nc
        drop = self._centre(self.k) - HALF - 64 - self.base
        if drop > 0:
            self.buf = self.buf[drop:]
            self.base += drop
        return frames

    # -- notes -------------------------------------------------------------------------
    def _segment(self, v, m, lvl, name):
        n = len(v)
        v = v.copy()
        m = m.copy()
        car = self.carry[name]
        # bridge short dropouts, drop blips
        f = 0
        while f < n:
            if v[f]:
                f += 1
                continue
            g = f
            while g < n and not v[g]:
                g += 1
            if 0 < f and g < n and g - f <= self.fr(2) and abs(m[f - 1] - m[g]) < 1.0:
                v[f:g] = True
                m[f:g] = np.linspace(m[f - 1], m[g], g - f + 2)[1:-1]
            f = g
        f = 0
        minrun = self.fr(3)
        while f < n:
            if not v[f]:
                f += 1
                continue
            g = f
            while g < n and v[g]:
                g += 1
            if g - f < minrun and g < n and not (f == 0 and car["gate"]):
                v[f:g] = False
            f = g
        notes = []
        f = 0
        while f < n:
            if not v[f]:
                f += 1
                continue
            e = f
            while e < n and v[e]:
                e += 1
            s = f
            cur = _Note(s, e, False, False)
            if s == 0 and car["gate"] and car["midi"] is not None:
                if abs(m[0] - car["midi"]) < 0.8:
                    cur.cont = True
                else:
                    cur.retrig = True
            cen = float(np.median(m[s:s + 3]))
            hmin = lvl[s:s + 3].max()
            k = s + 1
            while k < e:
                split = None
                if abs(m[k] - cen) > 0.8 and (k + 1 >= e or abs(m[k + 1] - cen) > 0.8) and k - s >= self.fr(3):
                    rise = lvl[k] > lvl[max(s, k - 3):k].min() * 1.15 + 0.03
                    split = rise
                    cen = float(np.median(m[k:k + 3]))
                else:
                    hmin = min(hmin, lvl[k])
                    if k - s >= self.fr(4) and lvl[k] > hmin * 1.45 + 0.07 and lvl[k] > lvl[k - 1]:
                        split = True
                if split is not None:
                    cur.e = k
                    notes.append(cur)
                    cur = _Note(k, e, split, not split)
                    s = k
                    hmin = lvl[k:k + 3].max()
                else:
                    cen = float(np.median(m[max(s, k - 8):k + 1]))
                k += 1
            notes.append(cur)
            f = e
        return notes, v, m

    def _pick_wave(self, name, note, h):
        r = h[note.s:note.e]
        a1 = np.maximum(r[:, 0], 1e-6)
        r2 = float(np.median(r[:, 1] / a1))
        r3 = float(np.median(r[:, 2] / a1))
        if name == "b":
            note.wave = WAVE_TRI if (r2 < 0.35) else (WAVE_SAW if r2 > 0.9 else WAVE_PULSE)
            note.pw = 0x0A00
            return
        if r2 < 0.2 and r3 < 0.2:
            note.wave, note.pw = WAVE_TRI, 0x0800
        elif r3 > r2 * 1.3 and r3 > 0.25:
            note.wave, note.pw = WAVE_PULSE, 0x0800
        elif r2 > 0.5:
            note.wave, note.pw = WAVE_SAW, 0x0800
        else:
            note.wave, note.pw = WAVE_PULSE, 0x0500

    def _plan(self, name, vc, nc, k0):
        car = self.carry[name]
        notes, v, m = self._segment(vc["v"], vc["midi"], vc["lvl"], name)
        m = np.where(v, m, np.nan)
        lvl = vc["lvl"]
        P = {key: [0] * nc for key in ("gate", "wave", "freq", "pw", "ad", "sr")}
        P["trig"] = [False] * nc
        P["act"] = [False] * nc
        P["drum"] = [False] * nc
        midi_last = car["midi"]
        held_prev = car["held"] if car["held"] else 15
        prev_end = -1
        for nt_ in notes:
            if nt_.s >= nc:
                break
            self._pick_wave(name, nt_, vc["h"])
            seg = m[nt_.s:nt_.e]
            ms = _median5(seg)
            base = float(np.median(ms))
            d = ms - base
            ms = base + np.sign(d) * np.maximum(np.abs(d) - 0.25, 0.0)
            first = max(lvl[nt_.s:nt_.s + 4])
            contiguous = nt_.s == prev_end
            if nt_.cont or (contiguous and not nt_.retrig):
                held = min(held_prev, first)
            else:
                held = first
            for j, f in enumerate(range(nt_.s, min(nt_.e, nc))):
                if j > 0:
                    held = min(held, lvl[f])
                nib = int(max(1, min(15, round(held * 15))))
                P["act"][f] = True
                P["gate"][f] = 1
                P["wave"][f] = nt_.wave
                P["freq"][f] = self._reg(float(hz_of(ms[j])))
                P["ad"][f] = 0x00
                P["sr"][f] = (nib << 4) | 0x00
                if nt_.wave == WAVE_PULSE:
                    t = (k0 + f) / self.fps
                    P["pw"][f] = int(nt_.pw + 0x0300 * math.sin(2 * math.pi * 1.3 * t + (0 if name == "l" else 2.0)))
                else:
                    P["pw"][f] = nt_.pw
                midi_last = float(ms[j])
            if nt_.retrig:
                P["trig"][nt_.s] = True
            held_prev = held
            prev_end = nt_.e
        P["midi_last"] = midi_last if P["gate"][nc - 1] else None
        return P

    # -- drums -------------------------------------------------------------------------
    def _drum_items(self, kind, level):
        seq = DRUMS[kind]
        n = int(math.ceil(len(seq) * self.sc))
        items, nib = [], 15
        for j in range(n):
            typ, val, frac = seq[min(len(seq) - 1, int(j / self.sc))]
            nib = min(nib, max(1, int(round(level * frac * 15))))      # sustain only ever falls
            items.append((WAVE_NOISE, int(val), nib) if typ == "n" else (WAVE_TRI, self._reg(val), nib))
        return items

    def _drums(self, P, nc, nt, k0, silent):
        """Detect hits (short-window spectral flux, adaptive thresholds) and write them into voice 3."""
        ks = np.arange(k0 - 1, k0 + nt)
        mag, _ = self._stft(ks, self.win_s, WIN_S, WIN_S // 2 + 1)
        lg = np.log1p(mag * 40.0)
        d = np.maximum(lg[1:] - lg[:-1], 0.0)
        bands = np.stack((d[:, 1:6].mean(1), d[:, 40:128].mean(1), d[:, 150:256].mean(1)), axis=1)
        hist = np.concatenate((self.flux_tail, bands))
        off = len(self.flux_tail)
        self.flux_tail = hist[max(0, off + nc - 60):off + nc]
        w = 2 * self.fr(25) + 1
        floors = (0.10, 0.10, 0.08)
        flags = np.zeros((len(hist), 3), bool)
        for b in range(3):
            o = hist[:, b]
            win = np.lib.stride_tricks.sliding_window_view(np.pad(o, (w // 2, w // 2), mode="edge"), w)
            thr = np.maximum(win.mean(1) + 2.0 * win.std(1), floors[b])
            pk = np.lib.stride_tricks.sliding_window_view(np.pad(o, (2, 2), mode="edge"), 5).max(1)
            flags[:, b] = (o > thr) & (o >= pk)
        z = np.clip((hist - hist.mean(0)) / (hist.std(0) + 1e-9), 0, 8).max(1)
        events = []
        for f in range(nc):
            if silent[f] or not flags[off + f].any():
                continue
            fl = flags[off + max(0, f - 1):off + f + 2].any(0)
            kind = "snare" if fl[1] else ("kick" if fl[0] else "hat")
            if (k0 + f) - self.last_event < self.fr(3 if kind == "hat" else 5):
                continue
            self.last_event = k0 + f
            events.append((f, kind, float(np.clip(0.55 + 0.08 * z[off + f], 0.55, 1.0))))
        plan = [None] * nc
        for j, it in enumerate(self.drum_pending[:nc]):
            plan[j] = it
        rest = self.drum_pending[nc:]
        for i, (f, kind, lv) in enumerate(events):
            stop = events[i + 1][0] if i + 1 < len(events) else None
            plan[f:] = [None] * (nc - f)
            rest = []
            for j, it in enumerate(self._drum_items(kind, lv)):
                g = f + j
                if stop is not None and g >= stop:
                    break
                if g < nc:
                    plan[g] = it
                else:
                    rest.append(it)
        self.drum_pending = rest
        for f, it in enumerate(plan):
            if it is None:
                continue
            P["act"][f] = P["drum"][f] = True
            P["gate"][f] = 1
            P["wave"][f], P["freq"][f], nib = it
            P["pw"][f], P["ad"][f], P["sr"][f] = 0x0800, 0x00, nib << 4
            if f == 0 or not P["drum"][f - 1]:
                P["trig"][f] = True
        for f in range(1, nc):                 # first melodic frame after a hit needs a fresh gate edge
            if P["act"][f] and not P["drum"][f] and P["drum"][f - 1]:
                P["trig"][f] = True

    def _finish(self, P, name, nc):
        """Hold registers through inactive frames and open a gate gap before every retrigger."""
        car = self.carry[name]
        for f in range(nc):
            if P["act"][f]:
                continue
            w_, fq, pw, ad, sr = car["last"] if f == 0 else (P["wave"][f - 1], P["freq"][f - 1], P["pw"][f - 1],
                                                              P["ad"][f - 1], P["sr"][f - 1])
            if f > 0 and P["gate"][f - 1]:
                sr = (sr & 0xF0) | 0x03                # note just ended: short release
            P["wave"][f], P["freq"][f], P["pw"][f], P["ad"][f], P["sr"][f] = w_, fq, pw, ad, sr
            P["gate"][f] = 0
        for f in range(nc):
            if not P["trig"][f]:
                continue
            if f > 0 and P["gate"][f - 1]:
                P["gate"][f - 1] = 0
                P["sr"][f - 1] &= 0xF0
            elif f == 0 and car["gate"]:
                P["gate"][0] = 0
                P["sr"][0] &= 0xF0
