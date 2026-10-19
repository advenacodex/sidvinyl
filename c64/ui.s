; ---------------------------------------------------------------------------
; User interface: keyboard, setup, album menu, now-playing vinyl screen
; ---------------------------------------------------------------------------

; string literals below are assembled as C64 screen codes (upper/lower charset)
.repeat 26, I
        .charmap $61+I, 1+I
.endrepeat

.macro SETPTR zp, addr
        lda #<(addr)
        sta zp
        lda #>(addr)
        sta zp+1
.endmacro

.macro ADD16 zp, val
        clc
        lda zp
        adc #<(val)
        sta zp
        lda zp+1
        adc #>(val)
        sta zp+1
.endmacro

.macro PRINT row, col, label
        SETPTR dst, SCREEN + (row) * 40 + (col)
        SETPTR src, label
        jsr puts
.endmacro

.macro STR40 text
        .byte text
        .res 39 - .strlen(text), $20
        .byte 0
.endmacro

ROT_STEP_TICKS = 6
MARQ_TICKS     = 24

.segment "BSS"
scan_col:   .res 1
ds_y:       .res 1
show_lost:  .res 1

.segment "RODATA"
scr_lo:     .repeat 25, I
              .byte <(SCREEN + I * 40)
            .endrepeat
scr_hi:     .repeat 25, I
              .byte >(SCREEN + I * 40)
            .endrepeat
col_hi:     .repeat 25, I
              .byte >(COLORRAM + I * 40)
            .endrepeat
bm_lo:      .repeat 25, I
              .byte <(BITMAP + I * 320)
            .endrepeat
bm_hi:      .repeat 25, I
              .byte >(BITMAP + I * 320)
            .endrepeat

nibtab:     .byte $00,$03,$0C,$0F,$30,$33,$3C,$3F,$C0,$C3,$CC,$CF,$F0,$F3,$FC,$FF

lbuf_lo:    .byte <LINEBUF, <(LINEBUF+64), <(LINEBUF+128)
lbuf_hi:    .byte >LINEBUF, >(LINEBUF+64), >(LINEBUF+128)
line_row:   .byte 17, 18, 19

; PETSCII of each matrix position: index = column*8 + row (KERNAL layout)
keytab:
        .byte $14,$0D,$1D,$88,$85,$86,$87,$11
        .byte $33,$57,$41,$34,$5A,$53,$45,$00
        .byte $35,$52,$44,$36,$43,$46,$54,$58
        .byte $37,$59,$47,$38,$42,$48,$55,$56
        .byte $39,$49,$4A,$30,$4D,$4B,$4F,$4E
        .byte $2B,$50,$4C,$2D,$2E,$3A,$00,$2C
        .byte $5C,$2A,$3B,$13,$00,$3D,$5E,$2F
        .byte $31,$5F,$04,$32,$20,$02,$51,$03

s_title:    .byte "SIDVINYL - C64 ULTIMATE CLIENT", 0
s_prompt:   .byte "BRIDGE ADDRESS (HOST:PORT):", 0
s_hint:     .byte "TYPE, DEL, RETURN = CONNECT", 0
s_connecting: STR40 "CONNECTING..."
s_nouci:    STR40 "UCI NOT FOUND: ENABLE COMMAND INTERFACE"
s_connfail: STR40 "CONNECT FAILED. PRESS ANY KEY."
s_nohello:  STR40 "BRIDGE NOT ANSWERING. PRESS ANY KEY."
s_lost:     STR40 "CONNECTION LOST. RETURN TO RECONNECT."
s_link_uci: STR40 "F1 LINK: ULTIMATE UCI (TCP)"
s_link_ser: STR40 "F1 LINK: SERIAL SWIFTLINK $DE00"
s_head:     .byte "SIDVINYL", 0
s_sortlbl:  .byte "SORT:", 0
s_pglbl:    .byte "PAGE", 0
s_help:     .byte "CRSR:MOVE RETURN:PLAY S:SORT LR:PAGE", 0
s_loading:  STR40 "LOADING..."
s_blank40:  STR40 ""
s_noalb:    .byte "NO ALBUMS", 0

s_sort0:    .byte "A-Z ALBUM  ", 0
s_sort1:    .byte "A-Z ARTIST ", 0
s_sort2:    .byte "NEWEST     ", 0
s_sort3:    .byte "MOST PLAYED", 0
s_sort4:    .byte "RANDOM     ", 0
s_sort5:    .byte "STARRED    ", 0
sort_lo:    .byte <s_sort0, <s_sort1, <s_sort2, <s_sort3, <s_sort4, <s_sort5
sort_hi:    .byte >s_sort0, >s_sort1, >s_sort2, >s_sort3, >s_sort4, >s_sort5

