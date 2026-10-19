; ---------------------------------------------------------------------------
; Ultimate Command Interface driver (network target $03)
;   command bytes in CMDBUF, length in uci_len
;   response data -> RXBUF (rx_count bytes), first two status chars -> status0/1
; ---------------------------------------------------------------------------
.segment "CODE"

; wait for the idle state; C=1 on timeout (after trying an ABORT once)
uci_wait_idle:
        lda #2
        sta kcnt
@again: ldx #0
        ldy #0
@l:     lda UCI_STAT
        and #$30
        beq @ok
        dex
        bne @l
        dey
        bne @l
        lda #$04                ; ABORT
        sta UCI_CTRL
        lda #$08                ; CLR_ERR
        sta UCI_CTRL
        dec kcnt
        bne @again
        sec
        rts
@ok:    clc
        rts

; execute CMDBUF[0..uci_len-1]; C=1 on failure
uci_exec:
        jsr uci_wait_idle
        bcc @go
        rts
@go:    ldx #0
@send:  lda CMDBUF,x
        sta UCI_CMD
        inx
        cpx uci_len
        bne @send
        lda #$01                ; PUSH_CMD
        sta UCI_CTRL

        lda #0
        sta cnt
        sta cnt+1
@w:     lda UCI_STAT
        and #$20                ; data states have bit 5 set
        bne @got
        dec cnt
        bne @w
        dec cnt+1
        bne @w
        lda #$04
        sta UCI_CTRL
        sec
        rts

@got:   lda #<RXBUF
        sta dst
        lda #>RXBUF
        sta dst+1
        lda #0
        sta rx_count
        sta rx_count+1
@rd:    lda UCI_STAT
        bpl @rdone              ; bit 7 = DATA_AV
        lda UCI_DATA
        ldx rx_count+1
        cpx #4
        bcs @rd                 ; staging buffer full: drop the rest
        ldy #0
        sta (dst),y
        inc dst
        bne @n1
        inc dst+1
@n1:    inc rx_count
        bne @rd
        inc rx_count+1
        jmp @rd

@rdone: lda #0
        sta status0
        sta status1
        ldx #0
@rs:    lda UCI_STAT
        and #$40                ; STAT_AV
        beq @sdone
        lda UCI_STDAT
        cpx #2
        bcs @rs
        sta status0,x
        inx
        jmp @rs

@sdone: lda #$02                ; DATA_ACC
        sta UCI_CTRL
        jsr uci_wait_idle
        rts
@fail:  sec
        rts

; read up to tmp2 bytes from the socket -> RXBUF = [len lo][len hi][data...]
uci_sock_read:
        lda #3
        sta CMDBUF
        lda #$10
        sta CMDBUF+1
        lda sock
        sta CMDBUF+2
        lda tmp2
        sta CMDBUF+3
        lda tmp2+1
        sta CMDBUF+4
        lda #5
        sta uci_len
        jmp uci_exec

; send A payload bytes (already placed at CMDBUF+3...) to the bridge
net_send:
        ldx transport
        beq @uci
        jmp acia_send
@uci:   clc
        adc #3
        sta uci_len
        lda #3
        sta CMDBUF
        lda #$11
        sta CMDBUF+1
        lda sock
        sta CMDBUF+2
        jmp uci_exec

; read from whichever transport is active (same RXBUF contract for both)
sock_read:
        lda transport
        beq @u
        jmp acia_sock_read
@u:     jmp uci_sock_read
