#!/bin/sh
# Build sidvinyl.prg with cc65 (ca65/ld65 must be on PATH or set CC65_BIN)
set -e
cd "$(dirname "$0")"
B=${CC65_BIN:-}
${B:+$B/}ca65 -g -l vinyl.lst vinyl.s -o vinyl.o
${B:+$B/}ld65 -C vinyl.cfg vinyl.o -o sidvinyl.prg -m vinyl.map -Ln vinyl.lbl
ls -l sidvinyl.prg