s_h1:       .byte "N/P:TRACK SPC:PAUSE", 0
s_h2:       .byte "M:MODE +-:VOL Q:LIST", 0
s_trk:      .byte "TRK ", 0
s_vol:      .byte "VOL ", 0
s_digi:     .byte "DIGI    ", 0
s_sid:      .byte "SID     ", 0
s_paused:   .byte "PAUSED  ", 0
s_load:     .byte "LOADING ", 0

.segment "CODE"

; ===========================================================================
; text-mode helpers
; ===========================================================================
set_text_mode:
        lda #$0B                ; display off, text mode
        sta VIC_CTL1
        lda #$08
        sta VIC_CTL2
        lda #$17                ; screen $0400, charset ROM $1800 (lower case)
        sta VIC_MEM
        lda #0
        sta VIC_SPREN
        sta VIC_BORDER
        sta VIC_BG0
        rts

screen_on_text:
        lda #$1B
        sta VIC_CTL1
        rts

clear_text:
        lda #$20
        ldx #0
@l:     sta SCREEN,x
        sta SCREEN+$100,x
        sta SCREEN+$200,x
        sta SCREEN+$2E8,x
        inx
        bne @l
        lda #15
        ldx #0
@c:     sta COLORRAM,x
        sta COLORRAM+$100,x
        sta COLORRAM+$200,x
        sta COLORRAM+$2E8,x
        inx
        bne @c
        rts

puts:
        ldy #0
@l:     lda (src),y
        beq @d
        sta (dst),y
        iny
        bne @l
@d:     rts

row_color:                      ; X = row, A = colour
        pha
        lda scr_lo,x
        sta dst
        lda col_hi,x
        sta dst+1
        pla
        ldy #39
@l:     sta (dst),y
        dey
        bpl @l
        rts

bin2dec2:                       ; A (0..99) -> X = tens char, A = ones char
        ldx #0
@l:     cmp #10
        bcc @d
        sbc #10
        inx
        jmp @l
@d:     ora #$30
        pha
        txa
        ora #$30
        tax
        pla
        rts

; ===========================================================================
; keyboard (own matrix scan; KERNAL is switched off)
; ===========================================================================
scan_keys:
        lda #0
        sta shiftf
        sta found
        sta scan_col
        lda #$FE
        sta tmp
@col:   lda tmp
        sta CIA1_PRA
        lda CIA1_PRB
        eor #$FF
        beq @next
        sta tmp2
        ldy #0
@row:   lsr tmp2
        bcc @nr
        lda scan_col
        asl
        asl
        asl
        sty kcnt
        ora kcnt
        cmp #15                 ; left shift
        beq @shift
        cmp #52                 ; right shift
        beq @shift
        ldx found
        bne @nr
        tax
        lda keytab,x
        sta found
        jmp @nr
@shift: lda #1
        sta shiftf
@nr:    iny
        cpy #8
        bne @row
@next:  sec
        rol tmp
        inc scan_col
        lda scan_col
        cmp #8
        bne @col
        lda #$FF
        sta CIA1_PRA
        lda found
        beq @joy
        ldx shiftf
        beq @ret
        cmp #K_RIGHT
        bne @c2
        lda #K_LEFT
        rts
@c2:    cmp #K_DOWN
        bne @ret
        lda #K_UP
@ret:   rts
@joy:   lda CIA1_PRA            ; joystick in port 2
        eor #$FF
        and #$1F
        beq @none
        lsr
        bcs @ju
        lsr
        bcs @jd
        lsr
        bcs @jl
        lsr
        bcs @jr
        lda #K_RETURN
        rts
@ju:    lda #K_UP
        rts
@jd:    lda #K_DOWN
        rts
@jl:    lda #K_LEFT
        rts
@jr:    lda #K_RIGHT
        rts
@none:  lda #0
        rts

; one event per key press; cursor/volume keys auto-repeat
get_key:
        jsr scan_keys
        tax
        beq @none
        cpx key_last
        beq @held
        stx key_last
        lda #18
        sta key_timer
        txa
        rts
@held:  dec key_timer
        bne @zero
        lda #3
        sta key_timer
        cpx #K_DOWN
        beq @rep
        cpx #K_UP
        beq @rep
        cpx #$2B
        beq @rep
        cpx #$2D
        beq @rep
@zero:  lda #0
        rts
@rep:   txa
        rts
@none:  lda #0
        sta key_last
        rts

wait_key:
@w:     lda tick
        beq @w
        lda #0
        sta tick
        jsr get_key
        beq @w
        rts

; ===========================================================================
; setup screen: edit "host:port", connect, handshake
; ===========================================================================
setup_screen:
        jsr set_text_mode
        jsr clear_text
        PRINT 1, 0, s_title
        PRINT 3, 0, s_prompt
        PRINT 4, 0, s_hint
        jsr draw_link
        lda show_lost
        beq @nolost
        PRINT 9, 0, s_lost
        lda #0
        sta show_lost
