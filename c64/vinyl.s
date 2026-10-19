; SIDVinyl - Navidrome/Subsonic frontend for the C64 (Ultimate command interface)
; Assemble with:  ca65 vinyl.s -o vinyl.o && ld65 -C vinyl.cfg vinyl.o -o sidvinyl.prg
.include "defs.inc"
.include "boot.s"
.include "audio.s"
.include "uci.s"
.include "net.s"
.include "ui.s"
.include "acia.s"
