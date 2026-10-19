"""Synthetic-music check of the chiptune analyser and the digi quantiser."""
import math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
import numpy as np
from audio import ChiptuneEncoder, DigiEncoder, hz_of, SID_CLOCK_PAL

R = 16000

def synth(seconds=8.0):
    n = int(R * seconds)
    t = np.arange(n) / R
    x = np.zeros(n)
    bass_notes = [36, 43, 41, 38]            # C2 G2 F2 D2, 0.5 s each
    lead_notes = [69, 72, 76, 72, 74, 77, 81, 77]  # 0.25 s each
    truth_b = np.zeros(n); truth_l = np.zeros(n)
    for i in range(n):
        pass
    for i, tt in enumerate(np.arange(0, seconds, 0.5)):
        m = bass_notes[i % 4]; f = hz_of(m)
        s, e = int(tt * R), int((tt + 0.5) * R)
        seg = t[s:e]
        x[s:e] += 0.35 * np.sin(2 * np.pi * f * seg) + 0.15 * np.sin(2 * np.pi * 2 * f * seg)
        truth_b[s:e] = m
    for i, tt in enumerate(np.arange(0, seconds, 0.25)):
        m = lead_notes[i % 8]; f = hz_of(m)
        s, e = int(tt * R), int((tt + 0.25) * R)
        seg = t[s:e]
        for h, a in ((1, 0.22), (2, 0.11), (3, 0.07), (4, 0.04)):
            x[s:e] += a * np.sin(2 * np.pi * h * f * seg)
        truth_l[s:e] = m
    rng = np.random.default_rng(3)
    for tt in np.arange(0, seconds, 0.5):    # kicks
        s = int(tt * R); L = int(0.09 * R)
        tk = np.arange(L) / R
        x[s:s + L] += 0.5 * np.sin(2 * np.pi * (50 * tk + 700 * tk ** 2 * 0.5 * -1 + 120 * tk)) * np.exp(-tk * 35)
    for tt in np.arange(0.25, seconds, 0.5):  # hats
        s = int(tt * R); L = int(0.04 * R)
        nz = rng.normal(0, 1, L); nz = np.diff(np.concatenate(([0], nz)))   # crude highpass
        x[s:s + L] += 0.25 * nz * np.exp(-np.arange(L) / R * 90)
    x = np.clip(x, -0.98, 0.98)
    return (x * 32767).astype(np.int16), truth_b, truth_l

def decode(frame, clock=SID_CLOCK_PAL):
    out = []
    for v in range(3):
        o = v * 7
        reg = frame[o] | (frame[o + 1] << 8)
        ctrl = frame[o + 4]
        f = reg * clock / 16777216.0
        out.append((f, ctrl))
    return out, frame[21]

def main():
    pcm, tb, tl = synth()
    enc = ChiptuneEncoder(fps=50)
    frames = []
    for i in range(0, len(pcm), 1600):
        frames += enc.feed(pcm[i:i + 1600])
    print("frames", len(frames), "frame len", set(len(f) for f in frames))
    ok_b = ok_l = tot = 0
    drum_frames = 0
    lat = 4      # frames of analysis latency (window ends at hop end)
    for k, fr in enumerate(frames):
        centre = int((k + 0.5) * R / 50) - int(0.032 * R)   # window centre approx
        if centre < 6000 or centre >= len(pcm) - 3000:
            continue
        (f1, c1), (f2, c2), (f3, c3), = decode(fr)[0]
        tot += 1
        mb, ml = tb[centre], tl[centre]
        if c1 & 1 and f1 > 0 and abs(12 * math.log2(f1 / hz_of(mb))) < 0.8: ok_b += 1
        if c2 & 1 and f2 > 0 and abs(12 * math.log2(f2 / hz_of(ml))) < 0.8: ok_l += 1
        if c3 & 0x80: drum_frames += 1
    print(f"bass ok {ok_b}/{tot} = {ok_b/tot:.0%}   lead ok {ok_l}/{tot} = {ok_l/tot:.0%}   noise(drum) frames {drum_frames}")
    vols = [f[21] for f in frames]
    print("master volume range", min(vols), max(vols))

    # digi
    d = DigiEncoder()
    sig = (np.sin(2 * np.pi * 440 * np.arange(8000) / 8000) * 12000).astype(np.int16)
    packed = d.encode(sig)
    nib = np.empty(len(packed) * 2, dtype=np.uint8); nib[0::2] = np.frombuffer(packed, np.uint8) >> 4; nib[1::2] = np.frombuffer(packed, np.uint8) & 15
    rec = (nib.astype(float) - 7.5) / 7.5
    ref = sig / 32768.0
    sp = np.abs(np.fft.rfft(rec * np.hanning(len(rec))))
    peak = int(np.argmax(sp))
    sig_pow = sp[peak - 2: peak + 3].sum(); tot_pow = sp[:2000].sum()
    print("digi bytes", len(packed), "peak bin", peak, "(440 expected)  signal share %.2f" % (sig_pow / tot_pow),
          "nibble range", nib.min(), nib.max())

if __name__ == "__main__":
    main()