@nolost:
        lda HOSTBUF
        bne @have
        ldx #0
@cp:    lda host_default,x
        sta HOSTBUF,x
        beq @cpd
        inx
        cpx #39
        bne @cp
        lda #0
        sta HOSTBUF,x
@cpd:   stx host_len
@have:  jsr screen_on_text
@draw:  jsr draw_input
@wait:  lda tick
        beq @wait
        lda #0
        sta tick
        jsr get_key
        beq @wait
        cmp #K_RETURN
        beq @done
        cmp #$85                ; F1: switch link UCI <-> serial
        bne @nf1
        lda transport
        eor #1
        sta transport
        jsr draw_link
        jmp @wait
@nf1:   cmp #K_DEL
        beq @del
        cmp #$41
        bcc @chk
        cmp #$5B
        bcs @chk
        clc
        adc #$20                ; letters -> lower case ASCII
        jmp @add
@chk:   cmp #$30
        bcc @sp
        cmp #$3B                ; '0'..':'
        bcc @add
@sp:    cmp #$2D
        beq @add
        cmp #$2E
        beq @add
        jmp @wait
@add:   ldx host_len
        cpx #38
        bcs @wait
        sta HOSTBUF,x
        inx
        lda #0
        sta HOSTBUF,x
        stx host_len
        jmp @draw
@del:   ldx host_len
        beq @wait
        dex
        lda #0
        sta HOSTBUF,x
        stx host_len
        jmp @draw
@done:  rts

draw_link:
        lda transport
        bne @ser
        PRINT 8, 0, s_link_uci
        rts
@ser:   PRINT 8, 0, s_link_ser
        rts

draw_input:
        SETPTR dst, SCREEN + 6 * 40
        ldy #0
@l:     cpy host_len
        bcc @ch
        beq @cur
        lda #$20
        jmp @st
@cur:   lda #$A0
        jmp @st
@ch:    lda HOSTBUF,y
        cmp #$61
        bcc @st
        sbc #$60
@st:    sta (dst),y
        iny
        cpy #39
        bne @l
        rts

; HOSTBUF "host[:port]" -> UCI connect command in CMDBUF
build_connect:
        lda #3
        sta CMDBUF
        lda #7
        sta CMDBUF+1
        lda #<6464
        sta port_lo
        lda #>6464
        sta port_hi
        ldx #0
        ldy #4
@h:     lda HOSTBUF,x
        beq @end
        cmp #$3A
        beq @port
        sta CMDBUF,y
        inx
        iny
        jmp @h
@port:  inx
        lda #0
        sta port_lo
        sta port_hi
@d:     lda HOSTBUF,x
        beq @end
        sec
        sbc #$30
        cmp #10
        bcs @end
        sta tmp
        lda port_lo
        sta tmp2
        lda port_hi
        sta tmp2+1
        asl port_lo
        rol port_hi
        asl port_lo
        rol port_hi
        clc
        lda port_lo
        adc tmp2
        sta port_lo
        lda port_hi
        adc tmp2+1
        sta port_hi
        asl port_lo
        rol port_hi
        clc
        lda port_lo
        adc tmp
        sta port_lo
        lda port_hi
        adc #0
        sta port_hi
        inx
        jmp @d
@end:   lda #0
        sta CMDBUF,y
        iny
        sty uci_len
        lda port_lo
        sta CMDBUF+2
        lda port_hi
        sta CMDBUF+3
        rts

connect:                        ; C=1 on failure
        PRINT 9, 0, s_connecting
        lda transport
        beq @u
        jmp acia_connect
@u:     jsr acia_off
        lda UCI_ID
        cmp #$C9
        beq @uci
        cmp #$49
        beq @uci
        PRINT 9, 0, s_nouci
        sec
        rts
@uci:   jsr build_connect
        jsr uci_exec
        bcs conn_fail
        lda status0
        cmp #$30
        bne conn_fail
        lda status1
        cmp #$30
        bne conn_fail
        lda RXBUF
        sta sock
connect_hs:                     ; common handshake (UCI socket open / serial link up)
        lda #0
        sta ps_state
        sta ev_hello
        sta disc_ready
        sta conn_lost
        sta uci_fail
        sta cover_ready
        lda #ST_MENU            ; lets net_service run during the handshake
        sta app_state
        jsr send_hello
        lda #<500
        sta cnt
        lda #>500
        sta cnt+1
@wait:  lda #1
        sta poll_now
        jsr net_service
        lda ev_hello
        beq @tk
        lda disc_ready
        bne @ok
@tk:    lda tick
        beq @wait
        lda #0
        sta tick
        lda cnt
        bne @d1
        dec cnt+1
@d1:    dec cnt
        lda cnt
        ora cnt+1
        bne @wait
        lda #ST_SETUP
        sta app_state
        PRINT 9, 0, s_nohello
        sec
        rts
