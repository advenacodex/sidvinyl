; ---------------------------------------------------------------------------
; Boot: BASIC stub, hardware init, video standard detection
; ---------------------------------------------------------------------------
.segment "LOADADDR"
        .word $0801

.segment "BASIC"
        .word @next
        .word 10
        .byte $9E
        .byte "2061"
        .byte 0
@next:  .word 0

.segment "RODATA"
; patched by tools/patch_host.py -- keep the magic and the 40-byte field
host_magic:
        .byte $53,$49,$44,$48,$4F,$53,$54,$3D        ; "SIDHOST="
host_default:
        .byte "192.168.1.100:6464"
        .res 40 - 18, 0

.segment "CODE"
start:
        sei
        cld
        ldx #$FF
        txs

        ; copy character ROM set 2 (upper/lower case, normal glyphs) into RAM
        lda #$33
        sta $01
        lda #<$D800
        sta src
        lda #>$D800
        sta src+1
        lda #<FONT
        sta dst
        lda #>FONT
        sta dst+1
        ldx #4
        ldy #0
@cf:    lda (src),y
        sta (dst),y
        iny
        bne @cf
        inc src+1
        inc dst+1
        dex
        bne @cf
        lda #$35                ; all RAM except I/O
        sta $01

        ; clear zero page vars and BSS
        ldx #2
        lda #0
@zp:    sta $00,x
        inx
        cpx #$60
        bne @zp
        ldx #0
@bss:   sta $EF00,x
        inx
        bne @bss

        lda #0                  ; HOSTBUF is not part of BSS: RAM powers up with a pattern
        sta HOSTBUF

        ; vectors live in RAM now
        lda #<nmi_handler
        sta $FFFA
        lda #>nmi_handler
        sta $FFFB
        lda #<irq_handler
        sta $FFFE
        lda #>irq_handler
        sta $FFFF

        ; CIAs: no interrupts, keyboard matrix ready, VIC bank 0
        lda #$7F
        sta CIA1_ICR
        sta CIA2_ICR
        lda CIA1_ICR
        lda CIA2_ICR
        lda #$FF
        sta CIA1_DDRA
        sta CIA1_PRA
        lda #$00
        sta CIA1_DDRB
        lda CIA2_DDRA
        ora #$03
        sta CIA2_DDRA
        lda CIA2_PRA
        ora #$03
        sta CIA2_PRA

        ; silence SID
        ldx #$18
        lda #0
@sid:   sta SID,x
        dex
        bpl @sid

        ; video: text mode, black
        lda #$1B
        sta VIC_CTL1
        lda #$08
        sta VIC_CTL2
        lda #$17
        sta VIC_MEM
        lda #0
        sta VIC_BORDER
        sta VIC_BG0
        sta VIC_SPREN

        jsr detect_video

        lda #12
        sta vol_level
        lda #1
        sta skip_audio
        jsr audio_flush

        ; raster interrupt at line 250 (frame tick)
        lda #250
        sta VIC_RASTER
        lda #$01
        sta VIC_IRQ
        sta VIC_IRQEN
        jsr detect_transport
        cli
        jmp app_main

; ---------------------------------------------------------------------------
; detect_video: watch the highest raster line for a while. PAL reaches 311.
; ---------------------------------------------------------------------------
detect_video:
        lda #0
        sta tmp
        lda #<14000
        sta cnt
        lda #>14000
        sta cnt+1
@l:     lda VIC_CTL1
        bpl @n
        lda VIC_RASTER
        cmp tmp
        bcc @n
        sta tmp
@n:     dec cnt
        bne @l
        dec cnt+1
        bne @l
        lda tmp
        cmp #$20
        bcs @pal
        lda #1                  ; NTSC
        sta video
        lda #60
        sta fps
        rts
@pal:   lda #0
        sta video
        lda #50
        sta fps
        rts
