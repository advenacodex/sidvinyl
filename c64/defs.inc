; ---------------------------------------------------------------------------
; SIDVinyl - C64 client. Hardware, memory map and variables.
; ---------------------------------------------------------------------------
.setcpu "6502"

; ---- VIC-II
VIC_SPRX0   = $D000
VIC_MSBX    = $D010
VIC_CTL1    = $D011
VIC_RASTER  = $D012
VIC_SPREN   = $D015
VIC_CTL2    = $D016
VIC_SPRYEXP = $D017
VIC_SPRXEXP = $D01D
VIC_MEM     = $D018
VIC_IRQ     = $D019
VIC_IRQEN   = $D01A
VIC_SPRMC   = $D01C
VIC_BORDER  = $D020
VIC_BG0     = $D021
VIC_SPRCOL0 = $D027

; ---- SID
SID         = $D400
SID_VOL     = $D418

; ---- CIA
CIA1_PRA    = $DC00
CIA1_PRB    = $DC01
CIA1_DDRA   = $DC02
CIA1_DDRB   = $DC03
CIA1_ICR    = $DC0D
CIA2_PRA    = $DD00
CIA2_DDRA   = $DD02
CIA2_TALO   = $DD04
CIA2_TAHI   = $DD05
CIA2_ICR    = $DD0D
CIA2_CRA    = $DD0E

; ---- Ultimate Command Interface
UCI_CTRL    = $DF1C          ; write: control   read: status
UCI_STAT    = $DF1C
UCI_CMD     = $DF1D          ; write: command bytes
UCI_ID      = $DF1D          ; read: $C9 when present
UCI_DATA    = $DF1E
UCI_STDAT   = $DF1F

; ---- memory map
BITMAP      = $2000
SCREEN      = $0400
COLORRAM    = $D800
SPR_DATA    = $0340          ; 3 sprites, blocks 13..15
DG_BASE     = $4000          ; digi ring: 32 pages
DG_END_HI   = $60
SID_BASE    = $6000          ; sid ring: 100 frames of 22 bytes
SID_NFR     = 100
SID_END     = SID_BASE + SID_NFR * 22
DISC_BUF    = $6900          ; 2560 disc + 192 sprites
FRAMES      = $8000          ; 32 label frames * 640 bytes
NFRAMES     = 32

RXBUF       = $E000          ; UCI response staging (1024)
CMDBUF      = $E400          ; UCI command staging (128)
HOSTBUF     = $E480          ; "host:port" ASCII, zero terminated (64)
LISTBUF     = $E500          ; PKT_LIST payload (768)
TRACKBUF    = $E800          ; PKT_TRACK payload (256)
LINEBUF     = $E900          ; 3 text lines x 64 (title, artist, album)
ERRBUF      = $E9C0          ; PKT_ERROR text (38 + pad)
SMALLBUF    = $E9F0          ; tiny payloads
FONT        = $EA00          ; copy of char ROM set 2 (1024)
TIMEBUF     = $EE00          ; formatted strings
RXRING      = $F000          ; serial receive ring: 2 KB ($F000-$F7FF), written by the timer-NMI handler
RXRING_HI   = >RXRING
RXRING_END  = $F8            ; first page after the ring
RX_WINDOW   = 224            ; flow-control window in 8-byte units (1792 bytes)

; ---- ACIA 6551 (SwiftLink layout at $DE00)
ACIA_DATA   = $DE00
ACIA_STATUS = $DE01
ACIA_CMD    = $DE02
ACIA_CTRL   = $DE03

NO_ROOM_MIN = 64

; ---- packet / command ids (see bridge/protocol.py)
PKT_HELLO   = $01
PKT_LIST    = $02
PKT_TRACK   = $03
PKT_COVER   = $04
PKT_DIGI    = $05
PKT_SID     = $06
PKT_STOP    = $07
PKT_ERROR   = $08
PKT_VOL     = $09
PKT_DISC    = $0A

CMD_HELLO   = $01
CMD_LIST    = $02
CMD_PLAY    = $03
CMD_NEXT    = $04
CMD_PREV    = $05
CMD_MODE    = $07
CMD_VOL     = $08
CMD_STOP    = $09
CMD_CREDIT  = $0A

; ---- app states
ST_SETUP    = 0
ST_MENU     = 1
ST_NOWPLAY  = 2

; ---- key codes (PETSCII)
K_DOWN      = $11
K_UP        = $91
K_RIGHT     = $1D
K_LEFT      = $9D
K_RETURN    = $0D
K_DEL       = $14
K_STOP      = $03
K_SPACE     = $20

.segment "ZEROPAGE"
src:        .res 2
dst:        .res 2
tmp:        .res 2
tmp2:       .res 2
cnt:        .res 2
psrc:       .res 2           ; parser source
pcnt:       .res 2           ; parser bytes left in chunk
pn:         .res 2           ; parser bytes in this step
ps_dst:     .res 2           ; parser linear destination
ps_len:     .res 2           ; payload bytes left
fptr:       .res 2
dptr:       .res 2
dptr2:      .res 2
lbp:        .res 2
dg_wr:      .res 2
sid_rd:     .res 2
sid_wr:     .res 2
dg_phase:   .res 1
dg_byte:    .res 1
sid_count:  .res 1
sid_part:   .res 1
pub_page:   .res 1
tick:       .res 1
kcnt:       .res 1

.segment "BSS"
video:      .res 1           ; 0 PAL, 1 NTSC
fps:        .res 1
app_state:  .res 1
mode:       .res 1           ; 0 digi, 1 sid
paused:     .res 1
playing:    .res 1           ; audio engine running
prebuf:     .res 1           ; waiting for pre-roll
skip_audio: .res 1
nav_seq:    .res 1
vol_level:  .res 1
sock:       .res 1
sort:       .res 1
page:       .res 1
sel:        .res 1
sel_after:  .res 1
list_count: .res 1
list_more:  .res 1
ev_hello:   .res 1
ev_list:    .res 1
ev_sec:     .res 1
disc_ready: .res 1
cover_ready:.res 1
err_pending:.res 1
stop_wait:  .res 1
loading:    .res 1
ps_state:   .res 1
ps_type:    .res 1
rx_count:   .res 2
uci_len:    .res 1
status0:    .res 1
status1:    .res 1
trk_no:     .res 1
trk_total:  .res 1
dur_lo:     .res 1
dur_hi:     .res 1
sec_lo:     .res 1
sec_hi:     .res 1
frame_div:  .res 1
rot_div:    .res 1
cur_frame:  .res 1
marq_div:   .res 1
line_off:   .res 3
key_last:   .res 1
key_timer:  .res 1
shiftf:     .res 1
found:      .res 1
host_len:   .res 1
port_lo:    .res 1
port_hi:    .res 1
g_row:      .res 1
g_col:      .res 1
dl_idx:     .res 1
dl_i:       .res 1
dl_len:     .res 1
dl_per:     .res 1
bar_n:      .res 1
dg_under:   .res 1
transport:  .res 1           ; 0 = Ultimate UCI, 1 = serial (SwiftLink/ACIA)
dsel:       .res 1           ; digi rate selector from the bridge
rx_rd:      .res 1           ; serial ring read pointer, low byte
rx_rdh:     .res 1           ; ... and page ($F0-$F7)
rx_lim:     .res 1           ; credit limit in 8-byte units (mod 256)
acia_ovr:   .res 1           ; diagnostics: ACIA overrun events seen by the NMI poller
