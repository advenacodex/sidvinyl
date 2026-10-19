; ---------------------------------------------------------------------------
; Interrupt handlers and audio engine control
;   NMI  (CIA2 timer A, ~8 kHz): 4-bit digi playback from the ring buffer
;   IRQ  (raster, 1/frame):      frame tick, clock, SID register frames
; ---------------------------------------------------------------------------
.segment "RODATA"
; CIA2 timer A latch per digi profile, index = dsel*2 + video:
;   [8 kHz PAL, 8 kHz NTSC, 3.5 kHz PAL, 3.5 kHz NTSC]   (3.5 kHz = the serial link's 19200 baud budget)
latch_lo:   .byte 122, 127, 24, 35
latch_hi:   .byte 0, 0, 1, 1

.segment "CODE"

irq_handler:
        pha
        txa
        pha
        tya
        pha
        lda VIC_IRQ
        and #$01
        beq @out
        sta VIC_IRQ             ; acknowledge raster interrupt
        lda #1
        sta tick
        lda playing
        beq @out
        lda paused
        bne @out
        inc frame_div
        lda frame_div
        cmp fps
        bcc @sidchk
        lda #0
        sta frame_div
        inc sec_lo
        bne @s1
        inc sec_hi
@s1:    lda #1
        sta ev_sec
@sidchk:
        lda mode
        beq @out
        jsr sid_play_frame
@out:   pla
        tay
        pla
        tax
        pla
        rti

; write one 22-byte frame from the ring to the SID (registers in reverse order)
sid_play_frame:
        lda sid_count
        beq @done
        ldy #20
@l:     lda (sid_rd),y
        sta SID,y
        dey
        bpl @l
        ldy #21
        lda (sid_rd),y
        sta SID_VOL
        clc
        lda sid_rd
        adc #22
        sta sid_rd
        bcc @c
        inc sid_rd+1
@c:     lda sid_rd+1
        cmp #>SID_END
        bcc @ok
        bne @wrap
        lda sid_rd
        cmp #<SID_END
        bcc @ok
@wrap:  lda #<SID_BASE
        sta sid_rd
        lda #>SID_BASE
        sta sid_rd+1
@ok:    dec sid_count
@done:  rts

; digi: two 4-bit samples per ring byte, high nibble first
nmi_handler:
        pha
nmi_body:
        lda CIA2_ICR
nmi_gate:
        and #$01                ; operand patched to 0 while paused on the serial link
        beq nmi_out             ; RESTORE key, ACIA or other: no digi sample due
        lda dg_phase
        bne nmi_low
        lda nmi_fetch+2         ; read page == published write page -> nothing to play
        cmp pub_page
        beq nmi_out
nmi_fetch:
        lda DG_BASE
        sta dg_byte
        lsr
        lsr
        lsr
        lsr
        sta SID_VOL
        inc nmi_fetch+1
        bne @adv
        inc nmi_fetch+2
        lda nmi_fetch+2
        cmp #DG_END_HI
        bne @adv
        lda #>DG_BASE
        sta nmi_fetch+2
@adv:   lda #1
        sta dg_phase
nmi_out:
nmi_tail:
        jmp nmi_pla             ; patched to acia_rx when the serial link is active
nmi_low:
        lda dg_byte
        and #$0F
        sta SID_VOL
        lda #0
        sta dg_phase
        jmp nmi_tail
nmi_pla:
        pla
        rti

; Serial transport entry.  The NMI cannot be masked with SEI, and with the longer handler a badline +
; sprite DMA stall can make it outlast the tick: the next NMI then nested between our status
; and data reads of the 6551 and stored one byte twice.  So the timer NMI is masked (ICR bit 0) while we run
; and unmasked on exit; an underflow in between stays flagged and fires right after the RTI.
nmi_ser:
        pha
        lda #$01
        sta CIA2_ICR
        jmp nmi_body
nmi_pla_s:
        lda #$81
        sta CIA2_ICR
        pla
        rti
.assert >nmi_ser = >nmi_handler, error, "nmi_ser must share a page with nmi_handler (vector patched byte-wise)"

; serial link (polled): at 19200 baud the 6551 holds one byte for 513 cycles and the timer NMI
; fires every 281 cycles, so each NMI sees at most one new byte.  (At 38400 baud the 257-cycle
; budget is too tight: VIC badline/sprite DMA stretches the handler and bytes get lost.)
acia_rx:
        lda ACIA_STATUS
        and #$0C                ; $08 receive data register full, $04 overrun
        beq nmi_pla_s
        cmp #$08
        beq acia_rd
        inc acia_ovr            ; diagnostics: a byte was lost before we got to it
acia_rd:
        lda ACIA_DATA
acia_st: sta RXRING
        inc acia_st+1
        bne nmi_pla_s
        inc acia_st+2           ; page crossed
        lda acia_st+2
        cmp #RXRING_END
        bne nmi_pla_s
        lda #RXRING_HI          ; wrap the 2 KB ring
        sta acia_st+2
        jmp nmi_pla_s

; timer A helpers --------------------------------------------------------
digi_timer_start:               ; sample-rate NMI
        lda dsel                ; index = dsel*2 + video
        asl
        ora video
        tax
        lda #0
        sta dg_phase
        ldy latch_hi,x
        lda latch_lo,x
        jmp set_latch_run

poll_timer_start:               ; serial link: 281-cycle tick that only serves the ACIA
        ldy #1
        lda #24
set_latch_run:                  ; Y:A = latch; (re)start the timer unless it already runs.
        sta CIA2_TALO           ; A running timer keeps ticking (no gap: the 6551 holds only
        sty CIA2_TAHI           ; one byte for 513 cycles) and picks up the new period at
        lda CIA2_CRA            ; its next underflow.
        and #$01
        bne @r
        lda CIA2_ICR
        lda #$81
        sta CIA2_ICR
        lda #$11                ; start, force load
        sta CIA2_CRA
@r:     rts

serial_tick_if:                 ; keep the ACIA served when no digi timer is running
        lda transport
        beq @r
        lda #1
        sta nmi_gate+1
        jmp poll_timer_start
@r:     rts

; ---------------------------------------------------------------------------
; audio_flush: stop everything, empty both rings, silence the SID
; ---------------------------------------------------------------------------
audio_flush:
        sei
        lda transport
        beq @stop
        lda #0                  ; serial link: keep the timer NMI running (it serves the
        sta nmi_gate+1          ; ACIA); just switch the digi path off while we reset
        beq @go
