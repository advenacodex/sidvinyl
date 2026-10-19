#!/usr/bin/env python3
"""Patch the default bridge address ("host:port") inside sidvinyl.prg.

    python3 patch_host.py sidvinyl.prg 192.168.1.50:6464 [-o out.prg]
"""
import argparse, sys

MAGIC = b"SIDHOST="  # the default string follows, 40 bytes, zero padded (ASCII)

def patch(data: bytes, addr: str) -> bytes:
    if len(addr) > 38 or not addr.isascii():
        raise SystemExit("address too long (max 38 ASCII chars)")
    i = data.find(MAGIC)
    if i < 0:
        raise SystemExit("SIDHOST= marker not found: not a SIDVinyl PRG?")
    j = i + len(MAGIC)
    return data[:j] + addr.encode().ljust(40, b"\0") + data[j + 40:]

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("prg"); ap.add_argument("addr"); ap.add_argument("-o", "--out")
    a = ap.parse_args()
    out = patch(open(a.prg, "rb").read(), a.addr)
    open(a.out or a.prg, "wb").write(out)
    print(f"{a.out or a.prg}: default bridge = {a.addr}")

if __name__ == "__main__":
    main()
