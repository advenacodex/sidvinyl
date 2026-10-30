"""Audio decoding and the two C64 encoders.

DigiEncoder      PCM 8 kHz -> packed 4-bit samples (noise-shaped) for $D418 playback.
ChiptuneEncoder  PCM 16 kHz -> 50/60 Hz SID register frames (3 voices); buffered, look-ahead
                 analysis, see sidenc.py.
"""
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


from sidenc import ChiptuneEncoder, hz_of, midi_of  # noqa: E402,F401  (buffered SID encoder)
