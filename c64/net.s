; ---------------------------------------------------------------------------
; Network layer: socket pump, packet parser, command senders
; ---------------------------------------------------------------------------
.segment "BSS"
poll_now:   .res 1
conn_lost:  .res 1
bp_idx:     .res 1
ps_sink:    .res 1              ; 0 linear, 1 digi, 2 sid, 3 discard
pn_save:    .res 2
uci_fail:   .res 1

.segment "RODATA"
; linear sinks: packet type -> destination buffer
bp_type:    .byte PKT_HELLO, PKT_LIST, PKT_TRACK, PKT_COVER, PKT_STOP, PKT_ERROR, PKT_VOL, PKT_DISC
bp_lo:      .byte <SMALLBUF, <LISTBUF, <TRACKBUF, <FRAMES, <SMALLBUF, <ERRBUF, <SMALLBUF, <DISC_BUF
bp_hi:      .byte >SMALLBUF, >LISTBUF, >TRACKBUF, >FRAMES, >SMALLBUF, >ERRBUF, >SMALLBUF, >DISC_BUF
BP_N = 8

.segment "CODE"

; ---- how many bytes may we request right now?  C=1: don't read.
compute_want:
        lda app_state
        cmp #ST_NOWPLAY
        bne @full
        lda skip_audio
        bne @full
        lda mode
        bne @sid

        ; digi ring: room = fp*256 + (255 - wr_lo), fp = (rd_page - wr_page - 1) & 31
        lda nmi_fetch+2
        sec
        sbc dg_wr+1
        sec
        sbc #1
        and #$1F
        sta tmp+1               ; fp
        lda #$FF
        sec
        sbc dg_wr
        sta tmp                 ; low part
        lda tmp+1
        cmp #2
        bcs @full
        sta tmp2+1
        lda tmp
        sta tmp2
        lda tmp2+1
        bne @ok                 ; at least 256
        lda tmp2
        cmp #NO_ROOM_MIN
        bcc @none
@ok:    clc
        rts

@sid:   lda #SID_NFR-1
        sec
        sbc sid_count
        bcc @none
        cmp #2
        bcc @none
        cmp #24
        bcs @full
        tax
        lda #0
        sta tmp2
        sta tmp2+1
@mul:   clc
        lda tmp2
        adc #22
        sta tmp2
        bcc @m1
        inc tmp2+1
@m1:    dex
        bne @mul
        clc
        rts

@full:  lda #<512
        sta tmp2
        lda #>512
        sta tmp2+1
        clc
        rts
@none:  sec
        rts

; ---- poll the bridge once and feed whatever arrived to the parser
net_service:
        lda app_state
        cmp #ST_SETUP
        beq @ret
        lda poll_now
        beq @ret
        jsr compute_want
        bcs @ret
        jsr sock_read
        bcs @fail
        lda #0
        sta uci_fail
        lda rx_count
        cmp #3
        bcs @data
        lda rx_count+1
        bne @data
        lda status0             ; no payload: "01" means the bridge closed the socket
        cmp #$30
        bne @lost
        lda status1
        cmp #$31
        beq @lost
        lda #0
        sta poll_now
@ret:   rts
@data:  jsr parse_chunk
        rts
@fail:  inc uci_fail
        lda #0
        sta poll_now
        lda uci_fail
        cmp #3
        bcc @r2
        lda #1
        sta conn_lost
@r2:    rts
@lost:  lda #1
        sta conn_lost
        rts

; ---- packet parser -------------------------------------------------------
; RXBUF = [len lo][len hi][bytes...]
parse_chunk:
        lda RXBUF
        sta pcnt
        lda RXBUF+1
        sta pcnt+1
        lda rx_count            ; never trust more than we staged
        sec
        sbc #2
        sta tmp
        lda rx_count+1
        sbc #0
        sta tmp+1
        lda pcnt+1
        cmp tmp+1
        bcc @lenok
        bne @clamp
        lda pcnt
        cmp tmp
        bcc @lenok
@clamp: lda tmp
        sta pcnt
        lda tmp+1
        sta pcnt+1
@lenok: lda #<(RXBUF+2)
        sta psrc
        lda #>(RXBUF+2)
        sta psrc+1

@loop:  lda pcnt
        ora pcnt+1
        bne @more
        rts