@ok:    clc
        rts
conn_fail:
        lda #ST_SETUP
        sta app_state
        PRINT 9, 0, s_connfail
        sec
        rts

; ===========================================================================
; album menu
; ===========================================================================
menu_enter:
        lda #ST_MENU
        sta app_state
        jsr set_text_mode
        jsr clear_text
        jsr menu_draw_all
        jmp screen_on_text

menu_draw_all:
        jsr menu_header
        jsr menu_draw_list
        jsr menu_hilite
        jmp menu_footer

menu_header:
        PRINT 0, 0, s_head
        PRINT 0, 10, s_sortlbl
        ldx sort
        lda sort_lo,x
        sta src
        lda sort_hi,x
        sta src+1
        SETPTR dst, SCREEN + 16
        jsr puts
        PRINT 0, 29, s_pglbl
        lda page
        clc
        adc #1
        jsr bin2dec2
        sta SCREEN+36
        txa
        sta SCREEN+35
        ldx #0
        lda #3
        jmp row_color

clear_list_area:
        SETPTR dst, SCREEN + 80
        lda #$20
        ldx #3
        ldy #0
@p:     sta (dst),y
        iny
        bne @p
        inc dst+1
        dex
        bne @p
@t:     sta (dst),y
        iny
        cpy #32
        bne @t
        rts

menu_draw_list:
        jsr clear_list_area
        lda list_count
        bne @go
        PRINT 3, 1, s_noalb
        rts
@go:    SETPTR lbp, LISTBUF + 4
        ldx #0
@row:   stx dl_idx
        lda scr_lo+2,x
        clc
        adc #1
        sta dst
        lda scr_hi+2,x
        adc #0
        sta dst+1
        ldy #37
@c:     lda (lbp),y
        sta (dst),y
        dey
        bpl @c
        ADD16 lbp, 38
        ldx dl_idx
        inx
        cpx list_count
        bne @row
        rts

menu_hilite:
        ldx #0
@l:     cpx list_count
        bcs @done
        stx dl_idx
        lda #15
        cpx sel
        bne @n
        lda #7
@n:     pha
        txa
        clc
        adc #2
        tax
        pla
        jsr row_color
        ldx dl_idx
        lda scr_lo+2,x
        sta dst
        lda scr_hi+2,x
        sta dst+1
        ldy #0
        lda #$20
        cpx sel
        bne @m
        lda #$3E                ; '>'
@m:     sta (dst),y
        ldx dl_idx
        inx
        jmp @l
@done:  rts

menu_footer:
        PRINT 23, 0, s_help
        ldx #23
        lda #12
        jsr row_color
        PRINT 24, 0, s_blank40
        lda err_pending
        beq @ld
        SETPTR lbp, ERRBUF
        SETPTR dst, SCREEN + 24 * 40 + 1
        ldy #37
@e:     lda (lbp),y
        sta (dst),y
        dey
        bpl @e
        ldx #24
        lda #2
        jmp row_color
@ld:    lda loading
        beq @r
        PRINT 24, 0, s_loading
        ldx #24
        lda #7
        jmp row_color
@r:     rts

request_list:
        lda #1
        sta loading
        jsr send_list
        jmp menu_footer

menu_key:
        ldx err_pending
        beq @k
        ldx #0
        stx err_pending
        pha
        jsr menu_footer
        pla
@k:     cmp #K_DOWN
        beq @down
        cmp #K_UP
        beq @up
        cmp #K_RIGHT
        beq @pgnext
        cmp #K_LEFT
        beq @pgprev
        cmp #K_RETURN
        beq @play
        cmp #$53                ; S
        beq @sort
        rts
@down:  ldx sel
        inx
        cpx list_count
        bcs @pgnext
        stx sel
        jmp menu_hilite
@up:    lda sel
        beq @pgprevlast
        dec sel
        jmp menu_hilite
@pgprevlast:
        lda #$FF
        sta sel_after
        jmp @pgprevgo
@pgnext:
        lda list_more
        beq @r
        inc page
        lda #0
        sta sel_after
        jmp request_list
@pgprev:
        lda #0
        sta sel_after
@pgprevgo:
        lda page
        beq @r
        dec page
        jmp request_list
@play:  lda list_count
        beq @r
        lda sel
        jmp start_play
@sort:  lda sort
        clc
        adc #1
        cmp #6
        bcc @s1
        lda #0
@s1:    sta sort
        lda #0
        sta page
        sta sel_after
        jmp request_list
@r:     rts

start_play:                     ; A = index in the current list page
        pha
        jsr begin_skip
        pla
        jsr send_play
        lda #0
        sta err_pending
        jmp np_enter