@stop:  lda #$01
        sta CIA2_ICR            ; mask timer A NMI
        lda #0
        sta CIA2_CRA
        lda CIA2_ICR
@go:    lda #0
        sta playing
        sta dg_phase
        sta sid_count
        sta sid_part
        lda #<DG_BASE
        sta nmi_fetch+1
        sta dg_wr
        lda #>DG_BASE
        sta nmi_fetch+2
        sta dg_wr+1
        sta pub_page
        lda #<SID_BASE
        sta sid_rd
        sta sid_wr
        lda #>SID_BASE
        sta sid_rd+1
        sta sid_wr+1
        jsr sid_silence
        jsr serial_tick_if
        cli
        rts

sid_silence:
        lda #0
        sta SID+4
        sta SID+11
        sta SID+18
        sta SID_VOL
        rts

sid_clear:
        ldx #$18
        lda #0
@l:     sta SID,x
        dex
        bpl @l
        rts

; start playback in the current mode (called once the pre-roll is buffered)
audio_start:
        lda mode
        bne @sid
        lda #8
        sta SID_VOL
        jsr digi_timer_start
        jmp @run
@sid:   jsr sid_clear
@run:   lda #1
        sta playing
        rts

audio_pause:
        lda mode
        bne @sid
        lda transport
        beq @uci
        lda #0
        sta nmi_gate+1          ; digi NMI keeps ticking for the ACIA but plays nothing
        sta SID_VOL
        jmp poll_timer_start
@uci:   lda #$01
        sta CIA2_ICR
        lda #0
        sta CIA2_CRA
        lda CIA2_ICR
        lda #0
        sta SID_VOL
        rts
@sid:   jmp sid_silence

audio_resume:
        lda mode
        bne @r
        lda #1
        sta nmi_gate+1
        jmp digi_timer_start
@r:     rts

; main loop hook: begin playback when enough audio is buffered
audio_poll:
        lda prebuf
        beq @r
        lda playing
        bne @r
        lda mode
        bne @sid
        lda pub_page            ; pages buffered = pub - read (mod 32)
        sec
        sbc nmi_fetch+2
        and #$1F
        cmp #6
        bcc @r
        jmp @go
@sid:   lda sid_count
        cmp #8
        bcc @r
@go:    lda #0
        sta prebuf
        jsr audio_start
@r:     rts