@more:  lda ps_state
        cmp #3
        beq @payload
        ldy #0
        lda (psrc),y
        inc psrc
        bne @h0
        inc psrc+1
@h0:    tax                     ; X = byte
        lda pcnt
        bne @h1
        dec pcnt+1
@h1:    dec pcnt
        lda ps_state
        bne @s1
        stx ps_type
        inc ps_state
        jmp @loop
@s1:    cmp #1
        bne @s2
        stx ps_len
        inc ps_state
        jmp @loop
@s2:    stx ps_len+1
        lda #3
        sta ps_state
        jsr begin_packet
        lda ps_len
        ora ps_len+1
        beq @pend
        jmp @loop
@pend:  jsr end_packet          ; empty payload
        lda #0
        sta ps_state
        jmp @loop

@payload:
        ; pn = min(pcnt, ps_len)
        lda pcnt+1
        cmp ps_len+1
        bcc @usec
        bne @usel
        lda pcnt
        cmp ps_len
        bcc @usec
@usel:  lda ps_len
        sta pn
        lda ps_len+1
        sta pn+1
        jmp @go
@usec:  lda pcnt
        sta pn
        lda pcnt+1
        sta pn+1
@go:    lda ps_sink
        beq @lin
        cmp #1
        beq @dg
        cmp #2
        beq @sd
        jsr sink_discard
        jmp @adv
@lin:   jsr sink_linear
        jmp @adv
@dg:    jsr sink_digi
        jmp @adv
@sd:    jsr sink_sid
@adv:   ; pcnt -= pn ; ps_len -= pn  (pn was consumed by the sink: recompute from lengths)
        lda pcnt
        sec
        sbc pn_save
        sta pcnt
        lda pcnt+1
        sbc pn_save+1
        sta pcnt+1
        lda ps_len
        sec
        sbc pn_save
        sta ps_len
        lda ps_len+1
        sbc pn_save+1
        sta ps_len+1
        lda ps_len
        ora ps_len+1
        beq @pend2
        jmp @loop
@pend2:  jsr end_packet
        lda #0
        sta ps_state
        jmp @loop

; choose the sink for a new packet
begin_packet:
        ldx #0
@f:     lda bp_type,x
        cmp ps_type
        beq @found
        inx
        cpx #BP_N
        bne @f
        lda ps_type             ; digi / sid / unknown
        cmp #PKT_DIGI
        bne @n1
        lda skip_audio
        bne @disc
        lda #1
        sta ps_sink
        rts
@n1:    cmp #PKT_SID
        bne @disc
        lda skip_audio
        bne @disc
        lda #2
        sta ps_sink
        rts
@disc:  lda #3
        sta ps_sink
        rts
@found: lda bp_lo,x
        sta ps_dst
        lda bp_hi,x
        sta ps_dst+1
        lda #0
        sta ps_sink
        lda ps_type
        cmp #PKT_COVER
        bne @r
        lda #0
        sta cover_ready
@r:     rts

end_packet:
        lda ps_type
        cmp #PKT_HELLO
        bne @a
        lda SMALLBUF+1          ; digi sample-rate selector (0 = 8 kHz, 1 = 3.5 kHz)
        sta dsel
        lda #1
        sta ev_hello
        rts
@a:     cmp #PKT_LIST
        bne @b
        jmp on_list
@b:     cmp #PKT_TRACK
        bne @c
        jmp on_track
@c:     cmp #PKT_COVER
        bne @d
        jmp on_cover
@d:     cmp #PKT_STOP
        bne @e
        jmp on_stop
@e:     cmp #PKT_ERROR
        bne @f
        jmp on_error
@f:     cmp #PKT_VOL
        bne @g
        jmp on_vol
@g:     cmp #PKT_DISC
        bne @h
        lda #1
        sta disc_ready
@h:     rts

; ---- sinks: copy pn bytes (also saved in pn_save) -----------------------------
sink_discard:
        jsr save_pn
        clc
        lda psrc
        adc pn
        sta psrc
        lda psrc+1
        adc pn+1
        sta psrc+1
        rts

save_pn:
        lda pn
        sta pn_save
        lda pn+1
        sta pn_save+1
        rts

sink_linear:
        jsr save_pn
        ldy #0
        ldx pn+1
        beq @tail