; ---- packet handlers (run inside the parser; they never send commands) ----
on_list:
        lda LISTBUF
        sta sort
        lda LISTBUF+1
        sta page
        lda LISTBUF+2
        sta list_count
        lda LISTBUF+3
        sta list_more
        lda #0
        sta loading
        lda sel_after
        cmp list_count
        bcc @set
        ldx list_count
        beq @z
        dex
        txa
        jmp @set
@z:     lda #0
@set:   sta sel
        lda #0
        sta sel_after
        lda app_state
        cmp #ST_MENU
        bne @r
        jmp menu_draw_all
@r:     rts

on_error:
        lda #1
        sta err_pending
        lda #0
        sta loading
        lda app_state
        cmp #ST_MENU
        bne @r
        jmp menu_footer
@r:     rts

on_vol:
        lda SMALLBUF
        sta vol_level
        lda app_state
        cmp #ST_NOWPLAY
        bne @r
        jmp np_draw_status
@r:     rts

on_cover:
        lda #1
        sta cover_ready
        lda #0
        sta cur_frame
        lda app_state
        cmp #ST_NOWPLAY
        bne @r
        lda #0
        jmp draw_frame
@r:     rts

on_stop:
        lda skip_audio
        beq @acc
        lda SMALLBUF
        cmp nav_seq
        bne @ign
@acc:   jsr audio_flush
        lda #1
        sta skip_audio
        lda #0
        sta paused
        sta stop_wait
        lda app_state
        cmp #ST_NOWPLAY
        bne @ign
        jmp menu_enter
@ign:   rts

on_track:
        lda skip_audio
        beq @apply
        lda TRACKBUF
        cmp nav_seq
        bne @ign
        lda #0
        sta skip_audio
@apply: jsr audio_flush
        lda TRACKBUF+1
        sta mode
        lda TRACKBUF+2
        sta trk_no
        lda TRACKBUF+3
        sta trk_total
        lda TRACKBUF+4
        sta dur_lo
        lda TRACKBUF+5
        sta dur_hi
        ldx #0
@cp:    lda TRACKBUF+7,x
        sta LINEBUF,x
        inx
        cpx #192
        bne @cp
        lda #0
        sta line_off
        sta line_off+1
        sta line_off+2
        sta sec_lo
        sta sec_hi
        sta frame_div
        sta paused
        sta marq_div
        sta rot_div
        lda #1
        sta prebuf
        lda app_state
        cmp #ST_NOWPLAY
        beq @redraw
        jmp np_enter
@redraw:
        jsr np_draw_all
        jmp sprites_update
@ign:   rts

; ===========================================================================
; now playing (multicolour bitmap, double-width bitmap text)
; ===========================================================================
np_enter:
        lda #ST_NOWPLAY
        sta app_state
        lda #$2B                ; bitmap on, display off while we build
        sta VIC_CTL1
        lda #0
        sta VIC_SPREN
        sta VIC_BORDER
        sta VIC_BG0
        SETPTR dst, BITMAP      ; clear bitmap: 8000 bytes
        lda #0
        ldx #31
        ldy #0
@p:     sta (dst),y
        iny
        bne @p
        inc dst+1
        dex
        bne @p
@t:     sta (dst),y
        iny
        cpy #64
        bne @t
        ldx #0                  ; clear colour matrix + colour RAM
@z:     sta SCREEN,x
        sta SCREEN+$100,x
        sta SCREEN+$200,x
        sta SCREEN+$2E8,x
        sta COLORRAM,x
        sta COLORRAM+$100,x
        sta COLORRAM+$200,x
        sta COLORRAM+$2E8,x
        inx
        bne @z
        jsr draw_disc
        jsr setup_np_rows
        jsr setup_sprites
        lda #$18
        sta VIC_MEM             ; bitmap at $2000, matrix at $0400
        sta VIC_CTL2            ; multicolour, 40 columns
        jsr draw_help
        jsr np_draw_all
        lda cover_ready
        beq @nc
        lda cur_frame
        jsr draw_frame
@nc:    lda #$3B
        sta VIC_CTL1
        rts

draw_disc:
        SETPTR src, DISC_BUF
        SETPTR dst, BITMAP + 96
        ldx #16
@r:     ldy #127
@b:     lda (src),y
        sta (dst),y
        dey
        bpl @b
        ADD16 src, 128
        ADD16 dst, 320
        dex
        bne @r
        SETPTR lbp, DISC_BUF + 2304
        SETPTR dst, SCREEN + 12
        SETPTR dptr, COLORRAM + 12
        ldx #16
@r2:    ldy #15
@c:     lda (src),y
        sta (dst),y
        lda (lbp),y
        sta (dptr),y
        dey
        bpl @c
        ADD16 src, 16
        ADD16 lbp, 16
        ADD16 dst, 40
        ADD16 dptr, 40
        dex
        bne @r2
        rts

