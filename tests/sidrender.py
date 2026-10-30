"""Minimal SID voice renderer (no filter): turns 22-byte frames back into audio for objective checks."""
import numpy as np

# attack / decay-release times in seconds for the 16 register values
_AT = np.array([2, 8, 16, 24, 38, 56, 68, 80, 100, 250, 500, 800, 1000, 3000, 5000, 8000]) / 1000.0


def render(frames, fps=50, clock=985248, rate=32000):
    n = len(frames)
    per = int(round(rate / fps))
    out = np.zeros(n * per)
    rng = np.random.default_rng(1)
    for v in range(3):
        phase, env, lfsr = 0.0, 0.0, 1
        state = "rel"
        noise_val = 0.0
        acc = 0.0
        sig = np.zeros(n * per)
        for k, fr in enumerate(frames):
            o = v * 7
            freq = (fr[o] | (fr[o + 1] << 8)) * clock / 16777216.0
            pw = (fr[o + 2] | ((fr[o + 3] & 15) << 8)) / 4096.0
            ctrl, ad, sr = fr[o + 4], fr[o + 5], fr[o + 6]
            gate = ctrl & 1
            if gate and state in ("rel", "idle"):
                state = "att"
            elif not gate:
                state = "rel"
            sus = (sr >> 4) / 15.0
            t = (np.arange(per) / rate)
            ph = (phase + freq * t) % 1.0
            phase = (phase + freq * per / rate) % 1.0
            if ctrl & 0x80:
                steps = int(freq * per / rate) + 1
                nv = rng.uniform(-1, 1, steps)
                idx = np.minimum((np.arange(per) * steps // per), steps - 1)
                w = nv[idx]
            elif ctrl & 0x40:
                w = np.where(ph < pw, 1.0, -1.0)
            elif ctrl & 0x20:
                w = 2 * ph - 1
            elif ctrl & 0x10:
                w = 4 * np.abs(ph - 0.5) - 1
            else:
                w = np.zeros(per)
            e = np.empty(per)
            for i in range(per):
                if state == "att":
                    env += 1.0 / max(1, _AT[ad >> 4] * rate)
                    if env >= 1.0:
                        env, state = 1.0, "dec"
                elif state in ("dec", "sus"):
                    state = "dec"
                    if env > sus:
                        env = max(sus, env - 1.0 / max(1, 3 * _AT[ad & 15] * rate))
                elif state == "rel":
                    env = max(0.0, env - 1.0 / max(1, 3 * _AT[sr & 15] * rate))
                e[i] = env
            sig[k * per:(k + 1) * per] = w * e
        out += sig
    return out / 3.0, rate