@pg:    lda (psrc),y
        sta (ps_dst),y
        iny
        bne @pg
        inc psrc+1
        inc ps_dst+1
        dex
        bne @pg
@tail:  ldx pn
        beq @done
@t:     lda (psrc),y
        sta (ps_dst),y
        iny
        dex
        bne @t
        clc
        tya
        adc psrc
        sta psrc
        bcc @a
        inc psrc+1
@a:     clc
        tya
        adc ps_dst
        sta ps_dst
        bcc @done
        inc ps_dst+1
@done:  rts

; digi ring writer: bursts that never cross a ring page
sink_digi:
        jsr save_pn
@next:  lda pn
        ora pn+1
        beq @done
        sec
        lda #0
        sbc dg_wr
        bne @k1
        lda #255
@k1:    ldx pn+1
        bne @go
        cmp pn
        bcc @go
        lda pn
@go:    sta kcnt
        ldy #0
@cp:    lda (psrc),y
        sta (dg_wr),y
        iny
        cpy kcnt
        bne @cp
        clc
        lda psrc
        adc kcnt
        sta psrc
        bcc @p1
        inc psrc+1
@p1:    sec
        lda pn
        sbc kcnt
        sta pn
        bcs @p2
        dec pn+1
@p2:    clc
        lda dg_wr
        adc kcnt
        sta dg_wr
        bcc @next
        inc dg_wr+1
        lda dg_wr+1
        cmp #DG_END_HI
        bne @pub
        lda #>DG_BASE
        sta dg_wr+1
@pub:   lda dg_wr+1
        sta pub_page
        jmp @next
@done:  rts

; sid ring writer: byte by byte, counting finished 22-byte frames
sink_sid:
        jsr save_pn
@lp:    lda pn
        ora pn+1
        beq @done
        ldy #0
        lda (psrc),y
        sta (sid_wr),y
        inc psrc
        bne @a
        inc psrc+1
@a:     inc sid_wr
        bne @b
        inc sid_wr+1
@b:     lda sid_wr+1
        cmp #>SID_END
        bcc @nw
        bne @wr
        lda sid_wr
        cmp #<SID_END
        bcc @nw
@wr:    lda #<SID_BASE
        sta sid_wr
        lda #>SID_BASE
        sta sid_wr+1
@nw:    inc sid_part
        lda sid_part
        cmp #22
        bne @d
        lda #0
        sta sid_part
        inc sid_count
@d:     lda pn
        bne @e
        dec pn+1
@e:     dec pn
        jmp @lp
@done:  rts

; ---- command senders (never call these from inside a packet handler) ----------
send_hello:
        lda #CMD_HELLO
        sta CMDBUF+3
        lda video
        sta CMDBUF+4
        lda #2                  ; protocol version
        sta CMDBUF+5
        lda transport
        sta CMDBUF+6
        lda #4
        jmp net_send

send_list:
        lda #CMD_LIST
        sta CMDBUF+3
        lda sort
        sta CMDBUF+4
        lda page
        sta CMDBUF+5
        lda #3
        jmp net_send

send_play:                      ; A = index in list
        sta CMDBUF+4
        lda #CMD_PLAY
        sta CMDBUF+3
        inc nav_seq
        lda nav_seq
        sta CMDBUF+5
        lda #3
        jmp net_send

send_next:
        lda #CMD_NEXT
        jmp send_nav
send_prev:
        lda #CMD_PREV
        jmp send_nav
send_stop:
        lda #CMD_STOP
send_nav:
        sta CMDBUF+3
        inc nav_seq
        lda nav_seq
        sta CMDBUF+4
        lda #2
        jmp net_send

send_mode:                      ; request the other mode at the current position
        lda #CMD_MODE
        sta CMDBUF+3
        lda mode
        eor #1
        sta CMDBUF+4
        inc nav_seq
        lda nav_seq
        sta CMDBUF+5
        lda sec_lo
        sta CMDBUF+6
        lda sec_hi
        sta CMDBUF+7
        lda #5
        jmp net_send

send_vol:                       ; A = signed delta
        sta CMDBUF+4
        lda #CMD_VOL
        sta CMDBUF+3
        lda #2
        jmp net_send

; user navigated: drop buffered audio and ignore stale packets until acknowledged
begin_skip:
        lda #1
        sta skip_audio
        lda #0
        sta prebuf
        jmp audio_flush