setup_np_rows:
        ldx #17
        lda #1
        jsr row_color
        ldx #18
        lda #14
        jsr row_color
        ldx #19
        lda #15
        jsr row_color
        ldx #20
        lda #13
        jsr row_color
        ldx #21
        lda #3
        jsr row_color
        ldx #22
        lda #7
        jsr row_color
        ldx #23
        lda #12
        jsr row_color
        ldx #24
        lda #12
        jsr row_color
        lda scr_lo+20
        sta dst
        lda scr_hi+20
        sta dst+1
        ldy #39
        lda #$BB
@l:     sta (dst),y
        dey
        bpl @l
        rts

setup_sprites:
        ldx #0
@c:     lda DISC_BUF+2560,x
        sta SPR_DATA,x
        inx
        cpx #192
        bne @c
        lda #13
        sta SCREEN+$3F8
        lda #14
        sta SCREEN+$3F9
        lda #15
        sta SCREEN+$3FA
        ldx #0
@p:     lda DISC_BUF+2752,x
        sta VIC_SPRX0,x
        inx
        cpx #6
        bne @p
        lda DISC_BUF+2758
        sta VIC_MSBX
        lda #7
        sta VIC_SPRXEXP
        sta VIC_SPRYEXP
        lda #0
        sta VIC_SPRMC
        lda #15
        sta VIC_SPRCOL0
        sta VIC_SPRCOL0+1
        sta VIC_SPRCOL0+2
        jmp sprites_update

sprites_update:                 ; tonearm only while the record is playing
        lda app_state
        cmp #ST_NOWPLAY
        bne @off
        lda playing
        beq @off
        lda paused
        bne @off
        lda #7
        sta VIC_SPREN
        rts
@off:   lda #0
        sta VIC_SPREN
        rts

; ---- rotating label: copy frame A (0..31) into the bitmap/matrix/colour RAM
draw_frame:
        sta tmp
        asl
        asl
        clc
        adc tmp                 ; frame * 5
        pha
        lsr
        clc
        adc #>FRAMES
        sta src+1
        pla
        and #1
        beq @z
        lda #$80
@z:     sta src                 ; frame * 640 = (f*5) << 7
        SETPTR dst, BITMAP + 4 * 320 + 128
        ldx #8
@row:   ldy #63
@b:     lda (src),y
        sta (dst),y
        dey
        bpl @b
        ADD16 src, 64
        ADD16 dst, 320
        dex
        bne @row
        lda src                 ; src = frame + 512 : matrix; colours follow 64 later
        sta lbp
        lda src+1
        sta lbp+1
        ADD16 lbp, 64
        SETPTR dst, SCREEN + 4 * 40 + 16
        SETPTR dptr, COLORRAM + 4 * 40 + 16
        ldx #8
@r2:    ldy #7
@c:     lda (src),y
        sta (dst),y
        lda (lbp),y
        sta (dptr),y
        dey
        bpl @c
        ADD16 src, 8
        ADD16 lbp, 8
        ADD16 dst, 40
        ADD16 dptr, 40
        dex
        bne @r2
        rts

; ---- double-width glyph: A = screen code, g_row/g_col = cell row / glyph column
draw_glyph:
        sta tmp
        lda #0
        sta fptr+1
        lda tmp
        asl
        rol fptr+1
        asl
        rol fptr+1
        asl
        rol fptr+1
        sta fptr
        lda fptr+1
        clc
        adc #>FONT
        sta fptr+1
        ldx g_row
        lda bm_lo,x
        sta dptr
        lda bm_hi,x
        sta dptr+1
        lda g_col
        lsr
        lsr
        lsr
        lsr
        sta tmp2+1
        lda g_col
        asl
        asl
        asl
        asl
        clc
        adc dptr
        sta dptr
        lda dptr+1
        adc tmp2+1
        sta dptr+1
        clc
        lda dptr
        adc #8
        sta dptr2
        lda dptr+1
        adc #0
        sta dptr2+1
        ldy #0
@g:     lda (fptr),y
        pha
        lsr
        lsr
        lsr
        lsr
        tax
        lda nibtab,x
        sta (dptr),y
        pla
        and #$0F
        tax
        lda nibtab,x
        sta (dptr2),y
        iny
        cpy #8
        bne @g
        rts

draw_ch:
        jsr draw_glyph
        inc g_col
        rts

draw_str:                       ; src = string, g_row/g_col = start
        ldy #0
@l:     lda (src),y
        beq @d
        sty ds_y
        jsr draw_glyph
        inc g_col
        ldy ds_y
        iny
        jmp @l
@d:     rts

draw_num2:
        jsr bin2dec2
        pha
        txa
        jsr draw_ch
        pla
        jmp draw_ch

.macro DSTR row, col, label
        lda #row
        sta g_row
        lda #col
        sta g_col
        SETPTR src, label
        jsr draw_str
.endmacro

