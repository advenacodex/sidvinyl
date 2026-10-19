"""SIDVinyl wire protocol (bridge <-> C64) and text helpers.

Transport: plain TCP (default port 6464). The C64 reaches it either through the
Ultimate Command Interface network target (UCI, pull model) or through a
SwiftLink-style ACIA serial port (VICE / real SwiftLink, 19200 baud). On the
serial transport the bridge is credit-flow-controlled: it may only send as many
bytes as the C64 has granted with CREDIT (units of CREDIT_UNIT bytes).

C64 -> bridge: fixed-length commands, first byte is the command id.
    HELLO  01 video(0=PAL,1=NTSC) version transport(0=UCI,1=serial)
    LIST   02 sort page
    PLAY   03 index seq
    NEXT   04 seq
    PREV   05 seq
    MODE   07 new_mode seq pos_lo pos_hi        (0=DIGI 1=SID, pos in seconds)
    VOL    08 delta(signed byte)
    STOP   09 seq
    CREDIT 0A n                      (serial only: bridge may send n*8 more bytes)

bridge -> C64: packets  [type][len_lo][len_hi][payload]
"""
import unicodedata

# ---- commands (C64 -> bridge)
CMD_HELLO = 0x01
CMD_LIST = 0x02
CMD_PLAY = 0x03
CMD_NEXT = 0x04
CMD_PREV = 0x05
CMD_MODE = 0x07
CMD_VOL = 0x08
CMD_STOP = 0x09
CMD_CREDIT = 0x0A

CMD_LEN = {  # total length including the id byte
    CMD_HELLO: 4, CMD_LIST: 3, CMD_PLAY: 3, CMD_NEXT: 2,
    CMD_PREV: 2, CMD_MODE: 5, CMD_VOL: 2, CMD_STOP: 2, CMD_CREDIT: 2,
}

# ---- packets (bridge -> C64)
PKT_HELLO = 0x01
PKT_LIST = 0x02
PKT_TRACK = 0x03
PKT_COVER = 0x04
PKT_DIGI = 0x05
PKT_SID = 0x06
PKT_STOP = 0x07
PKT_ERROR = 0x08
PKT_VOL = 0x09
PKT_DISC = 0x0A         # disc graphics + tonearm sprites, sent once after HELLO

PROTOCOL_VERSION = 2
CREDIT_UNIT = 8
TRANSPORT_UCI = 0
TRANSPORT_SERIAL = 1
LIST_PAGE = 20          # albums per page
LIST_ENTRY = 38         # bytes per list record (screen codes)
STR_FIELD = 64          # [len][63 chars] per string in PKT_TRACK
SID_FRAME = 22          # $D400-$D414 (21 bytes) + $D418
DIGI_RATES = (8000, 3500)  # nominal digi sample rate per HELLO reply 'digi_sel' (UCI, serial)
DIGI_RATE = DIGI_RATES[0]
COVER_FRAMES = 32
COVER_FRAME_BYTES = 640  # 512 bitmap + 64 screen + 64 colour

MODE_DIGI = 0
MODE_SID = 1

SORTS = ["alphabeticalByName", "alphabeticalByArtist", "newest", "frequent", "random", "starred"]
SORT_NAMES = ["A-Z ALBUM", "A-Z ARTIST", "NEWEST", "MOST PLAYED", "RANDOM", "STARRED"]

_TRANSLIT = {"{": "(", "}": ")", "|": "!", "~": "-", "`": "'", "’": "'", "‘": "'",
             "“": '"', "”": '"', "–": "-", "—": "-", "…": "...",
             "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
             "¿": "?", "¡": "!", "·": "-", "•": "*"}


def ascii_clean(text):
    """Strip accents and map unsupported characters to something the C64 can show."""
    out = []
    for ch in str(text):
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
            continue
        if ord(ch) < 128:
            out.append(ch if ch >= " " else " ")
            continue
        base = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in base if ord(c) < 128 and not unicodedata.combining(c))
        out.append(base if base else "?")
    return "".join(out)


def screen_code(ch):
    """ASCII char -> C64 screen code for the upper/lower-case character set."""
    o = ord(ch)
    if 0x61 <= o <= 0x7A:          # a-z  -> 1..26
        return o - 0x60
    if 0x41 <= o <= 0x5A:          # A-Z  -> 65..90
        return o
    if o == 0x40:                  # @
        return 0x00
    if o == 0x5B:                  # [
        return 0x1B
    if o == 0x5D:                  # ]
        return 0x1D
    if o == 0x5C:                  # backslash -> pound sign
        return 0x1C
    if o == 0x5E:                  # ^ -> up arrow
        return 0x1E
    if o == 0x5F:                  # _ -> left arrow
        return 0x1F
    if 0x20 <= o <= 0x3F:
        return o
    return 0x3F                    # '?'


def to_screen(text, width=None, pad=True):
    codes = [screen_code(c) for c in ascii_clean(text)]
    if width is not None:
        codes = codes[:width]
        if pad:
            codes += [0x20] * (width - len(codes))
    return bytes(codes)


def str_field(text):
    """[len][63 screen codes] fixed 64-byte field used by PKT_TRACK."""
    codes = to_screen(text, STR_FIELD - 1, pad=False)
    return bytes([len(codes)]) + codes + bytes(STR_FIELD - 1 - len(codes))


def packet(ptype, payload=b""):
    n = len(payload)
    if n > 0xFFFF:
        raise ValueError("payload too large")
    return bytes([ptype, n & 0xFF, n >> 8]) + payload
