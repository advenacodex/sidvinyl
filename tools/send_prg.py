#!/usr/bin/env python3
"""Send a PRG to an Ultimate 64 / Ultimate-II+ and run it (REST API, firmware 3.x).

    python3 send_prg.py sidvinyl.prg 192.168.1.60 [--password PW]

Needs "Web Remote Control Service" enabled in the Ultimate network settings.
NOTE: the endpoint (POST /v1/runners:run_prg) follows the Ultimate REST docs but
could not be tested here; if it fails, copy the PRG to a USB stick / FTP and run
it from the Ultimate file browser instead.
"""
import argparse, urllib.request

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("prg"); ap.add_argument("host"); ap.add_argument("--password")
    a = ap.parse_args()
    req = urllib.request.Request(f"http://{a.host}/v1/runners:run_prg", data=open(a.prg, "rb").read(),
                                 method="POST", headers={"Content-Type": "application/octet-stream"})
    if a.password: req.add_header("X-Password", a.password)
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.status, r.read().decode(errors="replace"))

if __name__ == "__main__":
    main()