draw_help:
        DSTR 23, 0, s_h1
        DSTR 24, 0, s_h2
        rts

; one text line (title/artist/album): X = 0..2, shows a 20-char window
draw_line:
        stx dl_idx
        lda lbuf_lo,x
        sta lbp
        lda lbuf_hi,x
        sta lbp+1
        ldy #0
        lda (lbp),y
        sta dl_len
        lda line_row,x
        sta g_row
        lda #0
        sta g_col
        sta dl_i
        lda dl_len
        clc
        adc #3
        sta dl_per
@ch:    lda dl_len
        cmp #21
        bcs @scroll
        lda dl_i
        jmp @have
@scroll:
        ldx dl_idx
        lda line_off,x
        clc
        adc dl_i
        cmp dl_per
        bcc @have
        sbc dl_per
@have:  cmp dl_len
        bcs @space
        tay
        iny
        lda (lbp),y
        jmp @draw
@space: lda #$20
@draw:  jsr draw_glyph
        inc g_col
        inc dl_i
        lda dl_i
        cmp #20
        bne @ch
        rts

fmt_time:                       ; tmp = seconds, X = offset in TIMEBUF
        stx tmp2
        ldx #0
@lp:    lda tmp+1
        bne @sub
        lda tmp
        cmp #60
        bcc @done
@sub:   sec
        lda tmp
        sbc #60
        sta tmp
        lda tmp+1
        sbc #0
        sta tmp+1
        inx
        jmp @lp
@done:  lda tmp
        pha
        txa
        cmp #100
        bcc @m
        lda #99
@m:     jsr bin2dec2
        ldy tmp2
        sta TIMEBUF+1,y
        txa
        sta TIMEBUF,y
        lda #$3A
        sta TIMEBUF+2,y
        pla
        jsr bin2dec2
        ldy tmp2
        sta TIMEBUF+4,y
        txa
        sta TIMEBUF+3,y
        rts

np_update_time:
        lda sec_lo
        sta tmp
        lda sec_hi
        sta tmp+1
        ldx #0
        jsr fmt_time
        lda #$2F
        sta TIMEBUF+5
        lda dur_lo
        sta tmp
        lda dur_hi
        sta tmp+1
        ldx #6
        jsr fmt_time
        lda #0
        sta TIMEBUF+11
        lda #21
        sta g_row
        lda #0
        sta g_col
        SETPTR src, TIMEBUF
        jsr draw_str
        ; progress = elapsed * 40 / duration
        lda #0
        sta bar_n
        lda dur_lo
        ora dur_hi
        beq @draw
        lda sec_hi
        cmp #6
        bcs @full
        lda sec_lo              ; tmp = sec*32 + sec*8
        sta tmp
        lda sec_hi
        sta tmp+1
        ldx #3
@s8:    asl tmp
        rol tmp+1
        dex
        bne @s8
        lda tmp
        sta tmp2
        lda tmp+1
        sta tmp2+1
        asl tmp
        rol tmp+1
        asl tmp
        rol tmp+1
        clc
        lda tmp
        adc tmp2
        sta tmp
        lda tmp+1
        adc tmp2+1
        sta tmp+1
@div:   lda tmp+1
        cmp dur_hi
        bcc @draw
        bne @sub
        lda tmp
        cmp dur_lo
        bcc @draw
@sub:   sec
        lda tmp
        sbc dur_lo
        sta tmp
        lda tmp+1
        sbc dur_hi
        sta tmp+1
        inc bar_n
        lda bar_n
        cmp #40
        bcc @div
        jmp @draw
@full:  lda #40
        sta bar_n
@draw:  jsr draw_bar
        rts

draw_bar:
        lda bm_lo+20
        sta dptr
        lda bm_hi+20
        sta dptr+1
        ldx #0
@c:     lda #$55
        cpx bar_n
        bcs @u
        lda #$FF
@u:     ldy #3
        sta (dptr),y
        iny
        sta (dptr),y
        ADD16 dptr, 8
        inx
        cpx #40
        bne @c
        rts

np_draw_status:                 ; mode / pause, track number, volume
        lda #21
        sta g_row
        lda #12
        sta g_col
        lda paused
        beq @np
        SETPTR src, s_paused
        jmp @ds
@np:    lda prebuf
        bne @ld
        lda playing
        beq @ld
        lda mode
        bne @sid
        SETPTR src, s_digi
        jmp @ds
@sid:   SETPTR src, s_sid
        jmp @ds
@ld:    SETPTR src, s_load
@ds:    jsr draw_str
        lda #22
        sta g_row
        lda #0
        sta g_col
        SETPTR src, s_trk
        jsr draw_str
        lda trk_no
        jsr draw_num2
        lda #$2F
        jsr draw_ch
        lda trk_total
        jsr draw_num2
        lda #14
        sta g_col
        SETPTR src, s_vol
        jsr draw_str
        lda vol_level
        jmp draw_num2

