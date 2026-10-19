; ACIA receive reliability test (VICE / SwiftLink): the host sends a counting stream, the NMI poller
; stores it in a 2 KB ring, the main loop verifies it.  Variables are read through the VICE monitor.
ACIA_DATA = $DE00
ACIA_STAT = $DE01
ACIA_CMD  = $DE02
ACIA_CTRL = $DE03
CIA2_TALO = $DD04
CIA2_TAHI = $DD05
CIA2_ICR  = $DD0D
CIA2_CRA  = $DD0E
RING      = $4000        ; 2 KB: $4000-$47FF
.segment "LOADADDR"
        .word $0801
.segment "STUB"
        .word @next
        .word 10
        .byte $9E, "2061", 0
@next:  .word 0
.segment "ZEROPAGE"
rd:     .res 2
.segment "BSS"
total:   .res 3
errors:  .res 1
dups:    .res 1
lost:    .res 1
ovr:     .res 1
phantom: .res 1
expect:  .res 1
nmis:    .res 2
tmo: .res 2
txfail: .res 1
txcnt: .res 1
firstbad: .res 1
firstexp: .res 1
firstpos: .res 2
.segment "CODE"
start:  sei
        lda #$35
        sta $01
        lda #<nmi
        sta $FFFA
        lda #>nmi
        sta $FFFB
        lda #0
        ldx #0
@z:     sta total,x
        inx
        cpx #20
        bne @z
        sta ACIA_STAT          ; programmed reset
        lda #$1E                ; 19200 (SwiftLink x2)
        sta ACIA_CTRL
        lda #$0B
        sta ACIA_CMD
        lda ACIA_STAT
        lda ACIA_DATA
        lda #<RING
        sta rd
        lda #>RING
        sta rd+1
        lda #0
        sta expect
        lda #24
        sta CIA2_TALO
        lda #1
        sta CIA2_TAHI
        lda CIA2_ICR
        lda #$81
        sta CIA2_ICR
        lda #$11
        sta CIA2_CRA
        ; VIC stress: 8 sprites on the same lines (worst-case DMA), sprite data pointers -> $2000
        ldx #0
@sp:    txa
        asl
        tay
        lda #(24+40)
        sta $D000,y
        lda #100
        sta $D001,y
        lda #$80
        sta $07F8,x
        inx
        cpx #8
        bne @sp
        lda #$FF
        sta $D015
        lda #$7F
        sta $DC0D
        lda $DC0D
        cli
main:   lda st+2                ; consistent snapshot of the write pointer high byte
        cmp rd+1
        bne @avail
        lda st+1
        cmp rd
        beq main
@avail: ldy #0
        lda (rd),y
        cmp expect
        beq @ok
        inc errors
        ldx errors
        cpx #1
        bne @ok
        sta firstbad            ; remember the first anomaly
        lda expect
        sta firstexp
        lda total
        sta firstpos
        lda total+1
        sta firstpos+1
        lda (rd),y
@ok:    tax
        inx
        stx expect
        inc total
        bne @t
        inc total+1
        bne @t
        inc total+2
@t:     lda total               ; every 128 bytes: send a 2-byte "credit" like the player does
        and #$7F
        bne @nosend
        jsr send2
@nosend:
        inc rd
        bne main
        inc rd+1
        lda rd+1
        cmp #>(RING+$800)
        bne main
        lda #>RING
        sta rd+1
        jmp main

send2:  ldx #0
@n:     lda #0
        sta tmo
        sta tmo+1
@w:     lda ACIA_STAT
        and #$10
        bne @go
        inc tmo
        bne @w
        inc tmo+1
        bne @w
        inc txfail
        rts
@go:    lda #$0A
        sta ACIA_DATA
        inx
        cpx #2
        bne @n
        inc txcnt
        rts

nmi:    pha
        inc nmis
        bne nmi1
        inc nmis+1
nmi1:   .repeat 30
        nop
        .endrepeat
        lda CIA2_ICR
        lda ACIA_STAT
        and #$0C
        beq nmi_done
        cmp #$08
        beq nmi_rd
        cmp #$04
        bne nmi_both
        inc phantom             ; overrun flag without data-register-full
        jmp nmi_rd
nmi_both:  inc ovr                 ; overrun with data pending
nmi_rd:    lda ACIA_DATA
st:     sta RING
        inc st+1
        bne nmi_done
        inc st+2
        lda st+2
        cmp #>(RING+$800)
        bne nmi_done
        lda #>RING
        sta st+2
nmi_done:  pla
        rti
