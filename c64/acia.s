; ---------------------------------------------------------------------------
; Serial transport: 6551 ACIA in SwiftLink layout ($DE00), 19200 8N1.
; Used by VICE (-acia1 -acia1mode 1 -myaciadev 0 -rsdev1 host:port) and real
; SwiftLink cartridges.  The bridge is credit-flow-controlled: it may only send
; as many bytes as we have granted (CMD_CREDIT), so the 256-byte receive ring
; filled by the timer-NMI handler (acia_rx in audio.s) can never overflow.
; ---------------------------------------------------------------------------
.segment "BSS"
a_want:     .res 1
a_n:        .res 1
a_tmo:      .res 2
a_wl:       .res 1
a_wh:       .res 1
a_ah:       .res 1

.segment "CODE2"

; boot-time guess: Ultimate answers $C9/$49 at $DF1D -> UCI, otherwise serial
detect_transport:
        lda #0
        sta transport
        lda UCI_ID
        cmp #$C9
        beq @r
        cmp #$49
        beq @r
        lda #1
        sta transport
@r:     rts

; point the NMI tail back at the plain "pla / rti" (UCI transport)
acia_off:
        sei
        lda #<nmi_pla
        sta nmi_tail+1
        lda #>nmi_pla
        sta nmi_tail+2
        lda #<nmi_handler       ; same page as nmi_ser: the low byte is the only one that differs
        sta $FFFA
        lda #1
        sta nmi_gate+1
        lda #$01
        sta CIA2_ICR            ; the poll tick is only needed on the serial link
        lda #0
        sta CIA2_CRA
        lda CIA2_ICR
        cli
        rts

acia_init:
        sei
        sta ACIA_STATUS         ; programmed reset
        lda #$1E                ; internal baud generator, 19200 (SwiftLink x2), 8N1
        sta ACIA_CTRL
        lda #$0B                ; DTR on, RTS on, receiver interrupt OFF (polled from the timer NMI)
        sta ACIA_CMD
        lda ACIA_STATUS
        lda ACIA_DATA
        lda #<acia_rx
        sta nmi_tail+1
        lda #>acia_rx
        sta nmi_tail+2
        lda #<nmi_ser
        sta $FFFA
        lda acia_st+1           ; start with an empty ring
        sta rx_rd
        lda acia_st+2
        sta rx_rdh
        jsr rd_units
        sta rx_lim
        jsr serial_tick_if
        cli
        rts

acia_connect:                   ; C=1 on failure (shares the handshake with UCI)
        jsr acia_init
        jsr acia_grant
        jmp connect_hs

; read position in 8-byte units (0..255, mod 2 KB)
rd_units:
        lda rx_rdh
        sec
        sbc #RXRING_HI
        asl
        asl
        asl
        asl
        asl
        sta a_n
        lda rx_rd
        lsr
        lsr
        lsr
        ora a_n
        rts

; grant the bridge more credit whenever >= 16 units (128 bytes) of window are free
acia_grant:
        jsr rd_units
        clc
        adc #RX_WINDOW
        sec
        sbc rx_lim
        cmp #16
        bcc @r
        sta a_n
        clc
        adc rx_lim
        sta rx_lim
        lda #CMD_CREDIT
        sta CMDBUF+3
        lda a_n
        sta CMDBUF+4
        lda #2
        jmp acia_send
@r:     rts

; send A bytes from CMDBUF+3...; C=1 if the transmitter never became ready
acia_send:
        sta a_n
        ldx #0
@next:  lda #0
        sta a_tmo
        sta a_tmo+1
@w:     lda ACIA_STATUS
        and #$10                ; transmit data register empty
        bne @go
        inc a_tmo
        bne @w
        inc a_tmo+1
        bne @w
        sec
        rts
@go:    lda CMDBUF+3,x
        sta ACIA_DATA
        inx
        cpx a_n
        bne @next
        clc
        rts

; same contract as uci_sock_read: tmp2 = bytes wanted, RXBUF = [len lo][len hi][data]
; (at most 255 bytes per call, never across the end of the ring)
acia_sock_read:
        lda tmp2+1
        beq @lo
        lda #$FF
        jmp @have
@lo:    lda tmp2
@have:  sta a_want
@wr:    lda acia_st+2           ; consistent snapshot of the NMI's write pointer
        sta a_wh
        lda acia_st+1
        sta a_wl
        lda acia_st+2
        cmp a_wh
        bne @wr
        lda a_wl                ; avail = (wr - rd) mod 2048
        sec
        sbc rx_rd
        sta a_n
        lda a_wh
        sbc rx_rdh
        and #$07
        bne @big                ; >= 256 bytes available
        lda a_n
        cmp a_want
        bcc @n
@big:   lda a_want
@n:     sta a_n
        lda rx_rdh              ; stop at the end of the ring
        cmp #RXRING_END-1
        bne @cp0
        lda #0
        sec
        sbc rx_rd               ; bytes to the end (0 means 256)
        beq @cp0
        cmp a_n
        bcs @cp0
        sta a_n
@cp0:   lda rx_rd
        sta fptr
        lda rx_rdh
        sta fptr+1
        ldy #0
@c:     cpy a_n
        beq @cd
        lda (fptr),y
        sta RXBUF+2,y
        iny
        bne @c
@cd:    clc
        lda rx_rd
        adc a_n
        sta rx_rd
        bcc @ad
        inc rx_rdh
        lda rx_rdh
        cmp #RXRING_END
        bne @ad
        lda #RXRING_HI
        sta rx_rdh
@ad:    lda a_n
        sta RXBUF
        clc
        adc #2
        sta rx_count
        lda #0
        sta RXBUF+1
        adc #0
        sta rx_count+1
        lda #$30
        sta status0
        sta status1
        jsr acia_grant
        clc
        rts