np_draw_all:
        ldx #0
        jsr draw_line
        ldx #1
        jsr draw_line
        ldx #2
        jsr draw_line
        jsr np_update_time
        jmp np_draw_status

np_key:
        cmp #$4E                ; N
        beq @next
        cmp #K_RIGHT
        beq @next
        cmp #$50                ; P
        beq @prev
        cmp #K_LEFT
        beq @prev
        cmp #K_SPACE
        beq @pause
        cmp #$4D                ; M
        beq @mode
        cmp #$2B                ; +
        beq @volup
        cmp #$2D                ; -
        beq @voldn
        cmp #$51                ; Q
        beq @quit
        cmp #K_STOP
        beq @quit
        cmp #$5F                ; back-arrow key
        beq @quit
        rts
@next:  jsr begin_skip
        jsr np_busy
        jmp send_next
@prev:  jsr begin_skip
        jsr np_busy
        jmp send_prev
@mode:  jsr begin_skip
        jsr np_busy
        jmp send_mode
@pause: lda playing
        beq @r
        lda paused
        eor #1
        sta paused
        beq @res
        jsr audio_pause
        jmp @pd
@res:   jsr audio_resume
@pd:    jsr sprites_update
        jmp np_draw_status
@volup: lda #1
        jmp send_vol
@voldn: lda #$FF
        jmp send_vol
@quit:  jsr begin_skip
        jsr np_busy
        lda #1
        sta stop_wait
        jmp send_stop
@r:     rts

np_busy:
        jsr sprites_update
        jmp np_draw_status

; per-frame work while the vinyl screen is up
np_tick:
        jsr get_key
        beq @nk
        jsr np_key
@nk:    lda playing
        beq @ev
        lda paused
        bne @ev
        lda cover_ready
        beq @mq
        inc rot_div
        lda rot_div
        ldx video
        cmp rot_steps,x
        bcc @mq
        lda #0
        sta rot_div
        lda cur_frame
        clc
        adc #1
        and #NFRAMES-1
        sta cur_frame
        jsr draw_frame
@mq:    inc marq_div
        lda marq_div
        cmp #MARQ_TICKS
        bcc @ev
        lda #0
        sta marq_div
        ldx #0
@ml:    stx dl_idx
        lda lbuf_lo,x
        sta lbp
        lda lbuf_hi,x
        sta lbp+1
        ldy #0
        lda (lbp),y
        cmp #21
        bcc @mn
        clc
        adc #3                  ; period
        sta tmp
        ldx dl_idx
        inc line_off,x
        lda line_off,x
        cmp tmp
        bcc @md
        lda #0
        sta line_off,x
@md:    jsr draw_line
@mn:    ldx dl_idx
        inx
        cpx #3
        bne @ml
@ev:    lda ev_sec
        beq @pl
        lda #0
        sta ev_sec
        jsr np_update_time
@pl:    lda playing
        beq @sp
        lda #0
        sta tmp+1               ; (placeholder to keep alignment)
@sp:    jmp np_status_sync

; keep the status line and tonearm in step with the engine (cheap, change-driven)
np_status_sync:
        lda playing
        cmp last_playing
        beq @r
        sta last_playing
        jsr sprites_update
        jmp np_draw_status
@r:     rts

.segment "RODATA"
rot_steps:  .byte ROT_STEP_TICKS, ROT_STEP_TICKS + 1

.segment "BSS"
last_playing: .res 1

.segment "CODE"
; ===========================================================================
; main program
; ===========================================================================
app_main:
        lda #ST_SETUP
        sta app_state
        lda #1
        sta skip_audio
        lda #0
        sta prebuf
        jsr audio_flush
        lda #0
        sta ps_state
        sta conn_lost
        sta uci_fail
        sta poll_now
        sta paused
        sta stop_wait
        sta last_playing
@setup: jsr setup_screen
        jsr connect
        bcc @ok
        jsr wait_key
        jmp @setup
@ok:    lda #0
        sta sort
        sta page
        sta sel
        sta sel_after
        sta list_count
        sta list_more
        sta err_pending
        jsr menu_enter
        jsr request_list

main_loop:
        lda tick
        beq @nt
        lda #0
        sta tick
        lda #1
        sta poll_now
        jsr on_tick
@nt:    jsr net_service
        jsr audio_poll
        lda conn_lost
        beq main_loop
        ; connection lost: back to the setup screen
        lda #1
        sta show_lost
        lda #ST_SETUP
        sta app_state
        jmp app_main

on_tick:
        lda app_state
        cmp #ST_NOWPLAY
        beq @np
        cmp #ST_MENU
        bne @r
        jsr get_key
        beq @r
        jmp menu_key
@np:    jmp np_tick
@r:     rts
