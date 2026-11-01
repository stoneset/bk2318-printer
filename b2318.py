#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
b2318.py - full driver for the Brüel & Kjær Type 2318 thermal graphics printer
fitted with the ZI 0054 serial interface module.

Every command from the instruction manual (June 1988):
  - text: double width, inverse video, left/right alignment
  - character sets: G0-G3 designation, SI/SO/SS2/SS3/LS2/LS3,
    the 11 documented sets + the hidden rune set (firmware VP1915)
  - dynamically redefinable characters (DRC), 7x7 dots
  - PDI graphics set: cursor, lines, bars, dot columns
  - EOT (wait for end of printing), NSR (reset)
High-level features: Unicode text with automatic character-set switching,
characters missing from the built-in sets (ê, â, €...) drawn as DRC, images,
bar charts, strip-chart recorder, character-set tables, demo page,
calibration, cable diagnostic, Tkinter GUI.

Install: python -m pip install -r requirements.txt   (pyserial; pillow is optional)

Examples:
  python b2318.py                              graphical interface
  python b2318.py ports                        list serial ports
  python b2318.py -p COM3 text "Hello, it works"
  python b2318.py -p COM3 text --wide --inverse --align center "TITLE"
  echo "text" | python b2318.py -p /dev/ttyUSB0 text -
  python b2318.py -p COM3 demo                 test page showing every feature
  python b2318.py -p COM3 sample --all         tables of every character set
  python b2318.py -p COM3 image photo.png
  python b2318.py -p COM3 bars "63 Hz=72.5" "125 Hz=68" "250 Hz=61"
  python b2318.py -p COM3 strip readings.txt
  python b2318.py -p COM3 calibrate
  python b2318.py -p COM3 diag
  python b2318.py -p socket://192.168.4.1:2323 text "over Wi-Fi"   (serial-to-TCP bridge)
  python b2318.py --dry-run text "test"        show the bytes, print nothing
  python b2318.py config port COM3             remember the port

Settings are stored in bk2318-printer.json, next to the program.
Reference: B&K 2318 + ZI 0054 instruction manual,
https://archive.org/details/b-k-2318-zi-0054-instruction-manual
Points not yet verified on a real printer are marked ASSUMPTION.
"""
import argparse
import bisect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

__version__ = "1.12"
APP_NAME = "bk2318-printer"
PROJECT_URL = "https://github.com/stoneset/bk2318-printer"     # repository address (About box)

FROZEN = getattr(sys, "frozen", False)                       # running as a PyInstaller .exe
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))         # files packed inside the .exe

# =============================================================================
# Protocol (numbers refer to sections of the manual)
# =============================================================================

# C0 control codes (5.1, table 5.2)
EOT, LF, FF, CR = 0x04, 0x0A, 0x0C, 0x0D
SO, SI = 0x0E, 0x0F            # G1 / G0 become the active table (GL)
SS2, SS3 = 0x19, 0x1D          # single character taken from G2 / G3
ESC, NSR = 0x1B, 0x1F          # escape / full reset (power-up state)
XON, XOFF = 0x11, 0x13         # sent by the printer (flow control)

# ESC sequences (5.2.2 to 5.2.5)
VIDEO_INVERSE = b"\x1bH"       # VDOR: white on black
VIDEO_NORMAL = b"\x1bI"        # VDON
TEXT_WIDE = b"\x1bK"           # double width, 12 characters per line
TEXT_NORMAL = b"\x1bL"         # TXTN, 24 characters per line
ALIGN_LEFT = b"\x1b&<"         # TXTL
ALIGN_RIGHT = b"\x1b&>"        # TXTR (not usable with the graphics set)
DEFDRC = b"\x1bC"              # start of a DRC character definition
ENDDRC = b"\x1bE"              # end of a DRC definition
LS2, LS3 = b"\x1bn", b"\x1bo"  # G2 / G3 -> GL, locking (back with SI)
LS1R, LS2R, LS3R = b"\x1b~", b"\x1b}", b"\x1b|"   # G1/G2/G3 -> GR (8-bit mode)

# Designating a set into a G table: ESC <table> <set> (5.2.4)
DESIGNATE = {("N", 0): 0x28, ("N", 1): 0x29, ("N", 2): 0x2A, ("N", 3): 0x2B,
             ("S", 1): 0x2D, ("S", 2): 0x2E, ("S", 3): 0x2F}

CHARSETS = {  # name: (final byte, Normal/Special type, description)
    "us": (0x42, "N", "ISO 7-bit (US ASCII)"),
    "uk": (0x43, "N", "English"),
    "de": (0x44, "N", "German"),
    "fr": (0x45, "N", "French"),
    "es": (0x46, "N", "Spanish"),
    "it": (0x47, "N", "Italian"),
    "se": (0x48, "N", "Swedish"),
    "dk": (0x49, "N", "Danish/Norwegian"),
    "kana": (0x4A, "N", "Japanese (katakana)"),
    "gr": (0x4B, "N", "Greek"),
    "ru": (0x4C, "N", "Russian (KOI-7)"),
    "runes": (0x4D, "N", "Runes (hidden, firmware VP1915)"),
    "pdi": (0x57, "S", "PDI graphics"),
    "drc": (0x7B, "S", "DRC redefinable characters"),
}
TEXT_SETS = [k for k, v in CHARSETS.items() if v[1] == "N"]
POWER_UP_G = {0: "us", 1: "pdi", 2: "gr", 3: "drc"}        # 5.1.2

# PDI graphics set (5.2.6 and 5.2.7), active after SO
PDI_COS = 0x20          # cursor origin = current position (clamped to 3..164)
PDI_RIGHT = 0x24        # + (0x40+n): move right n dots (1..63)
PDI_LEFT = 0x25         # + (0x40+n): move left n dots
PDI_MOVE_REL = 0x26     # + length: cursor at a relative distance from the origin
PDI_LINE = 0x28         # + type/row (+ length for a partial line)
PDI_CHARS = 0x29        # + (0x40+n): move right n character positions (1..23)
PDI_DOTS = 0x2A         # + 2 bytes: one column of 7 dots
PDI_BAR = {("wide", "filled"): 0x30, ("narrow", "filled"): 0x31,
           ("wide", "hatched"): 0x32, ("narrow", "hatched"): 0x33,
           ("wide", "outline"): 0x34, ("narrow", "outline"): 0x35}
# Other PDI codes from the table in the appendix (p. 64), read by saschapo:
# 0x21 COT, 0x22 CEL, 0x23 CER, 0x27 SCLE (scale), 0x2F BK. Their parameters are not
# described in the text; Printer.pdi_raw() lets you experiment.
DEFAULT_SCALE_DOTS = 162  # relative length 1.0 at power-up: 165 - origin (3) dots
DEFAULT_ASPECT = 1.95     # line pitch 3.94 mm / printed height 2.0 mm (measured by saschapo)
TITLE_STYLES = ("wide-inverse", "inverse", "wide", "framed")
LINE_TYPES = {"full": 0, "dotted": 1, "dashed": 2, "dotdash": 3}
DOTS = 168              # dots per line
CHAR_DOTS = 7           # width of a normal character, in dots
LINES_PER_SECOND = 0.8
BAUDS = ["300", "600", "1200", "2400", "4800", "9600", "19200"]

# =============================================================================
# Character tables
# =============================================================================

# ASSUMPTION: ISO 646 national variants (the manual refers to ISO 646-1983).
# Check with "sample --charset fr" (etc.) and fix here if needed.
ISO646 = {
    "uk": {"£": 0x23},
    "de": {"§": 0x40, "Ä": 0x5B, "Ö": 0x5C, "Ü": 0x5D, "ä": 0x7B, "ö": 0x7C, "ü": 0x7D, "ß": 0x7E},
    "fr": {"£": 0x23, "à": 0x40, "°": 0x5B, "ç": 0x5C, "§": 0x5D, "é": 0x7B, "ù": 0x7C, "è": 0x7D},
    "es": {"£": 0x23, "§": 0x40, "¡": 0x5B, "Ñ": 0x5C, "¿": 0x5D, "ñ": 0x7C, "ç": 0x7D},
    "it": {"£": 0x23, "§": 0x40, "°": 0x5B, "ç": 0x5C, "é": 0x5D, "ù": 0x60, "à": 0x7B,
           "ò": 0x7C, "è": 0x7D, "ì": 0x7E},
    "se": {"Ä": 0x5B, "Ö": 0x5C, "Å": 0x5D, "ä": 0x7B, "ö": 0x7C, "å": 0x7D},
    "dk": {"Æ": 0x5B, "Ø": 0x5C, "Å": 0x5D, "æ": 0x7B, "ø": 0x7C, "å": 0x7D},
}


def _build_tables():
    t = {k: dict(v) for k, v in ISO646.items()}
    # Russian KOI-7: upper-case Cyrillic in place of lower-case Latin,
    # no lower-case Cyrillic (seen on FrodeM's printout).
    ru = {}
    for i, c in enumerate("ЮАБЦДЕФГХИЙКЛМНОПЯРСТУЖВЬЫЗШЭЩЧ"):
        ru[c] = ru[c.lower()] = 0x60 + i
    ru["Ё"] = ru["ё"] = ru["Е"]
    t["ru"] = ru
    # ASSUMPTION: Greek per ELOT 927 (capitals in place of lower-case Latin)
    gr = {}
    for i, c in enumerate("ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"):
        gr[c] = gr[c.lower()] = 0x61 + i
    gr["ς"] = gr["Σ"]
    for cp in range(0x0386, 0x03D0):                  # accented letters
        ch = chr(cp)
        base = unicodedata.normalize("NFD", ch)[0].upper()
        if ch not in gr and base in gr:
            gr[ch] = gr[base]
    t["gr"] = gr
    # ASSUMPTION: JIS X 0201 katakana at 0x21-0x5F (half-width and full-width)
    kana = {}
    for cp in range(0xFF61, 0xFFA0):
        code = 0x21 + cp - 0xFF61
        kana[chr(cp)] = code
        full = unicodedata.normalize("NFKC", chr(cp))
        if len(full) == 1:
            kana.setdefault(full, code)
    kana["゛"], kana["゜"] = 0x5E, 0x5F
    t["kana"] = kana
    return t


ALT = _build_tables()
DEFAULT_PREFER = ["fr", "de", "es", "it", "se", "dk", "uk", "ru", "gr", "kana"]

# Replacements when a character exists in no set
FALLBACK = {"’": "'", "‘": "'", "‚": ",", "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
            "–": "-", "—": "-", "‐": "-", "−": "-", "…": "...", "œ": "oe", "Œ": "OE",
            "æ": "ae", "Æ": "AE", "ß": "ss", "€": "EUR", "×": "x", "÷": "/", "±": "+/-",
            "°": "o", "µ": "u", "\t": "    "}

# 7x7 glyphs drawn as DRC for characters missing from the built-in sets.
# 7 rows of 5 columns ("#" = dot); an empty column is added on each side.
BUILTIN_GLYPHS = {
    "ê": ["..#..", ".#.#.", ".###.", "#...#", "#####", "#....", ".###."],
    "â": ["..#..", ".#.#.", ".###.", "....#", ".####", "#...#", ".####"],
    "î": ["..#..", ".#.#.", ".....", ".##..", "..#..", "..#..", ".###."],
    "ô": ["..#..", ".#.#.", ".....", ".###.", "#...#", "#...#", ".###."],
    "û": ["..#..", ".#.#.", ".....", "#...#", "#...#", "#..##", ".##.#"],
    "ë": [".#.#.", ".....", ".###.", "#...#", "#####", "#....", ".###."],
    "ï": [".#.#.", ".....", ".##..", "..#..", "..#..", "..#..", ".###."],
    "€": ["..###", ".#...", "####.", ".#...", "####.", ".#...", "..###"],
}

SPACE = (0, 0x20, None, " ")


def parse_pattern(rows):
    """7 rows of 5 or 7 characters ("#" = dot) -> 7 column masks
    (bit 6 = top row, bit 0 = bottom row)."""
    rows = [str(r).replace(" ", ".") for r in rows]
    if len(rows) != 7:
        raise ValueError("a glyph has 7 rows")
    widths = {len(r) for r in rows}
    if widths == {5}:
        rows = ["." + r + "." for r in rows]
    elif widths != {7}:
        raise ValueError("all rows must be 5 or 7 characters long")
    return [sum((rows[r][c] not in "._0") << (6 - r) for r in range(7)) for c in range(7)]


def column_bytes(mask):
    """7-bit mask -> 2 bytes (format of 0x2A and DRC, manual 5.2.5).
    1st byte: the 6 upper dots, 2nd byte: the bottom dot (bit 5)."""
    return bytes([0x40 | ((mask >> 1) & 0x3F), 0x40 | ((mask & 1) << 5)])


def rel_length(x):
    """Relative length 0..1 -> 2 bytes: 12 bits, 6 per byte, bit 6 set.
    Manual example: 0x66 0x58 = 0.5996."""
    n = max(0, min(4095, round(x * 4096)))
    return bytes([0x40 | (n >> 6), 0x40 | (n & 0x3F)])


def _row(row):
    if not 1 <= int(row) <= 7:
        raise ValueError("row from 1 (bottom) to 7 (top)")
    return int(row)


def _bar_masks(length, size="wide", style="filled"):
    """Bar drawn dot by dot (same look as the native bars)."""
    top, bot = (1, 5) if size == "wide" else (2, 4)
    full = sum(1 << (6 - r) for r in range(top, bot + 1))
    edge = (1 << (6 - top)) | (1 << (6 - bot))
    masks = []
    for i in range(int(length)):
        if style == "filled" or i in (0, length - 1):
            masks.append(full)
        elif style == "outline":
            masks.append(edge)
        else:
            masks.append(edge | sum(1 << (6 - r) for r in range(top, bot + 1) if (r + i) % 3 == 0))
    return masks


def _is_number(s):
    try:
        float(str(s).replace(",", ".").rstrip("%").strip())
        return True
    except ValueError:
        return False


# =============================================================================
# Byte stream builder
# =============================================================================

class Printer:
    """Builds the byte stream for the 2318. Nothing is sent here:
    take() returns the bytes, to be transmitted with Link.send()."""

    def __init__(self, mode="auto", prefer=None, glyphs=None, scale_px=DEFAULT_SCALE_DOTS,
                 aspect=DEFAULT_ASPECT, title_style="wide-inverse"):
        self.mode = mode                      # "auto" (Unicode, set switching) or "ascii"
        self.prefer = [p for p in (prefer or DEFAULT_PREFER) if p in ALT]
        self.glyphs = dict(BUILTIN_GLYPHS)
        self.glyphs.update(glyphs or {})
        self.scale_px = scale_px              # dots for a relative length of 1.0
        self.aspect = aspect                  # line pitch / printed height (images)
        self.title_style = title_style
        self.buf = bytearray()
        self._power_up_state()

    def _power_up_state(self):
        self.g = dict(POWER_UP_G)
        self.gl = 0
        self.is_wide = False
        self.is_inverse = False
        self.alignment = "left"
        self.native_right = False
        self.raw_set = None
        self.drc_codes = {}                   # character -> defined DRC code
        self._free_drc = list(range(0x7E, 0x20, -1))

    # ---------- low level ----------
    def _w(self, *parts):
        for p in parts:
            if isinstance(p, int):
                self.buf.append(p)
            else:
                self.buf += p
        return self

    def take(self):
        """Return the bytes built so far and empty the buffer."""
        data = bytes(self.buf)
        self.buf.clear()
        return data

    def raw(self, data):
        """Append raw bytes."""
        return self._w(bytes(data))

    SOFT_RESET = (b"\x0f"                                     # SI: G0 active
                  b"\x1b(B\x1b-W\x1b*K\x1b/{"                  # power-up G0, G1, G2, G3
                  b"\x1bL\x1bI\x1b&<")                         # normal width, video, left

    def soft_reset(self):
        """Back to the power-up character sets and styles without NSR. Safe while
        the printer is still printing: NSR would also clear its line buffer."""
        self._w(self.SOFT_RESET)
        self._power_up_state()
        return self

    def reset(self):
        """NSR: back to the power-up state (sets, styles, DRC depending on N2-2)."""
        self._w(NSR)
        self._power_up_state()
        return self

    def eot(self):
        """EOT: holds the sender (handshake) until printing is finished.
        Only active when DIP switch N1-6 is OFF."""
        return self._w(EOT)

    def newline(self, n=1):
        """CR: prints the current line and feeds one line (n times)."""
        return self._w(bytes([CR]) * int(n))

    feed = newline

    # ---------- character sets ----------
    def designate(self, table, charset):
        """Put a set into table G0..G3 (ESC <table> <set>)."""
        code, kind, _ = CHARSETS[charset]
        if (kind, table) not in DESIGNATE:
            raise ValueError(f"set {charset} cannot go into G{table}")
        self._w(ESC, DESIGNATE[(kind, table)], code)
        self.g[table] = charset
        return self

    def invoke(self, table):
        """Make table G0..G3 active: SI, SO, LS2 or LS3."""
        if self.gl != table:
            self._w({0: bytes([SI]), 1: bytes([SO]), 2: LS2, 3: LS3}[table])
            self.gl = table
        return self

    def _need(self, table, charset):
        if self.g[table] != charset:
            self.designate(table, charset)

    def raw_charset(self, charset=None):
        """Put a set into G0 and send the text as is (ASCII codes).
        E.g. raw_charset("runes") then text("ABC"). None = back to normal mode."""
        self._need(0, charset or "us")
        self.raw_set = charset if charset not in (None, "us") else None
        return self

    # ---------- styles ----------
    @property
    def width(self):
        return 12 if self.is_wide else 24

    def wide(self, on=True):
        self._w(TEXT_WIDE if on else TEXT_NORMAL)
        self.is_wide = bool(on)
        return self

    def inverse(self, on=True):
        self._w(VIDEO_INVERSE if on else VIDEO_NORMAL)
        self.is_inverse = bool(on)
        return self

    def align(self, how="left"):
        """left, right (native) or center (emulated with spaces)."""
        if how not in ("left", "right", "center"):
            raise ValueError("alignment: left, right or center")
        self.alignment = how
        self._native_align(how == "right")
        return self

    def _native_align(self, right):
        if right != self.native_right:
            self._w(ALIGN_RIGHT if right else ALIGN_LEFT)
            self.native_right = right

    # ---------- text encoding ----------
    def encode(self, text):
        """Unicode text -> glyphs (table, code, set, original character)."""
        out = []
        for ch in str(text):
            if "ぁ" <= ch <= "ゖ":          # hiragana -> katakana
                ch = chr(ord(ch) + 0x60)
            out += self._glyph(ch)
        return out

    def _glyph(self, ch):
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            return [(0, o, None, ch)]
        if self.raw_set or self.mode == "ascii":
            return self._fallback(ch)
        if ch in self.drc_codes:
            return [(3, self.drc_codes[ch], "drc", ch)]
        for name in self.prefer:
            code = ALT[name].get(ch)
            if code:
                return [(2, code, name, ch)]
        if "kana" in self.prefer and "゠" <= ch <= "ヿ":     # ガ -> カ + ゛
            parts = unicodedata.normalize("NFD", ch)
            if len(parts) > 1 and all(p in ALT["kana"] for p in parts):
                return [(2, ALT["kana"][p], "kana", p) for p in parts]
        if ch in self.glyphs:
            return [(3, self.define_char(ch, self.glyphs[ch]), "drc", ch)]
        return self._fallback(ch)

    @staticmethod
    def _fallback(ch):
        rep = FALLBACK.get(ch)
        if rep is None:
            if unicodedata.category(ch).startswith("C"):    # control character
                return []
            rep = "".join(c for c in unicodedata.normalize("NFKD", ch) if 0x20 <= ord(c) <= 0x7E)
            rep = rep or "?"
        return [(0, ord(c), None, c) for c in rep]

    def define_char(self, char_or_code, pattern):
        """Define a 7x7 DRC character (DEFDRC ... ENDDRC) and return its code.
        With a Unicode character, text() will then use it automatically.
        pattern: 7 rows of 5 or 7 characters ("#" = dot), or 7 masks."""
        if isinstance(char_or_code, int):
            code, ch = char_or_code, None
            if code in self._free_drc:
                self._free_drc.remove(code)
        else:
            ch = char_or_code
            if ch in self.drc_codes:
                code = self.drc_codes[ch]
            elif self._free_drc:
                code = self._free_drc.pop(0)
            else:
                raise RuntimeError("DRC table full")
        cols = list(pattern) if isinstance(pattern[0], int) else parse_pattern(pattern)
        self._w(DEFDRC, code)
        for m in cols:
            self._w(column_bytes(m))
        self._w(ENDDRC)
        if ch:
            self.drc_codes[ch] = code
        return code

    def _emit(self, glyphs):
        i, n = 0, len(glyphs)
        while i < n:
            table, code, name, _ = glyphs[i]
            if table == 0:
                self.invoke(0)
                self._w(code)
                i += 1
                continue
            self._need(table, name)
            j = i
            while j < n and glyphs[j][0] == table and glyphs[j][2] == name:
                j += 1
            if j - i > 1 or self.gl == table:        # several: locking shift
                self.invoke(table)
                self._w(bytes(g[1] for g in glyphs[i:j]))
            else:                                    # just one: single shift
                self._w(SS2 if table == 2 else SS3, code)
            i = j

    @staticmethod
    def _wrap(glyphs, width):
        words, cur = [], []
        for g in glyphs:
            if g[0] == 0 and g[1] == 0x20:
                words.append(cur)
                cur = []
            else:
                cur.append(g)
        words.append(cur)
        lines, line = [], []
        for w in words:
            while len(w) > width:
                if line:
                    lines.append(line)
                    line = []
                lines.append(w[:width])
                w = w[width:]
            sep = [SPACE] if line else []
            if len(line) + len(sep) + len(w) <= width:
                line += sep + w
            else:
                lines.append(line)
                line = list(w)
        lines.append(line)
        return lines

    # ---------- text ----------
    def write(self, text):
        """Write without line break or wrapping."""
        if self.alignment == "right":
            self._native_align(True)
        self._emit(self.encode(text))
        return self

    def _line_glyphs(self, glyphs):
        if self.alignment == "right":
            self._native_align(True)
        if self.alignment == "center":
            pad = max(0, self.width - len(glyphs))
            glyphs = [SPACE] * (pad // 2) + glyphs
            if self.is_inverse:                       # full band in inverse video
                glyphs += [SPACE] * (pad - pad // 2)
        self._emit(glyphs)
        self.newline()

    def line(self, text="", wrap=True):
        """One line of text, wrapped at the current width (24 or 12)."""
        glyphs = self.encode(text)
        rows = self._wrap(glyphs, self.width) if wrap else [glyphs[:self.width]]
        for row in rows:
            self._line_glyphs(row)
        return self

    def text(self, text):
        """Multi-line text."""
        for para in str(text).replace("\r\n", "\n").split("\n"):
            self.line(para)
        return self

    def preview(self, text):
        """Lines as they will be printed (for an on-screen preview)."""
        out, w = [], self.width
        for para in str(text).replace("\r\n", "\n").split("\n"):
            for row in self._wrap(self.encode(para), w):
                s = "".join(g[3] for g in row)
                if self.alignment == "center":
                    s = " " * ((w - len(row)) // 2) + s
                elif self.alignment == "right":
                    s = " " * (w - len(row)) + s
                out.append(s)
        return out

    def title(self, text, style=None):
        """Centered banner. Styles: wide-inverse (default), inverse (normal width,
        easier to read), wide (no inverse), framed (wide text between two hatched
        bars; inverse video does not apply to graphics)."""
        style = style or self.title_style
        if style not in TITLE_STYLES:
            raise ValueError(f"banner style: {', '.join(TITLE_STYLES)}")
        state = (self.is_wide, self.is_inverse, self.alignment)
        if style == "framed":
            self.band(_bar_masks(DOTS, "wide", "hatched"))
        self.wide(style != "inverse").inverse(style in ("wide-inverse", "inverse"))
        self.align("center").line(text)
        if style == "framed":
            self.band(_bar_masks(DOTS, "wide", "hatched"))
        self.wide(state[0]).inverse(state[1]).align(state[2])
        return self

    def kv(self, left, right, fill=" "):
        """Label on the left, value on the right of the same line."""
        r = self.encode(right)[:self.width]
        lft = self.encode(left)[:max(0, self.width - len(r) - 1)]
        f = (self.encode(fill) or [SPACE])[:1]
        self._line_glyphs(lft + f * (self.width - len(lft) - len(r)) + r)
        return self

    def table(self, rows, sep=" "):
        """Table fitted to the width, numeric columns right-aligned."""
        rows = [[str(c) for c in r] for r in rows]
        ncol = max(len(r) for r in rows)
        rows = [r + [""] * (ncol - len(r)) for r in rows]
        enc = [[self.encode(c) for c in r] for r in rows]
        widths = [max(len(r[i]) for r in enc) for i in range(ncol)]
        room = self.width - (ncol - 1) * len(sep)
        while sum(widths) > room and max(widths) > 1:
            widths[widths.index(max(widths))] -= 1
        body = rows[1:] or rows
        numeric = [all(_is_number(r[i]) for r in body if r[i]) for i in range(ncol)]
        s = self.encode(sep)
        for r in enc:
            out = []
            for i, cell in enumerate(r):
                cell = cell[:widths[i]]
                pad = [SPACE] * (widths[i] - len(cell))
                out += pad + cell if numeric[i] else cell + pad
                if i < ncol - 1:
                    out += s
            self._line_glyphs(out)
        return self

    # ---------- PDI graphics ----------
    def _gfx(self):
        self._native_align(False)
        self._need(1, "pdi")
        self.invoke(1)
        return self

    def pdi_raw(self, opcode, payload=b""):
        """Raw graphics command, e.g. to test 0x21 and 0x22."""
        self._gfx()
        return self._w(opcode, bytes(payload))

    def origin(self):
        """COS: the current position becomes the origin (clamped to 3..164 dots)."""
        self._gfx()
        return self._w(PDI_COS)

    def move(self, dots):
        """Move the cursor by n dots (negative = to the left)."""
        self._gfx()
        op = PDI_RIGHT if dots >= 0 else PDI_LEFT
        n = abs(int(dots))
        while n > 0:
            k = min(63, n)
            self._w(op, 0x40 + k)
            n -= k
        return self

    def move_rel(self, x):
        """Put the cursor at a relative length x (0..1) from the origin."""
        self._gfx()
        return self._w(PDI_MOVE_REL, rel_length(x))

    def skip_chars(self, n):
        """Move right by n character positions."""
        self._gfx()
        n = int(n)
        while n > 0:
            k = min(23, n)
            self._w(PDI_CHARS, 0x40 + k)
            n -= k
        return self

    def hline(self, x, kind="full", row=4):
        """Line of relative length x on row 1 (bottom) to 7 (top).
        kind: full, dotted, dashed, dotdash."""
        self._gfx()
        return self._w(PDI_LINE, 0x40 | (LINE_TYPES[kind] << 3) | _row(row), rel_length(x))

    def rule(self, kind="dashed", row=4, newline=True):
        """Line across the whole paper width."""
        self._gfx()
        self._w(PDI_LINE, 0x60 | (LINE_TYPES[kind] << 3) | _row(row))
        if newline:
            self.newline()
        return self

    def dots(self, *masks):
        """Columns of 7 dots (bit 6 = top); the cursor moves one dot per column."""
        self._gfx()
        for m in masks:
            self._w(PDI_DOTS, column_bytes(m))
        return self

    def bar(self, x, size="wide", style="filled"):
        """Native bar of relative length x from the cursor.
        size: wide/narrow, style: filled/hatched/outline."""
        self._gfx()
        return self._w(PDI_BAR[(size, style)], rel_length(x))

    def bar_dots(self, length, size="wide", style="filled"):
        """Bar drawn dot by dot, exact length in dots."""
        return self.dots(*_bar_masks(length, size, style))

    def band(self, masks, newline=True):
        """One graphics line, dot by dot (168 columns max).
        Empty stretches are skipped with cursor moves."""
        masks = [int(m) & 0x7F for m in masks][:DOTS]
        while masks and not masks[-1]:
            masks.pop()
        self._gfx()
        i = 0
        while i < len(masks):
            if masks[i] == 0:
                j = i
                while j < len(masks) and masks[j] == 0:
                    j += 1
                self.move(j - i)
                i = j
            else:
                self._w(PDI_DOTS, column_bytes(masks[i]))
                i += 1
        if newline:
            self.newline()
        return self

    # ---------- images ----------
    def prepare_image(self, source, width=DOTS, dither=True, threshold=128,
                      invert=False, rotate=False, aspect=None):
        """Image -> 1-bit image ready to print (black = dot).
        aspect compensates for the white gap between two print lines."""
        from PIL import Image, ImageOps
        im = source if isinstance(source, Image.Image) else Image.open(source)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P", "PA"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, "white")
            bg.alpha_composite(im)
            im = bg
        im = im.convert("L")
        if rotate:
            im = im.rotate(90, expand=True)
        w = max(1, min(int(width), DOTS))
        a = self.aspect if aspect is None else aspect
        h = max(1, round(im.height * w / im.width / max(a, 0.1)))
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        im = im.resize((w, h), resample)
        if invert:
            im = ImageOps.invert(im)
        if dither:
            return im.convert("1")
        nodither = getattr(getattr(Image, "Dither", Image), "NONE")
        return im.point(lambda v: 255 if v >= threshold else 0).convert("1", dither=nodither)

    def bitmap(self, img, center=True):
        """1-bit image (PIL) -> bands of 7 dots."""
        w, h = img.size
        px = img.load()
        off = (DOTS - w) // 2 if center and w < DOTS else 0
        for top in range(0, h, 7):
            masks = [0] * off
            for x in range(min(w, DOTS - off)):
                m = 0
                for r in range(7):
                    if top + r < h and not px[x, top + r]:
                        m |= 1 << (6 - r)
                masks.append(m)
            self.band(masks)
        return self

    def image(self, source, **kw):
        """Print an image (file or PIL object). See prepare_image()."""
        center = kw.pop("center", True)
        return self.bitmap(self.prepare_image(source, **kw), center=center)

    # ---------- measurement charts ----------
    def bar_chart(self, items, vmin=None, vmax=None, unit="", size="wide",
                  style="filled", native=None, decimals=None, axis=True):
        """Bar chart: one bar per line with label and value.
        items: list of (label, value). native=None: native bars if scale_px
        is calibrated, otherwise bars drawn dot by dot."""
        labels = [str(lab) for lab, _ in items]
        values = [float(str(v).replace(",", ".")) for _, v in items]
        if not values:
            return self
        lo = (0.0 if min(values) >= 0 else min(values)) if vmin is None else float(vmin)
        hi = max(values) if vmax is None else float(vmax)
        if lo > hi:                                   # min and max entered the wrong way round
            lo, hi = hi, lo
        if hi == lo:
            hi = lo + 1
        if decimals is None:
            decimals = 0 if all(v == int(v) for v in values) else 1
        nums = [f"{v:.{decimals}f}" for v in values]
        lw = min(max(len(self.encode(lab)) for lab in labels), 8)
        ulen = len(self.encode(unit)) if unit else 0
        # unit on every line only if it does not truncate the labels
        unit_on_rows = bool(unit) and lw + max(len(n) for n in nums) + ulen + 2 <= 14
        vals = [n + unit for n in nums] if unit_on_rows else nums
        vw = max(len(self.encode(v)) for v in vals)
        while lw + vw + 2 > 14 and lw > 3:
            lw -= 1
        chars = lw + vw + 2
        x0, avail = chars * CHAR_DOTS, DOTS - chars * CHAR_DOTS
        use_native = bool(self.scale_px) if native is None else native
        state = (self.is_wide, self.alignment)
        if self.is_wide:
            self.wide(False)
        self.align("left")
        for lab, v, txt in zip(labels, values, vals):
            g = self.encode(lab)[:lw]
            g = g + [SPACE] * (lw - len(g) + 1)
            t = self.encode(txt)
            g += [SPACE] * (vw - len(t)) + t + [SPACE]
            self._emit(g)
            px = round(avail * (min(max(v, lo), hi) - lo) / (hi - lo))
            if px > 0:
                if use_native and self.scale_px and px / self.scale_px < 1:
                    self.bar(px / self.scale_px, size, style)
                else:
                    self.bar_dots(px, size, style)
            self.newline()
        if axis:
            masks = [0] * DOTS
            for c in range(x0, DOTS):
                masks[c] = 0b0000010
            for k in range(5):
                masks[min(DOTS - 1, x0 + round(k * (avail - 1) / 4))] = 0b0011110
            self.band(masks)
            # scale: min under the start of the axis, max right-aligned,
            # unit in the middle when it is not on the lines
            a, b = f"{lo:.{decimals}f}", f"{hi:.{decimals}f}"
            u = "" if unit_on_rows else unit
            mid = 24 - chars - len(a) - len(b)
            if u and mid >= ulen + 2:
                left = (mid - ulen) // 2
                axis_txt = " " * chars + a + " " * left + u + " " * (mid - ulen - left) + b
            elif mid >= 1:
                axis_txt = " " * chars + a + " " * mid + b
            else:
                axis_txt = (a + " " + b).rjust(24)
            self.line(axis_txt[-24:] if len(axis_txt) > 24 else axis_txt, wrap=False)
            if u and not (mid >= ulen + 2):
                self.line(u.rjust(24), wrap=False)
        if state[0]:
            self.wide(True)
        self.align(state[1])
        return self

    def strip_point(self, value, vmin, vmax, prev=None, index=0, grid=True, grid_every=10):
        """One strip-chart recorder line: time runs down the paper,
        the value goes across it. Returns the value (for prev)."""
        def xpos(v):
            r = (min(max(float(v), vmin), vmax) - vmin) / ((vmax - vmin) or 1)
            return round(r * (DOTS - 1))
        masks = [0] * DOTS
        if grid:
            for k in range(5):
                masks[round(k * (DOTS - 1) / 4)] |= 0b0001000
            if index % grid_every == 0:
                for c in range(0, DOTS, 4):
                    masks[c] |= 0b0001000
        x = xpos(value)
        last = start = xpos(prev) if prev is not None else x
        for r in range(7):                            # trace diagonally within the line
            xr = round(start + (x - start) * (r + 1) / 7)
            for c in range(min(last, xr), max(last, xr) + 1):
                masks[c] |= 1 << (6 - r)
            last = xr
        self.band(masks)
        return value

    # ---------- tables, demo, calibration ----------
    def sample(self, charset):
        """Print characters 0x20-0x7E of a set, 16 per line, hex code first."""
        if charset == "pdi":
            raise ValueError("the graphics set contains commands, not characters")
        state = (self.is_wide, self.alignment)
        if self.is_wide:
            self.wide(False)
        self.align("left")
        code, _, desc = CHARSETS[charset]
        self.line(f"{charset} 0x{code:02X} {desc}")
        table = 3 if charset == "drc" else 2
        self._need(table, charset)
        for start in range(0x20, 0x80, 16):
            self.invoke(0)
            self._w(f"{start:02X} ".encode())
            self.invoke(table)
            self._w(bytes(c for c in range(start, start + 16) if c < 0x7F))
            self.newline()
        self.invoke(0)
        if state[0]:
            self.wide(True)
        self.align(state[1])
        return self


def demo_page(p):
    """Demonstration page of every feature."""
    p.title("B&K 2318")
    p.line(f"b2318.py {__version__}", wrap=False)
    p.rule("full")
    p.line("Normal text: 24 col.")
    p.wide(True).line("WIDE 12").wide(False)
    p.inverse(True).line(" Inverse video ").inverse(False)
    p.align("right").line("Right aligned")
    p.align("center").line("Centered")
    p.align("left")
    p.kv("Key", "value", ".")
    p.table([["Freq", "dB"], ["1 kHz", "94.0"], ["250 Hz", "88.5"]])
    p.rule("dashed")
    p.line("Accents: à é è ù ç")
    p.line("DRC: ê â î ô û ë ï €")
    p.line("German: Ä Ö Ü ä ö ü ß")
    p.line("Russian: ПРИВЕТ")
    p.line("Greek: ΑΒΓΔ ΩΣ")
    p.line("Kana: カタカナ ガギグ")
    p.line("Runes:")
    p.raw_charset("runes")
    for s in ("@ABCDEFGHIJKLMNO", "PQRSTUVWXYZ[\\]^_", "`abcdefghijklmno", "pqrstuvwxyz{|}~"):
        p.line(s, wrap=False)
    p.raw_charset(None)
    p.rule("dotted")
    for kind in LINE_TYPES:
        p.line(f"Line {kind}")
        p.rule(kind)
    p.line("Native bars 0.5:")
    for size in ("wide", "narrow"):
        for style in ("filled", "hatched", "outline"):
            p.bar(0.5, size, style).newline()
    p.line("Bar chart:")
    p.bar_chart([("63", 72), ("125", 68), ("250", 61), ("500", 58), ("1k", 55)], unit="dB",
                native=False)
    p.line("Dot columns:")
    p.dots(0x10, 0x20, 0x7F, 0x20, 0x10).newline()       # arrow, manual example
    p.rule("full")


def banner_samples(p):
    """The four banner styles, to pick the most readable one."""
    for style in TITLE_STYLES:
        p.line(f"Banner style: {style}")
        p.title("B&K 2318", style)
        p.feed(1)


def calibration_page(p):
    """Page to measure the native bar scale and the line spacing."""
    p.title("CALIBRATION")
    p.line("Ruler: 1 tick = 7 dots")
    masks = []
    for c in range(DOTS):
        m = 0b0000011 if c % 7 == 0 else 0
        if c % 28 == 0:
            m = 0b0001111
        if c in (0, DOTS - 1):
            m = 0x7F
        masks.append(m)
    p.band(masks)
    p.line("0   28  56  84  112 140", wrap=False)
    for x in (0.25, 0.5, 4095 / 4096):
        p.bar(x).newline()
        p.line(f"native bar {x:.2f}")
    p.line("Measure the 0.50 bar on")
    p.line("the ruler (in dots):")
    p.line("scale_px = 2 x length")
    p.line("b2318 config scale_px N")
    p.rule("dashed")
    p.line("Line spacing: 3 black")
    p.line("lines below.")
    for _ in range(3):
        p.band([0x7F] * DOTS)
    p.line("aspect = (black+white)")
    p.line("         / black")
    p.line("b2318 config aspect X")


# =============================================================================
# Text preview and text templates
# =============================================================================

class PreviewPrinter(Printer):
    """Printer that records a text approximation instead of bytes (previews).
    out: list of (text, inverse)."""

    RULES = {"full": "-" * 24, "dotted": "." * 24, "dashed": "- " * 12, "dotdash": "-." * 12}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.out = []

    def _line_glyphs(self, glyphs):
        s = "".join(g[3] for g in glyphs)
        w = self.width
        if self.alignment == "center":
            s = " " * ((w - len(s)) // 2) + s
            if self.is_inverse:
                s = s.ljust(w)
        elif self.alignment == "right":
            s = s.rjust(w)
        if self.is_wide:                              # one wide character = two columns
            s = "".join(c + " " for c in s)
            s = s if self.is_inverse else s.rstrip()
        self.out.append((s, self.is_inverse))

    def newline(self, n=1):
        self.out.extend([("", False)] * int(n))
        return self

    feed = newline

    def rule(self, kind="dashed", row=4, newline=True):
        self.out.append((self.RULES[kind], False))
        return self

    def band(self, masks, newline=True):
        self.out.append(("\u2592" * 24 if any(masks) else "", False))
        return self

    def bar_chart(self, items, vmin=None, vmax=None, unit="", *args, **kw):
        values = [float(str(v).replace(",", ".")) for _, v in items]
        if not values:
            return self
        lo = (0.0 if min(values) >= 0 else min(values)) if vmin is None else float(vmin)
        hi = max(values) if vmax is None else float(vmax)
        if lo > hi:
            lo, hi = hi, lo
        if hi == lo:
            hi = lo + 1
        for (lab, _), v in zip(items, values):
            n = round(10 * (min(max(v, lo), hi) - lo) / (hi - lo))
            self.out.append((f"{str(lab)[:6]:<6} {v:g}{unit}"[:13].ljust(14) + "█" * n, False))
        self.out.append((" " * 14 + f"{lo:g}..{hi:g}"[:10], False))
        return self

    def image(self, source, **kw):
        name = getattr(source, "name", None) or Path(str(source)).name
        kw.pop("center", None)
        try:
            h = self.prepare_image(source, **kw).size[1]
            self.out.append((f"[{name}: {(h + 6) // 7} lines]"[:24], False))
        except Exception as e:
            self.out.append((f"[{name}: {e}]"[:24], False))
        return self


def _templates_dir():
    d = APP_DIR / "templates"
    if not os.access(APP_DIR, os.W_OK):                       # e.g. .exe in Program Files
        d = Path.home() / APP_NAME / "templates"
    return d


TEMPLATES_DIR = _templates_dir()
TEMPLATE_EXTS = (".txt", ".b2318")


def ensure_templates():
    """First run of the .exe: copy the bundled example templates next to it."""
    src = BUNDLE_DIR / "templates"
    if FROZEN and src.is_dir() and not TEMPLATES_DIR.exists():
        try:
            shutil.copytree(src, TEMPLATES_DIR)
        except OSError:
            pass
BUILTIN_FIELDS = ("date", "time", "datetime")
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{(\w+)\}")

TEMPLATE_HELP = """\
Text template format (UTF-8 text file, .txt or .b2318)

Plain line          printed as is, wrapped at 24 columns
# comment           ignored (the first one is the description)
\\# or \\@           literal # or @ at the start of a line
{name}              field, asked when printing ({{ }} = literal braces)
{date} {time} {datetime}   filled in automatically

@field name "Label" "default"   label and default value of a field
@title TEXT                     banner (wide, inverse, centered)
@kv Label | Value | fill        label left, value right (fill: . or -)
@rule full|dotted|dashed|dotdash [row 1-7]
@feed N                         N empty lines
@wide on|off   @inverse on|off   @align left|center|right
@charset auto|fr|de|ru|gr|kana|runes...
@table ... @end                 one row per line, cells separated by |
@bars unit=dB min=0 max=100 style=filled|hatched|outline size=wide|narrow
  label = value ... @end        bar chart
@image file.png [width=120] [threshold=128] [invert] [rotate]
@raw ESC 28 4D CR    @eot    @reset
"""


def builtin_values():
    now = time.localtime()
    return {"date": time.strftime("%Y-%m-%d", now), "time": time.strftime("%H:%M", now),
            "datetime": time.strftime("%Y-%m-%d %H:%M", now)}


def substitute(s, values):
    """Replace {name} with values[name]; {{ and }} give literal braces."""
    def rep(m):
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        return str(values.get(m.group(1), ""))
    return _PLACEHOLDER.sub(rep, s)


class Template:
    """Text template: plain lines are printed, @ lines are commands,
    {name} are fields filled in when printing. See TEMPLATE_HELP."""

    COMMANDS = {"title", "kv", "rule", "feed", "wide", "inverse", "align", "charset",
                "table", "bars", "image", "raw", "eot", "reset", "field", "end"}

    def __init__(self, text, path=None):
        self.path = Path(path) if path else None
        self.name = self.path.stem if self.path else "template"
        self.description = ""
        self.ops = []                                 # (line number, command, argument)
        self.fields = {}                              # name -> {"label", "default"}
        self._parse(text)

    @classmethod
    def load(cls, path):
        return cls(Path(path).read_text(encoding="utf-8-sig"), path)

    def _add_field(self, name, label=None, default=""):
        if name in BUILTIN_FIELDS and label is None:
            return
        f = self.fields.setdefault(name, {"label": name.replace("_", " ").capitalize(),
                                          "default": ""})
        if label is not None:
            f["label"], f["default"] = label, default

    def _scan(self, s):
        for m in _PLACEHOLDER.finditer(s):
            if m.group(1):
                self._add_field(m.group(1))

    def _parse(self, text):
        block = None
        for n, line in enumerate(text.splitlines(), 1):
            if block is not None:                     # inside @table / @bars
                if line.strip().lower() == "@end":
                    self.ops.append((block[0], block[1], (block[2], block[3])))
                    block = None
                elif line.strip() and not line.lstrip().startswith("#"):
                    self._scan(line)
                    block[3].append(line.strip())
                continue
            if line.startswith("#"):
                if not self.description and not self.ops:
                    self.description = line.lstrip("#").strip()
                continue
            if line.startswith("@"):
                cmd, _, arg = line[1:].partition(" ")
                cmd, arg = cmd.lower(), arg.strip()
                if cmd not in self.COMMANDS:
                    raise ValueError(f"line {n}: unknown command @{cmd}")
                if cmd == "end":
                    raise ValueError(f"line {n}: @end without @table or @bars")
                if cmd == "field":
                    parts = shlex.split(arg)
                    if not parts or not re.fullmatch(r"\w+", parts[0]):
                        raise ValueError(f'line {n}: expected @field name "Label" "default"')
                    self._add_field(parts[0], parts[1] if len(parts) > 1 else parts[0],
                                    parts[2] if len(parts) > 2 else "")
                    continue
                self._scan(arg)
                if cmd in ("table", "bars"):
                    block = (n, cmd, arg, [])
                else:
                    self.ops.append((n, cmd, arg))
                continue
            if line.startswith(("\\#", "\\@")):
                line = line[1:]
            self._scan(line)
            self.ops.append((n, "text", line))
        if block is not None:
            raise ValueError(f"line {block[0]}: @{block[1]} without @end")

    @staticmethod
    def _opts(s):
        words, opts = [], {}
        for tok in shlex.split(s):
            k, eq, val = tok.partition("=")
            if eq:
                opts[k.lower()] = val
            else:
                words.append(tok)
        return words, opts

    def default_values(self):
        return {k: f["default"] for k, f in self.fields.items()}

    def render(self, p, values=None):
        """Apply the template to a Printer (or PreviewPrinter)."""
        v = builtin_values()
        v.update(self.default_values())
        v.update({k: val for k, val in (values or {}).items() if val is not None})
        base = self.path.parent if self.path else Path.cwd()
        for n, cmd, arg in self.ops:
            try:
                self._run(p, cmd, arg, v, base)
            except (ValueError, KeyError, IndexError, OSError) as e:
                raise ValueError(f"{self.name}, line {n}: {e}") from e
        return p

    def _run(self, p, cmd, arg, v, base):
        if cmd in ("table", "bars"):
            arg, rows = arg
            rows = [substitute(r, v) for r in rows]
        arg = substitute(arg, v)
        if cmd == "text":
            p.line(arg)
        elif cmd == "title":
            p.title(arg)
        elif cmd == "kv":
            parts = [x.strip() for x in arg.split("|")]
            fill = parts[2] if len(parts) > 2 and parts[2] else " "
            p.kv(parts[0], parts[1] if len(parts) > 1 else "", fill)
        elif cmd == "rule":
            words = arg.split()
            kind = words[0].lower() if words else "dashed"
            if kind not in LINE_TYPES:
                raise ValueError(f"line type: {', '.join(LINE_TYPES)}")
            p.rule(kind, int(words[1]) if len(words) > 1 else 4)
        elif cmd == "feed":
            p.feed(int(arg or 1))
        elif cmd in ("wide", "inverse"):
            on = arg.lower() not in ("off", "0", "no", "false")
            p.wide(on) if cmd == "wide" else p.inverse(on)
        elif cmd == "align":
            p.align(arg.lower() or "left")
        elif cmd == "charset":
            name = arg.lower()
            if name in ("", "auto", "us"):
                p.raw_charset(None)
            elif name in TEXT_SETS:
                p.raw_charset(name)
            else:
                raise ValueError(f"character set: auto, {', '.join(TEXT_SETS)}")
        elif cmd == "table":
            p.table([[c.strip() for c in r.split("|")] for r in rows])
        elif cmd == "bars":
            _, o = self._opts(arg)
            size, style = o.get("size", "wide"), o.get("style", "filled")
            if size not in ("wide", "narrow") or style not in ("filled", "hatched", "outline"):
                raise ValueError("size=wide|narrow, style=filled|hatched|outline")
            p.bar_chart(parse_items(rows),
                        vmin=float(o["min"].replace(",", ".")) if "min" in o else None,
                        vmax=float(o["max"].replace(",", ".")) if "max" in o else None,
                        unit=o.get("unit", ""), size=size, style=style)
        elif cmd == "image":
            words, o = self._opts(arg)
            if not words:
                raise ValueError("@image needs a file name")
            path = Path(words[0])
            if not path.is_absolute():
                path = base / path
            kw = {"invert": "invert" in words[1:], "rotate": "rotate" in words[1:]}
            if "width" in o:
                kw["width"] = int(o["width"])
            if "threshold" in o:
                kw.update(dither=False, threshold=int(o["threshold"]))
            p.image(path, **kw)
        elif cmd == "raw":
            p.raw(parse_raw(arg))
        elif cmd == "eot":
            p.eot()
        elif cmd == "reset":
            p.reset()

    def images(self):
        """Image files referenced by the template (relative paths as written)."""
        out = []
        for _, cmd, arg in self.ops:
            if cmd == "image":
                words, _ = self._opts(arg)
                if words and "{" not in words[0]:
                    out.append(Path(words[0]))
        return out


def list_templates(folder=None):
    folder = Path(folder or TEMPLATES_DIR)
    if not folder.is_dir():
        return []
    return sorted((f for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in TEMPLATE_EXTS),
                  key=lambda f: f.name.lower())


def find_template(name):
    """Template from a path, or a name in the templates folder (extension optional)."""
    p = Path(name)
    if p.is_file():
        return p
    for cand in [TEMPLATES_DIR / name] + [TEMPLATES_DIR / (name + e) for e in TEMPLATE_EXTS]:
        if cand.is_file():
            return cand
    raise ValueError(f"template not found: {name} (looked in {TEMPLATES_DIR})")


def import_template(src, folder=None):
    """Check a template and copy it (with the images it uses) into the templates folder."""
    src = Path(src)
    tpl = Template.load(src)                          # raises if the syntax is wrong
    folder = Path(folder or TEMPLATES_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    for rel in tpl.images():
        if not rel.is_absolute() and (src.parent / rel).is_file():
            target = folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if (src.parent / rel).resolve() != target.resolve():
                shutil.copy2(src.parent / rel, target)
    return dest


# =============================================================================
# Serial link
# =============================================================================

class Link:
    """Serial link to the printer (pyserial), 8N1. The port can also be a pyserial
    URL, e.g. socket://192.168.4.1:2323 for a Wi-Fi serial bridge.

    XON/XOFF is handled here rather than by the OS driver, so the bytes the
    printer sends back stay visible (activity LED, "buffer full" status)."""

    CHUNK = 16                                # bytes per write below 9600 baud

    def __init__(self, port, baud=1200, xonxoff=True, rtscts=False):
        import serial
        self.baud = int(baud)
        self.xonxoff = bool(xonxoff)
        self.sending = False                  # True while send() runs
        self.paused = False                   # True after XOFF, until XON
        self.activity = 0                     # incremented on every TX write and RX read
        self._rx_lock = threading.Lock()
        opts = dict(baudrate=self.baud, bytesize=8, parity="N", stopbits=1,
                    xonxoff=False, rtscts=rtscts, timeout=0.2, write_timeout=None)
        if "://" in port:
            self.ser = serial.serial_for_url(port, **opts)
        else:
            self.ser = serial.Serial(port, **opts)

    def poll_rx(self):
        """Read whatever the printer sent, track XON/XOFF, record RX activity.
        Raises if the port is gone."""
        with self._rx_lock:
            n = self.ser.in_waiting
            if not n:
                return b""
            data = self.ser.read(n)
        self.activity += 1
        if self.xonxoff:
            for byte in data:
                # In 7-bit data mode the printer sends 7 data bits: read as 8N1, its stop
                # bit becomes bit 7, so XOFF/XON arrive as 0x93/0x91 (found by saschapo).
                byte &= 0x7F
                if byte == XOFF:
                    self.paused = True
                elif byte == XON:
                    self.paused = False
        return data

    def _wait_xon(self, cancel):
        self.poll_rx()
        while self.paused:
            if cancel is not None and cancel.is_set():
                return False
            time.sleep(0.01)
            self.poll_rx()
        return True

    def send(self, data, progress=None, cancel=None):
        """Send the bytes, paced by the clock instead of flush() (on Windows,
        pyserial's flush() polls every 50 ms, which slowed small writes down).
        At most one chunk is queued in the adapter, so XOFF takes effect quickly.
        At 9600 and 19200 baud, bytes go one by one, 3 ms apart (the manual asks
        for at least 2 ms from start bit to start bit, section 4.1.5).
        progress(n) is called with the count sent. cancel: threading.Event.
        Returns the number of bytes actually sent."""
        data = bytes(data)
        spaced = self.baud >= 9600
        step = 1 if spaced else self.CHUNK
        per_byte = 0.003 if spaced else 10 / self.baud
        lead = 0.0 if spaced else per_byte * step     # how far ahead we may write
        self.sending = True
        try:
            i = 0
            if data[:1] == bytes([NSR]):              # give the reset some time
                self.ser.write(data[:1])
                self.activity += 1
                time.sleep(0.2)
                i = 1
            t_free = time.perf_counter()              # estimated end of what was written
            while i < len(data):
                if cancel is not None and cancel.is_set():
                    return i
                if not self._wait_xon(cancel):
                    return i
                now = time.perf_counter()
                wait = t_free - lead - now
                if wait > 0:
                    time.sleep(wait)
                    now = time.perf_counter()
                chunk = data[i:i + step]
                self.ser.write(chunk)
                t_free = max(t_free, now) + per_byte * len(chunk)
                self.activity += 1
                i += len(chunk)
                if progress:
                    progress(i)
            rest = t_free - time.perf_counter()       # let the last chunk go out
            if rest > 0:
                time.sleep(rest)
            return len(data)
        finally:
            self.sending = False

    def status(self):
        """Control lines: CTS = the printer's HS signal if the cable carries it.
        None if the adapter cannot report them."""
        s = self.ser
        try:
            return {"CTS": s.cts, "DSR": s.dsr, "CD": s.cd, "RI": s.ri}
        except (OSError, AttributeError, NotImplementedError):
            return None

    def read(self, n=64):
        return self.ser.read(n)

    def close(self):
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    return [(p.device, p.description) for p in lp.comports()]


# =============================================================================
# Dependencies
# =============================================================================

PACKAGES = {"serial": "pyserial", "PIL": "pillow"}
REQUIREMENTS_FILE = APP_DIR / "requirements.txt"


def missing_packages():
    """Missing pip packages. Also detects the wrong 'serial' package that hides pyserial."""
    import importlib
    if FROZEN:                                         # everything is bundled in the .exe
        return []
    missing = []
    for mod, pkg in PACKAGES.items():
        try:
            m = importlib.import_module(mod)
            if mod == "serial" and not hasattr(m, "Serial"):
                missing.append("pyserial (the installed 'serial' package is the wrong one: "
                               "pip uninstall serial)")
        except ImportError:
            missing.append(pkg)
    return missing


def pip_command(pkgs=None):
    """Install command for the Python running this script."""
    if REQUIREMENTS_FILE.exists():
        target = f'-r "{REQUIREMENTS_FILE}"'
    else:
        target = " ".join(pkgs or PACKAGES.values())
    return f'"{sys.executable}" -m pip install {target}'


def install_packages():
    """Install the dependencies with the current Python's pip. Returns (ok, log)."""
    import importlib
    import site
    import subprocess
    args = ["-r", str(REQUIREMENTS_FILE)] if REQUIREMENTS_FILE.exists() else list(PACKAGES.values())
    r = subprocess.run([sys.executable, "-m", "pip", "install", *args],
                       capture_output=True, text=True)
    try:                                   # --user install: make the folder visible
        usp = site.getusersitepackages()
        if os.path.isdir(usp) and usp not in sys.path:
            site.addsitedir(usp)
    except (AttributeError, OSError):
        pass
    importlib.invalidate_caches()
    return r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def hexdump(data, width=16):
    out = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hx = " ".join(f"{b:02X}" for b in chunk)
        tx = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        out.append(f"{i:06X}  {hx:<{width * 3}} {tx}")
    return "\n".join(out)


def line_ends(data):
    """Positions of the bytes that make the printer print a line (CR, LF, FF)."""
    return [i for i, c in enumerate(data) if c in (CR, LF, FF)]


def estimate_seconds(data, baud, lines_per_second=LINES_PER_SECOND):
    """Estimated job duration: each line is printed once it has been received,
    one after the other, at lines_per_second."""
    per_byte = 0.003 if baud >= 9600 else 10 / baud
    t = 0.0
    for end in line_ends(data):
        t = max(t, (end + 1) * per_byte) + 1 / lines_per_second
    return max(t, len(data) * per_byte)


RAW_NAMES = {"EOT": EOT, "LF": LF, "FF": FF, "CR": CR, "SO": SO, "SI": SI, "SS2": SS2,
             "SS3": SS3, "ESC": ESC, "NSR": NSR, "RESET": NSR}


def parse_raw(spec):
    """Raw bytes: names (ESC, CR, SO...), hex bytes (1B, 0x1b, 0x1B284D),
    text (t:text forces text, \\x1b accepted). E.g.: ESC 28 4D t:ABC CR"""
    tokens = shlex.split(spec) if isinstance(spec, str) else list(spec)
    out = bytearray()
    for t in tokens:
        u = t.upper()
        if u in RAW_NAMES:
            out.append(RAW_NAMES[u])
        elif re.fullmatch(r"[0-9A-F]{2}", u):
            out.append(int(u, 16))
        elif re.fullmatch(r"0X([0-9A-F]{2})+", u):
            out += bytes.fromhex(u[2:])
        else:
            s = t[2:] if t.startswith("t:") else t
            if "\\" in s:
                s = s.encode("latin-1").decode("unicode_escape")
            out += s.encode("latin-1")
    return bytes(out)


def diagnose(port, baud, send_test=True):
    """Check the cable: CTS state (HS signal), received bytes, test line."""
    lines = [f"Port {port}, {baud} baud 8N1"]

    def describe(rx):
        names = {XON: "XON (ready)", XOFF: "XOFF (buffer full)"}
        return " ".join(f"{b:02X}" + (f" = {names[b & 0x7F]}" if b & 0x7F in names else "")
                        for b in rx) or "nothing"

    def fmt(st):
        if st is None:
            return "Control lines cannot be read on this adapter."
        return "Lines: " + "  ".join(f"{k}={int(v)}" for k, v in st.items())

    with Link(port, baud, xonxoff=False, rtscts=False) as link:
        st = link.status()
        lines.append(fmt(st))
        if st and st["CTS"]:
            lines.append("CTS=1: HS signal received, printer ready. HS is wired correctly.")
        elif st:
            lines.append("CTS=0: printer off/busy, or HS (LEMO pin 2) not connected "
                         "to DB9 pin 8. Use XON/XOFF in that case.")
        lines.append("Received before sending: " + describe(link.read(64)))
        if send_test:
            p = Printer()
            p.reset().line("DIAG b2318 OK").line(f"{baud} baud 8N1").feed(2)
            link.send(p.take())
            time.sleep(1.0)
            lines.append("Test line sent.")
            lines.append("Received after sending: " + describe(link.read(64)))
            lines.append(fmt(link.status()))
            lines.append("If nothing prints: TX/RX swapped (null modem?), wrong baud rate "
                         "(see the set-up printout), batteries, ZI 0054 module not seated.")
    return "\n".join(lines)


# =============================================================================
# Configuration
# =============================================================================

def _config_path():
    """Settings file next to the program (script or .exe). Falls back to a folder
    in the user's home only if the program's folder is read-only."""
    env = os.environ.get("B2318_CONFIG")
    if env:
        return Path(env)
    if os.access(APP_DIR, os.W_OK):
        return APP_DIR / f"{APP_NAME}.json"
    return Path.home() / APP_NAME / f"{APP_NAME}.json"


CONFIG_PATH = _config_path()
LEGACY_CONFIG = Path.home() / ".b2318.json"           # location used up to version 1.7
CONFIG_VERSION = 2
DEFAULT_CONFIG = {"config_version": CONFIG_VERSION, "port": "", "baud": 1200, "xonxoff": True,
                  "rtscts": False, "mode": "auto", "prefer": DEFAULT_PREFER,
                  "scale_px": DEFAULT_SCALE_DOTS, "aspect": DEFAULT_ASPECT,
                  "lines_per_second": LINES_PER_SECOND, "title_style": "wide-inverse",
                  "glyphs": {}}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = CONFIG_PATH
    migrate = not path.exists() and LEGACY_CONFIG.is_file() and "B2318_CONFIG" not in os.environ
    if migrate:
        path = LEGACY_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    cfg.update(data)
    if data.get("config_version", 1) < 2:             # old defaults replaced by measured ones
        if cfg.get("aspect") == 1.5:
            cfg["aspect"] = DEFAULT_ASPECT
        if not cfg.get("scale_px"):
            cfg["scale_px"] = DEFAULT_SCALE_DOTS
        cfg["config_version"] = CONFIG_VERSION
        migrate = True
    if migrate and save_config(cfg):                  # moved next to the program
        try:
            LEGACY_CONFIG.unlink()
        except OSError:
            pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError as e:
        print(f"Settings not saved: {e}", file=sys.stderr)
        return False


# =============================================================================
# Command line
# =============================================================================

def make_printer(args, cfg):
    return Printer(mode="ascii" if getattr(args, "ascii", False) else cfg["mode"],
                   prefer=cfg["prefer"], glyphs=cfg["glyphs"],
                   scale_px=cfg["scale_px"], aspect=cfg["aspect"],
                   title_style=cfg.get("title_style", "wide-inverse"))


def resolve_port(args, cfg):
    port = args.port or os.environ.get("B2318_PORT") or cfg.get("port")
    if port:
        return port
    ports = list_ports()
    if len(ports) == 1:
        return ports[0][0]
    sys.exit("Serial port not found: use -p COM3 (Windows) or -p /dev/ttyUSB0 (Linux). "
             "See 'python b2318.py ports'.")


def open_link(args, cfg):
    return Link(resolve_port(args, cfg), args.baud or cfg["baud"],
                xonxoff=cfg["xonxoff"] and not args.no_xonxoff,
                rtscts=args.rtscts or cfg["rtscts"])


def deliver(data, args, cfg):
    baud = args.baud or cfg["baud"]
    secs = estimate_seconds(data, baud, cfg["lines_per_second"])
    if args.out:
        Path(args.out).write_bytes(data)
        print(f"{len(data)} bytes written to {args.out}")
    if args.dry_run:
        print(hexdump(data))
        print(f"{len(data)} bytes, ~{secs:.1f} s at {baud} baud (nothing was sent)")
        return
    if args.out and not args.port:
        return
    with open_link(args, cfg) as link:
        link.send(data)
    print(f"{len(data)} bytes sent ({baud} baud, printing ~{secs:.1f} s)")


def run_job(args, cfg, build, reset=True, feed=True):
    p = make_printer(args, cfg)
    if reset and not args.no_reset:
        p.soft_reset()
    build(p)
    if feed and args.feed:
        p.feed(args.feed)
    deliver(p.take(), args, cfg)


def read_text_arg(parts):
    if not parts or parts == ["-"]:
        if sys.stdin.isatty():
            print("Text to print (Ctrl+D, or Ctrl+Z then Enter on Windows, to finish):",
                  file=sys.stderr)
        return sys.stdin.read().rstrip("\n")
    return " ".join(parts)


def parse_items(specs, csv_path=None):
    items = []
    lines = list(specs or [])
    if csv_path:
        lines += Path(csv_path).read_text(encoding="utf-8").splitlines()
    for s in lines:
        s = s.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^(.*?)\s*[=;\t]\s*([-+]?\d+(?:[.,]\d+)?)\s*$", s) or \
            re.match(r"^(.*?)\s*,\s*([-+]?\d+(?:\.\d+)?)\s*$", s)
        if not m:
            raise ValueError(f"Line not understood (expected label=value): {s}")
        items.append((m.group(1), float(m.group(2).replace(",", "."))))
    return items


def cmd_text(args, cfg):
    content = read_text_arg(args.text)

    def build(p):
        if args.title:
            p.title(args.title)
        if args.wide:
            p.wide()
        if args.inverse:
            p.inverse()
        p.align(args.align)
        if args.charset:
            p.raw_charset(args.charset)
        p.text(content)
    run_job(args, cfg, build)


def cmd_sample(args, cfg):
    names = TEXT_SETS if args.all else (args.charset or ["us"])

    def build(p):
        for n in names:
            p.sample(n).feed(1)
    run_job(args, cfg, build)


def cmd_image(args, cfg):
    run_job(args, cfg, lambda p: p.image(
        args.file, width=args.width, dither=args.threshold is None and not args.no_dither,
        threshold=args.threshold or 128, invert=args.invert, rotate=args.rotate,
        aspect=args.aspect))


def cmd_bars(args, cfg):
    items = parse_items(args.items, args.csv)
    if not items:
        sys.exit('No values: bars "63 Hz=72" "125 Hz=68" or --csv file')
    native = True if args.native else (False if args.dots else None)
    run_job(args, cfg, lambda p: (p.title(args.title) if args.title else None, p.bar_chart(
        items, vmin=args.min, vmax=args.max, unit=args.unit,
        size="narrow" if args.narrow else "wide", style=args.style, native=native)))


def _numbers(stream):
    for line in stream:
        for tok in re.split(r"[\s;]+", line.strip()):
            if _is_number(tok):
                yield float(tok.replace(",", "."))


def cmd_strip(args, cfg):
    src = open(args.file, encoding="utf-8") if args.file and args.file != "-" else sys.stdin
    live = args.min is not None and args.max is not None
    if not live:                                      # read everything, auto scale
        values = list(_numbers(src))
        if not values:
            sys.exit("No numeric value found")
        lo, hi = min(values), max(values)
        if hi == lo:
            hi = lo + 1
        lo = args.min if args.min is not None else lo
        hi = args.max if args.max is not None else hi

        def build(p):
            p.kv(f"{lo:g}", f"{hi:g}")
            prev = None
            for i, v in enumerate(values):
                if args.label_every and i % args.label_every == 0 and i:
                    p.line(f"#{i} {v:g}")
                prev = p.strip_point(v, lo, hi, prev, i, grid_every=args.grid)
        run_job(args, cfg, build)
        return
    p = make_printer(args, cfg)
    if args.dry_run:
        sys.exit("live strip: --dry-run not available, remove --min/--max")
    with open_link(args, cfg) as link:
        if not args.no_reset:
            p.soft_reset()
        p.kv(f"{args.min:g}", f"{args.max:g}")
        link.send(p.take())
        prev, i = None, 0
        try:
            for v in _numbers(src):
                if args.label_every and i % args.label_every == 0 and i:
                    p.line(f"#{i} {v:g}")
                prev = p.strip_point(v, args.min, args.max, prev, i, grid_every=args.grid)
                link.send(p.take())
                i += 1
        except KeyboardInterrupt:
            pass
        p.feed(args.feed)
        link.send(p.take())


def cmd_glyph(args, cfg):
    glyphs = cfg.setdefault("glyphs", {})
    if args.action == "list":
        for ch, rows in {**BUILTIN_GLYPHS, **glyphs}.items():
            src = "custom" if ch in glyphs else "built-in"
            print(f"{ch}  ({src})")
            for r in rows:
                print("    " + r.replace(".", " ").replace("#", "█"))
        return
    if not args.char or len(args.char) != 1:
        sys.exit("Give a single character, e.g.: glyph add € ..### .#... ####. .#... ####. .#... ..###")
    if args.action == "add":
        parse_pattern(args.rows)                     # validation
        if ord(args.char) < 0x7F:
            print("Warning: ASCII character, the built-in font will be used instead.")
        glyphs[args.char] = args.rows
        save_config(cfg)
        print(f"Glyph {args.char} saved to {CONFIG_PATH}")
    elif args.action == "del":
        if glyphs.pop(args.char, None) is None:
            sys.exit("Unknown custom glyph")
        save_config(cfg)
        print("Deleted")
    elif args.action == "test":
        rows = glyphs.get(args.char) or BUILTIN_GLYPHS.get(args.char)
        if not rows:
            sys.exit("Unknown glyph")

        def build(p):
            code = p.define_char(0x21, rows)
            p.write(f"Test {args.char}: ")
            p._emit([(3, code, "drc", args.char)] * 8)
            p.newline()
        run_job(args, cfg, build)


def cmd_template(args, cfg):
    ensure_templates()
    if args.import_:
        dest = import_template(args.import_)
        print(f"Template imported: {dest}")
        return
    if args.list or not args.file:
        files = list_templates()
        if not files:
            print(f"No template in {TEMPLATES_DIR}")
        for f in files:
            try:
                t = Template.load(f)
                fields = ", ".join(t.fields) or "-"
                print(f"{f.stem:<24} {t.description}\n{'':<24} fields: {fields}")
            except ValueError as e:
                print(f"{f.stem:<24} ERROR {e}")
        return
    tpl = Template.load(find_template(args.file))
    values = {}
    for item in args.set:
        k, eq, val = item.partition("=")
        if not eq:
            sys.exit(f"--set expects NAME=VALUE, got: {item}")
        values[k.strip()] = val
    for name, f in tpl.fields.items():
        if name in values:
            continue
        if sys.stdin.isatty() and not args.dry_run:
            ans = input(f"{f['label']} [{f['default']}]: ")
            values[name] = ans if ans else f["default"]
    run_job(args, cfg, lambda p: tpl.render(p, values))


def cmd_raw(args, cfg):
    deliver(parse_raw(args.tokens), args, cfg)


def cmd_send(args, cfg):
    deliver(Path(args.file).read_bytes(), args, cfg)


def cmd_type(args, cfg):
    p = make_printer(args, cfg)
    with open_link(args, cfg) as link:
        if not args.no_reset:
            p.soft_reset()
            link.send(p.take())
        print("Typewriter mode: each line is printed after Enter. "
              "Ctrl+D (Ctrl+Z then Enter on Windows) to finish.", file=sys.stderr)
        try:
            for line in sys.stdin:
                p.line(line.rstrip("\r\n"))
                link.send(p.take())
        except KeyboardInterrupt:
            pass
        p.feed(args.feed)
        link.send(p.take())


def cmd_diag(args, cfg):
    print(diagnose(resolve_port(args, cfg), args.baud or cfg["baud"], not args.no_print))


def cmd_ports(args, cfg):
    missing = [m for m in missing_packages() if m.startswith("pyserial")]
    if missing:
        sys.exit(f"{missing[0]} missing. Install it with:\n  {pip_command()}")
    ports = list_ports()
    if not ports:
        print("No serial port found (adapter plugged in? driver installed?)")
    for dev, desc in ports:
        print(f"{dev}\t{desc}")


def cmd_config(args, cfg):
    if not args.key:
        print(f"# {CONFIG_PATH}")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return
    key = args.key
    hidden = ("glyphs", "config_version")
    if key not in DEFAULT_CONFIG or key in hidden:
        sys.exit(f"Possible keys: {', '.join(k for k in DEFAULT_CONFIG if k not in hidden)}")
    if args.value is None:
        print(cfg.get(key))
        return
    v = args.value
    try:
        if key == "baud":
            v = int(v)
        elif key in ("xonxoff", "rtscts"):
            v = v.lower() in ("1", "on", "true", "yes")
        elif key == "scale_px":
            v = None if v.lower() in ("none", "0", "") else int(v)
        elif key in ("aspect", "lines_per_second"):
            v = float(v.replace(",", "."))
        elif key == "mode" and v not in ("auto", "ascii"):
            raise ValueError("auto or ascii")
        elif key == "title_style" and v not in TITLE_STYLES:
            raise ValueError(", ".join(TITLE_STYLES))
        elif key == "prefer":
            v = [x.strip() for x in v.split(",") if x.strip()]
            bad = [x for x in v if x not in ALT]
            if bad:
                raise ValueError(f"unknown sets: {bad} (possible: {', '.join(ALT)})")
    except ValueError as e:
        sys.exit(f"Invalid value for {key}: {e}")
    cfg[key] = v
    save_config(cfg)
    print(f"{key} = {v}")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="b2318", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Full driver for the Brüel & Kjær 2318 printer (ZI 0054 module).",
        epilog="No command: graphical interface. Help on a command: b2318 COMMAND -h")
    ap.add_argument("-p", "--port", help="serial port (COM3, /dev/ttyUSB0, socket://host:port...)")
    ap.add_argument("-b", "--baud", type=int, help="baud rate (default: config, 1200)")
    ap.add_argument("--rtscts", action="store_true", help="hardware handshake, HS on CTS")
    ap.add_argument("--no-xonxoff", action="store_true", help="disable XON/XOFF")
    ap.add_argument("--ascii", action="store_true", help="plain ASCII text (no sets, no DRC)")
    ap.add_argument("--no-reset", action="store_true",
                    help="do not restore the default sets and styles before the job")
    ap.add_argument("--feed", type=int, default=3, help="lines fed at the end (default 3)")
    ap.add_argument("--dry-run", action="store_true", help="show the bytes without sending")
    ap.add_argument("--out", metavar="FILE", help="save the bytes to a file")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("text", help="print text")
    s.add_argument("text", nargs="*", help="text (nothing or -: standard input)")
    s.add_argument("--wide", action="store_true", help="double width (12 chars/line)")
    s.add_argument("--inverse", action="store_true", help="white on black")
    s.add_argument("--align", choices=["left", "center", "right"], default="left")
    s.add_argument("--title", help="banner before the text")
    s.add_argument("--charset", choices=TEXT_SETS, help="raw set in G0 (e.g. runes)")
    s.set_defaults(func=cmd_text)

    s = sub.add_parser("demo", help="demonstration page of every feature")
    s.set_defaults(func=lambda a, c: run_job(a, c, demo_page))

    s = sub.add_parser("sample", help="print the table of one or more character sets")
    s.add_argument("--charset", action="append", choices=TEXT_SETS + ["drc"])
    s.add_argument("--all", action="store_true", help="every set")
    s.set_defaults(func=cmd_sample)

    s = sub.add_parser("image", help="print an image (Pillow)")
    s.add_argument("file")
    s.add_argument("--width", type=int, default=DOTS, help="width in dots (max 168)")
    s.add_argument("--threshold", type=int, help="0-255 threshold instead of dithering")
    s.add_argument("--no-dither", action="store_true", help="threshold 128, no dithering")
    s.add_argument("--invert", action="store_true")
    s.add_argument("--rotate", action="store_true", help="rotate 90° (image along the paper)")
    s.add_argument("--aspect", type=float, help="line-spacing correction (default config, 1.5)")
    s.set_defaults(func=cmd_image)

    s = sub.add_parser("bars", help="bar chart: label=value")
    s.add_argument("items", nargs="*", help='e.g. "63 Hz=72.5"')
    s.add_argument("--csv", help="file of label;value or label,value lines")
    s.add_argument("--min", type=float)
    s.add_argument("--max", type=float)
    s.add_argument("--unit", default="")
    s.add_argument("--title")
    s.add_argument("--style", choices=["filled", "hatched", "outline"], default="filled")
    s.add_argument("--narrow", action="store_true")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--native", action="store_true", help="native bars (scale_px calibrated)")
    g.add_argument("--dots", action="store_true", help="bars drawn dot by dot")
    s.set_defaults(func=cmd_bars)

    s = sub.add_parser("strip", help="strip-chart recorder (one value per line)")
    s.add_argument("file", nargs="?", help="file of values (default: standard input)")
    s.add_argument("--min", type=float, help="with --max: live printing")
    s.add_argument("--max", type=float)
    s.add_argument("--grid", type=int, default=10, help="grid line every N values")
    s.add_argument("--label-every", type=int, default=0, help="value printed every N lines")
    s.set_defaults(func=cmd_strip)

    s = sub.add_parser("calibrate", help="calibration page (native scale, line spacing)")
    s.set_defaults(func=lambda a, c: run_job(a, c, calibration_page))

    s = sub.add_parser("glyph", help="custom DRC characters: list, add, del, test")
    s.add_argument("action", choices=["list", "add", "del", "test"])
    s.add_argument("char", nargs="?")
    s.add_argument("rows", nargs="*", help="7 rows of 5 or 7 characters (# = dot)")
    s.set_defaults(func=cmd_glyph)

    s = sub.add_parser("template", help="print a text template (templates folder)",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog=TEMPLATE_HELP)
    s.add_argument("file", nargs="?", help="template file, or its name in the templates folder")
    s.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="field value (repeatable); missing fields are asked")
    s.add_argument("--list", action="store_true", help="list templates and their fields")
    s.add_argument("--import", dest="import_", metavar="FILE",
                   help="check a template and copy it into the templates folder")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("raw", help="raw bytes: ESC 28 4D t:ABC CR")
    s.add_argument("tokens", nargs="+")
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser("send", help="send a binary file")
    s.add_argument("file")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("type", help="typewriter: prints every line typed")
    s.set_defaults(func=cmd_type)

    s = sub.add_parser("diag", help="cable and link diagnostic")
    s.add_argument("--no-print", action="store_true", help="do not print a test line")
    s.set_defaults(func=cmd_diag)

    s = sub.add_parser("ports", help="list serial ports")
    s.set_defaults(func=cmd_ports)

    s = sub.add_parser("config", help="show or change the settings")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("gui", help="graphical interface")
    s.set_defaults(func=lambda a, c: run_gui())
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_config()
    if not args.cmd:
        return run_gui()
    try:
        args.func(args, cfg)
    except ImportError as e:
        sys.exit(f"Missing module: {PACKAGES.get(e.name, e.name)}.\n"
                 f"Install the dependencies with:\n  {pip_command()}")
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"Error: {e}")


# =============================================================================
# Graphical interface
# =============================================================================

LED ={"off": "#9e9e9e", "red": "#d32f2f", "green": "#2e7d32", "orange": "#f9a825"}


def run_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox, scrolledtext
        import tkinter.font as tkfont
    except ImportError:
        sys.exit("Tkinter missing (Linux: sudo apt install python3-tk). "
                 "The command line is still available: python b2318.py -h")
    import webbrowser

    ensure_templates()
    cfg = load_config()
    root = tk.Tk()
    root.title(f"B&K 2318 Printer - {APP_NAME} {__version__}")
    root.minsize(860, 620)
    mono = ("Courier", 11)
    V = {}

    def var(name, value, kind=tk.StringVar):
        V[name] = kind(value=value)
        return V[name]

    class Tooltip:
        def __init__(self, widget, text):
            self.widget, self.text, self.tip = widget, text, None
            widget.bind("<Enter>", self.show)
            widget.bind("<Leave>", self.hide)

        def show(self, _event=None):
            text = self.text() if callable(self.text) else self.text
            if not text:
                return
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            tk.Label(self.tip, text=text, background="#ffffe0", relief="solid", borderwidth=1,
                     padx=6, pady=3, justify="left").pack()
            self.tip.update_idletasks()
            x = self.widget.winfo_rootx() + self.widget.winfo_width() - self.tip.winfo_width()
            y = self.widget.winfo_rooty() - self.tip.winfo_height() - 4
            self.tip.wm_geometry(f"+{max(0, x)}+{max(0, y)}")

        def hide(self, _event=None):
            if self.tip:
                self.tip.destroy()
                self.tip = None

    # ---------- footer (dB Plotter style) ----------
    ttk.Separator(root).pack(side="bottom", fill="x")
    footer = ttk.Frame(root, padding=(8, 5, 8, 6))
    footer.pack(side="bottom", fill="x")
    footer.lift()

    pbar = tk.Canvas(footer, width=150, height=16, highlightthickness=0, background=root.cget("bg"))
    pbar.grid(row=0, column=0, padx=(0, 10))

    def set_progress(frac):
        pbar.delete("all")
        w, h = 150, 16
        pbar.create_rectangle(1, 1, w - 1, h - 1, outline="#9e9e9e", fill="#ffffff")
        fill = int((w - 4) * max(0.0, min(1.0, frac)))
        if fill > 0:
            pbar.create_rectangle(2, 2, 2 + fill, h - 2, outline="", fill="#1976d2")

    set_progress(0)
    ttk.Label(footer, text="Status:").grid(row=0, column=1, sticky="w")
    status = ttk.Label(footer, text="Ready", width=34, anchor="w")
    status.grid(row=0, column=2, sticky="ew", padx=(4, 12))
    footer.columnconfigure(2, weight=1)
    ttk.Label(footer, text="Remaining time:").grid(row=0, column=3, sticky="e")
    remaining = ttk.Label(footer, text="-", width=13, anchor="w")
    remaining.grid(row=0, column=4, sticky="w", padx=(4, 12))
    ttk.Label(footer, text="Port:").grid(row=0, column=5, sticky="e")
    port_lbl = ttk.Label(footer, text="-", width=9, anchor="w")
    port_lbl.grid(row=0, column=6, sticky="w", padx=(4, 6))
    leds = {}
    for i, name in enumerate(("link", "activity")):
        c = tk.Canvas(footer, width=16, height=16, highlightthickness=0, background=root.cget("bg"))
        c.grid(row=0, column=7 + i, padx=1)
        leds[name] = (c, c.create_oval(2, 2, 14, 14, fill=LED["off"], outline="#555"))
    icon = tk.Canvas(footer, width=26, height=20, highlightthickness=0, background=root.cget("bg"))
    icon.grid(row=0, column=9, padx=(6, 0))

    status_full = {"text": "Ready", "base": "Ready", "note_until": 0.0}

    def fit_status(_event=None):
        """Show the status on one line; if it does not fit, end it with an ellipsis
        (the full text is in the tooltip)."""
        text, width = status_full["text"], status.winfo_width()
        if width > 1:
            font = tkfont.nametofont("TkDefaultFont")
            if font.measure(text) > width:
                while text and font.measure(text + "…") > width:
                    text = text[:-1]
                text = text.rstrip(" ,:;(") + "…"
        status.configure(text=text)

    def show_status(text):
        status_full["text"] = text
        fit_status()

    def set_status(msg):
        """State of the application; waits if a notice is on screen."""
        status_full["base"] = msg
        if time.time() >= status_full["note_until"]:
            show_status(msg)

    def notify(msg, seconds=3.0):
        """Short notice shown for a few seconds, then the state comes back."""
        status_full["note_until"] = time.time() + seconds
        show_status(msg)

    def status_loop():
        if time.time() >= status_full["note_until"] and status_full["text"] != status_full["base"]:
            show_status(status_full["base"])
        root.after(200, status_loop)

    status.bind("<Configure>", fit_status)
    root.after(200, status_loop)

    def set_led(name, color):
        c, item = leds[name]
        c.itemconfigure(item, fill=LED[color])

    def draw_icon(connected):
        icon.delete("all")
        icon.create_rectangle(3, 2, 23, 14, outline="#37474f", width=2,
                              fill="#1976d2" if connected else "#cfd8dc")
        icon.create_line(13, 14, 13, 17, fill="#37474f", width=2)
        icon.create_line(7, 18, 19, 18, fill="#37474f", width=2)

    draw_icon(False)

    # ---------- connection ----------
    conn = ttk.LabelFrame(root, text="Connection", padding=6)
    conn.pack(fill="x", padx=8, pady=(8, 4))
    ports = [d for d, _ in list_ports()]
    var("port", cfg["port"] or (ports[0] if ports else ""))
    ttk.Label(conn, text="Port").grid(row=0, column=0, sticky="w")
    cb_port = ttk.Combobox(conn, textvariable=V["port"], values=ports, width=18)
    cb_port.grid(row=0, column=1, padx=4)

    def refresh_ports():
        found = list_ports()
        cb_port.configure(values=[d for d, _ in found])
        if found and "://" not in V["port"].get() and \
                V["port"].get() not in [d for d, _ in found]:
            V["port"].set(found[0][0])
        if found:
            set_status("Ports: " + ", ".join(f"{d} ({desc})" for d, desc in found))
        elif "pyserial" in " ".join(missing_packages()):
            set_status("pyserial missing: cannot list ports")
        else:
            set_status("No serial port found")

    ttk.Button(conn, text="Refresh", command=refresh_ports).grid(row=0, column=2)
    ttk.Label(conn, text="Baud").grid(row=0, column=3, padx=(12, 0))
    var("baud", str(cfg["baud"]))
    ttk.Combobox(conn, textvariable=V["baud"], values=BAUDS, width=7,
                 state="readonly").grid(row=0, column=4, padx=4)
    var("xon", cfg["xonxoff"], tk.BooleanVar)
    ttk.Checkbutton(conn, text="XON/XOFF", variable=V["xon"]).grid(row=0, column=5, padx=4)
    var("rts", cfg["rtscts"], tk.BooleanVar)
    ttk.Checkbutton(conn, text="RTS/CTS", variable=V["rts"]).grid(row=0, column=6, padx=4)
    var("sim", False, tk.BooleanVar)
    ttk.Checkbutton(conn, text="Simulation (no output)", variable=V["sim"]
                    ).grid(row=0, column=7, padx=4)
    ttk.Label(conn, text="Text").grid(row=1, column=0, sticky="w", pady=(6, 0))
    var("mode", cfg["mode"])
    ttk.Combobox(conn, textvariable=V["mode"], values=["auto", "ascii"], width=7,
                 state="readonly").grid(row=1, column=1, sticky="w", pady=(6, 0), padx=4)
    var("reset", True, tk.BooleanVar)
    ttk.Checkbutton(conn, text="Reset before each job", variable=V["reset"]
                    ).grid(row=1, column=2, columnspan=3, sticky="w", pady=(6, 0))
    ttk.Label(conn, text="Feed after").grid(row=1, column=5, sticky="e", pady=(6, 0))
    var("feed", "3")
    ttk.Spinbox(conn, from_=0, to=10, textvariable=V["feed"], width=4
                ).grid(row=1, column=6, sticky="w", pady=(6, 0), padx=4)
    btns = ttk.Frame(conn)
    btns.grid(row=1, column=7, sticky="w", pady=(6, 0), padx=4)
    btn_connect = ttk.Button(btns, text="Connect", width=11, command=lambda: toggle_connect())
    btn_connect.pack(side="left")
    ttk.Button(btns, text="Cable diagnostic", command=lambda: do_diag()).pack(side="left", padx=4)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=4)

    # ---------- connection state, LEDs ----------
    link_state = {"link": None, "params": None}
    flash = {"count": 0, "on": False}

    def params_now():
        return (V["port"].get().strip(), int(V["baud"].get()), V["xon"].get(), V["rts"].get())

    def update_indicators():
        link = link_state["link"]
        port_lbl.configure(text=V["port"].get().strip() or "-")
        set_led("link", "green" if link else "red")
        draw_icon(bool(link))
        btn_connect.configure(text="Disconnect" if link else "Connect")

    def connect():
        params = params_now()
        if not params[0]:
            messagebox.showerror("Port", "Choose a serial port.")
            return False
        disconnect()
        try:
            link = Link(*params)
        except ImportError:
            messagebox.showerror("Missing module", f"pyserial is missing.\n\n{pip_command()}")
            return False
        except Exception as e:
            set_status(f"Connection failed: {e}")
            update_indicators()
            return False
        link_state.update(link=link, params=params)
        save_settings()
        update_indicators()
        return True

    def disconnect():
        link = link_state["link"]
        if link:
            try:
                link.close()
            except Exception:
                pass
        link_state.update(link=None, params=None)
        update_indicators()

    def toggle_connect():
        if printer_busy():
            return
        disconnect() if link_state["link"] else connect()

    def led_loop():
        """Activity LED: one short red flash for each TX write or RX read."""
        link = link_state["link"]
        if link:
            try:
                link.poll_rx()
            except Exception:
                if not busy["on"]:
                    disconnect()
                    set_status("Connection lost")
                link = None
        if flash["on"]:
            flash["on"] = False                   # a flash lasts one tick, then a gap
        elif link and link.activity != flash["count"]:
            flash["count"] = link.activity
            flash["on"] = True
        set_led("activity", "red" if flash["on"] else "off")
        root.after(60, led_loop)

    Tooltip(leds["link"][0], lambda: "Port open" if link_state["link"] else "Port closed")
    Tooltip(leds["activity"][0], "TX/RX activity")
    Tooltip(status, lambda: status_full["text"]
            if status.cget("text") != status_full["text"] else "")
    Tooltip(icon, lambda: f"Connected to {link_state['params'][0]}" if link_state["link"]
            else "Not connected")

    # ---------- sending ----------
    busy = {"on": False}
    job = {"t0": None, "tx_done": False, "cancel": None}

    def save_settings():
        cfg.update(port=V["port"].get().strip(), baud=int(V["baud"].get()),
                   xonxoff=V["xon"].get(), rtscts=V["rts"].get(), mode=V["mode"].get())
        save_config(cfg)

    def make(cls=Printer):
        return cls(mode=V["mode"].get(), prefer=cfg["prefer"], glyphs=cfg["glyphs"],
                   scale_px=cfg["scale_px"], aspect=cfg["aspect"],
                   title_style=cfg.get("title_style", "wide-inverse"))

    def preview_widget(parent):
        w = tk.Text(parent, width=28, height=22, font=mono, state="disabled", wrap="none",
                    background="#fbfaf5", relief="flat")
        w.tag_configure("inv", background="#222222", foreground="#ffffff")
        return w

    def show_preview(widget, out):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", "+" + "-" * 24 + "+\n")
        for s, inv in out:
            s = s[:24]
            widget.insert("end", "|")
            widget.insert("end", s, ("inv",) if inv else ())
            widget.insert("end", " " * (24 - len(s)) + "|\n")
        widget.insert("end", "+" + "-" * 24 + "+")
        widget.configure(state="disabled")

    def printer_busy():
        """True while a job is being sent or is still being printed (estimate)."""
        if busy["on"] or job["t0"] is not None:
            root.bell()
            notify("Printer busy, please wait")
            return True
        return False

    def submit(build, label="Print", reset=None, feed=None):
        if printer_busy():
            return
        p = make()
        try:
            if V["reset"].get() if reset is None else reset:
                p.soft_reset()
            build(p)
            n = int(V["feed"].get() or 0) if feed is None else feed
            if n:
                p.feed(n)
        except ImportError as e:
            messagebox.showerror("Missing module", f"{PACKAGES.get(e.name, e.name)} is missing.\n\n"
                                 f"Command:\n{pip_command()}")
            return
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        data = p.take()
        save_settings()
        if V["sim"].get():
            log(f"--- {label}: {len(data)} bytes (simulation) ---\n{hexdump(data)}\n")
            set_status(f"Simulated: {len(data)} bytes, see Tools")
            return
        if link_state["link"] is None or link_state["params"] != params_now():
            if not connect():
                return
        link = link_state["link"]
        busy["on"] = True
        btn_stop.configure(state="normal")
        cancel = threading.Event()
        now = time.time()
        job.update(t0=now, t_last=now, size=len(data), sent=0, ends=line_ends(data),
                   printed=0.0, tx_done=False, cancel=cancel, label=label, held=False,
                   baud=link.baud, lps=float(cfg.get("lines_per_second") or LINES_PER_SECOND))
        set_status(f"{label}: sending...")

        def progress(n):
            job["sent"] = n

        def worker():
            sent = 0
            try:
                sent = link.send(data, progress=progress, cancel=cancel)
                if sent < len(data):
                    msg = f"Stopped: {len(data) - sent} bytes not sent"
                else:
                    msg = "Transmission complete."
                ok = True
            except Exception as e:
                msg, ok = f"Error: {e}", False
            root.after(0, finish, msg, ok, sent)

        threading.Thread(target=worker, daemon=True).start()

    def finish(msg, ok, sent):
        busy["on"] = False
        job["tx_done"] = True
        job["sent"] = job["size"] = sent              # after Stop, only what really went out
        job["ends"] = [e for e in job["ends"] if e < sent]
        if not ok:
            job["t0"] = None
            set_progress(0)
            remaining.configure(text="-")
            btn_stop.configure(state="disabled")
            disconnect()
        set_status(msg)

    def stop():
        if busy["on"]:
            if job["cancel"] is not None:
                job["cancel"].set()               # stops sending; finish() follows
        elif job["t0"] is not None:               # only waiting for the paper: unlock
            job["t0"] = None
            set_progress(0)
            remaining.configure(text="-")
            btn_stop.configure(state="disabled")
            set_status("Stopped, printer empties its buffer")

    def tick():
        """Remaining time from what really happened: bytes actually sent (measured
        rate) and a model of the printer printing the received lines."""
        if job["t0"] is not None:
            now = time.time()
            elapsed = now - job["t0"]
            lps = job["lps"]
            sent = job["sent"]
            # the printer prints the lines it has received, at its own pace
            received = bisect.bisect_left(job["ends"], sent)
            job["printed"] = min(received, job["printed"] + (now - job["t_last"]) * lps)
            job["t_last"] = now
            if job["tx_done"]:
                per_byte = 0.0
            elif elapsed > 1.5 and sent > 32:
                per_byte = elapsed / sent                             # measured rate
            else:
                per_byte = 0.003 if job["baud"] >= 9600 else 10 / job["baud"]
            # simulate the rest: each line is printed once it has arrived, one at a time
            rem = (received - job["printed"]) / lps
            for end in job["ends"][received:]:
                rem = max(rem, (end + 1 - sent) * per_byte) + 1 / lps
            link = link_state["link"]
            if not job["tx_done"] and link and busy["on"] and link.paused != job["held"]:
                job["held"] = link.paused
                set_status("Printer buffer full, waiting..." if link.paused
                           else f"{job['label']}: sending...")
            rem = max(0.0, rem)
            set_progress(elapsed / (elapsed + rem) if elapsed + rem > 0 else 1)
            remaining.configure(text=f"{rem:.2f} seconds")
            if job["tx_done"] and rem <= 0:
                job["t0"] = None
                set_progress(1)
                btn_stop.configure(state="disabled")
        root.after(200, tick)

    btn_stop = ttk.Button(btns, text="Stop", width=6, command=stop, state="disabled")
    btn_stop.pack(side="left")

    # ---------- Text tab ----------
    tab = ttk.Frame(nb, padding=8)
    nb.add(tab, text="Text")
    left = ttk.Frame(tab)
    left.pack(side="left", fill="both", expand=True)
    txt = scrolledtext.ScrolledText(left, width=42, height=14, font=mono, undo=True)
    txt.pack(fill="both", expand=True)
    txt.insert("1.0", "Hello!\nBrüel & Kjær 2318 + ZI 0054\nAccents: é è à ç ê â €")
    opts = ttk.Frame(left)
    opts.pack(fill="x", pady=6)
    var("wide", False, tk.BooleanVar)
    var("inv", False, tk.BooleanVar)
    var("align", "left")
    var("rawset", "(auto)")
    var("ttl", "")
    preview_box = ttk.LabelFrame(tab, text="Preview", padding=4)
    preview_box.pack(side="left", fill="y", padx=(8, 0))
    prev = preview_widget(preview_box)
    prev.pack(fill="y", expand=True)

    def text_job():
        content = txt.get("1.0", "end-1c")
        ttl, wide, inv = V["ttl"].get(), V["wide"].get(), V["inv"].get()
        align, rs = V["align"].get(), V["rawset"].get()

        def build(p):
            if ttl:
                p.title(ttl)
            if wide:
                p.wide()
            if inv:
                p.inverse()
            p.align(align)
            if rs != "(auto)":
                p.raw_charset(rs)
            p.text(content)
        return build

    def refresh(*_):
        p = make(PreviewPrinter)
        try:
            text_job()(p)
        except Exception as e:
            p.out = [(str(e), False)]
        show_preview(prev, p.out)

    ttk.Checkbutton(opts, text="Wide", variable=V["wide"], command=refresh).pack(side="left")
    ttk.Checkbutton(opts, text="Inverse", variable=V["inv"], command=refresh
                    ).pack(side="left", padx=6)
    ttk.Label(opts, text="Alignment").pack(side="left", padx=(6, 2))
    cb = ttk.Combobox(opts, textvariable=V["align"], values=["left", "center", "right"],
                      width=7, state="readonly")
    cb.pack(side="left")
    cb.bind("<<ComboboxSelected>>", refresh)
    ttk.Label(opts, text="Raw set").pack(side="left", padx=(10, 2))
    cb = ttk.Combobox(opts, textvariable=V["rawset"], values=["(auto)"] + TEXT_SETS,
                      width=7, state="readonly")
    cb.pack(side="left")
    cb.bind("<<ComboboxSelected>>", refresh)
    opts2 = ttk.Frame(left)
    opts2.pack(fill="x")
    ttk.Label(opts2, text="Banner").pack(side="left")
    e = ttk.Entry(opts2, textvariable=V["ttl"], width=14)
    e.pack(side="left", padx=4)
    e.bind("<KeyRelease>", refresh)
    var("tstyle", cfg.get("title_style", "wide-inverse"))

    def set_title_style(_event=None):
        cfg["title_style"] = V["tstyle"].get()
        save_config(cfg)
        refresh()
        tpl_refresh()

    cb = ttk.Combobox(opts2, textvariable=V["tstyle"], values=list(TITLE_STYLES), width=12,
                      state="readonly")
    cb.pack(side="left", padx=(4, 0))
    cb.bind("<<ComboboxSelected>>", set_title_style)

    ttk.Button(opts2, text="Print", command=lambda: submit(text_job(), "Text")).pack(side="right")
    ttk.Button(opts2, text="Paper feed",
               command=lambda: submit(lambda p: p.feed(3), "Paper feed", reset=False, feed=0)
               ).pack(side="right", padx=6)
    txt.bind("<KeyRelease>", refresh)
    refresh()

    # ---------- Templates tab ----------
    tab_tpl = tab = ttk.Frame(nb, padding=8)
    nb.add(tab, text="Templates")
    tpl_state = {"files": [], "tpl": None, "vars": {}, "after": None}
    leftf = ttk.Frame(tab)
    leftf.pack(side="left", fill="y")
    ttk.Label(leftf, text=f"Folder: {TEMPLATES_DIR.name}/").pack(anchor="w")
    lb = tk.Listbox(leftf, width=26, height=16, exportselection=False, activestyle="none")
    lb.pack(fill="y", expand=True, pady=(2, 4))
    row = ttk.Frame(leftf)
    row.pack(fill="x")
    ttk.Button(row, text="Import...", command=lambda: import_tpl()).pack(side="left")
    ttk.Button(row, text="Reload", command=lambda: reload_tpls()).pack(side="left", padx=4)
    ttk.Button(row, text="Open folder", command=lambda: open_folder()).pack(side="left")
    ttk.Button(leftf, text="Template format help", command=lambda: tpl_help()
               ).pack(fill="x", pady=(4, 0))
    midf = ttk.LabelFrame(tab, text="Fields", padding=8)
    midf.pack(side="left", fill="both", expand=True, padx=8)
    tpl_desc = ttk.Label(midf, text="Select a template", wraplength=260, foreground="#555",
                         justify="left")
    tpl_desc.pack(anchor="w", fill="x")
    fields_box = ttk.Frame(midf)
    fields_box.pack(fill="x", pady=8)
    ttk.Label(midf, text="Filled in automatically: {date} {time} {datetime}",
              foreground="#555").pack(anchor="w")
    ttk.Button(midf, text="Print template", command=lambda: print_tpl()
               ).pack(side="bottom", fill="x")
    tprevf = ttk.LabelFrame(tab, text="Preview", padding=4)
    tprevf.pack(side="right", fill="y", before=midf)      # keeps its width when space is short
    tprev = preview_widget(tprevf)
    tprev.pack(fill="y", expand=True)

    def tpl_values():
        return {k: v.get() for k, v in tpl_state["vars"].items()}

    def tpl_refresh(*_):
        tpl_state["after"] = None
        tpl = tpl_state["tpl"]
        p = make(PreviewPrinter)
        if tpl is not None:
            try:
                tpl.render(p, tpl_values())
            except Exception as e:
                msg = f"Error: {e}"
                p.out += [(msg[i:i + 24], False) for i in range(0, len(msg), 24)]
        show_preview(tprev, p.out)

    def schedule_refresh(*_):
        if tpl_state["after"]:
            root.after_cancel(tpl_state["after"])
        tpl_state["after"] = root.after(150, tpl_refresh)

    def select_tpl(*_):
        sel = lb.curselection()
        for w in fields_box.winfo_children():
            w.destroy()
        tpl_state.update(tpl=None, vars={})
        if not sel or sel[0] >= len(tpl_state["files"]):
            tpl_refresh()
            return
        path = tpl_state["files"][sel[0]]
        try:
            tpl = Template.load(path)
        except Exception as e:
            tpl_desc.configure(text=f"{path.name}: {e}", foreground="#c62828")
            tpl_refresh()
            return
        tpl_state["tpl"] = tpl
        tpl_desc.configure(text=tpl.description or path.name, foreground="#555")
        saved = cfg.setdefault("template_values", {}).get(path.name, {})
        for i, (name, f) in enumerate(tpl.fields.items()):
            ttk.Label(fields_box, text=f["label"]).grid(row=i, column=0, sticky="w",
                                                        pady=2, padx=(0, 8))
            sv = tk.StringVar(value=saved.get(name, f["default"]))
            sv.trace_add("write", schedule_refresh)
            ttk.Entry(fields_box, textvariable=sv, width=26).grid(row=i, column=1,
                                                                  sticky="ew", pady=2)
            tpl_state["vars"][name] = sv
        if not tpl.fields:
            ttk.Label(fields_box, text="(no field to fill in)").grid(row=0, column=0, sticky="w")
        fields_box.columnconfigure(1, weight=1)
        tpl_refresh()

    def reload_tpls(select=None):
        files = list_templates()
        tpl_state["files"] = files
        lb.delete(0, "end")
        for f in files:
            lb.insert("end", f.stem)
        if files:
            names = [f.name for f in files]
            idx = names.index(select) if select in names else 0
            lb.selection_clear(0, "end")
            lb.selection_set(idx)
            lb.see(idx)
        else:
            tpl_desc.configure(text="No template yet: use Import...", foreground="#555")
        select_tpl()

    def import_tpl():
        paths = filedialog.askopenfilenames(
            title="Import templates",
            filetypes=[("Templates", "*.txt *.b2318"), ("All files", "*.*")])
        last = None
        for src in paths:
            dest = TEMPLATES_DIR / Path(src).name
            if dest.exists() and Path(src).resolve() != dest.resolve() and \
                    not messagebox.askyesno("Import", f"{dest.name} already exists. Replace it?"):
                continue
            try:
                last = import_template(src).name
            except Exception as e:
                messagebox.showerror("Import", f"{Path(src).name}: {e}")
        if last:
            nb.select(tab_tpl)
            reload_tpls(last)
            set_status(f"Template imported: {last}")

    def open_folder():
        TEMPLATES_DIR.mkdir(exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(TEMPLATES_DIR)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(TEMPLATES_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(TEMPLATES_DIR)])
        except Exception as e:
            messagebox.showerror("Folder", str(e))

    def tpl_help():
        win = tk.Toplevel(root)
        win.title("Template format")
        t = tk.Text(win, width=80, height=28, font=("Courier", 10))
        t.insert("1.0", TEMPLATE_HELP)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True, padx=8, pady=8)

    def print_tpl():
        tpl = tpl_state["tpl"]
        if tpl is None:
            messagebox.showinfo("Template", "Select a template first.")
            return
        values = tpl_values()
        cfg.setdefault("template_values", {})[tpl.path.name] = values
        save_config(cfg)
        submit(lambda p: tpl.render(p, values), f"Template {tpl.name}")

    lb.bind("<<ListboxSelect>>", select_tpl)
    reload_tpls()

    # ---------- Charts tab ----------
    tab = ttk.Frame(nb, padding=8)
    nb.add(tab, text="Charts")
    fr = ttk.LabelFrame(tab, text="Bar chart: one line per bar, label=value", padding=6)
    fr.pack(fill="both", expand=True)
    gtxt = scrolledtext.ScrolledText(fr, width=30, height=9, font=mono)
    gtxt.pack(side="left", fill="both", expand=True)
    gtxt.insert("1.0", "63 Hz=72.5\n125 Hz=68\n250 Hz=61.2\n500 Hz=58\n1 kHz=55.4\n"
                       "2 kHz=50\n4 kHz=47.5\n8 kHz=42")
    gopt = ttk.Frame(fr, padding=(10, 0))
    gopt.pack(side="left", fill="y")
    for i, (key, lab, val) in enumerate([("gtitle", "Title", ""), ("gmin", "Scale min", ""),
                                         ("gmax", "Scale max", ""), ("gunit", "Unit", "")]):
        ttk.Label(gopt, text=lab).grid(row=i, column=0, sticky="w", pady=2)
        ttk.Entry(gopt, textvariable=var(key, val), width=12).grid(row=i, column=1, pady=2)
    for i, (key, lab, vals) in enumerate([("gstyle", "Style", ["filled", "hatched", "outline"]),
                                          ("gsize", "Size", ["wide", "narrow"]),
                                          ("grender", "Rendering", ["auto", "dots", "native"])], 4):
        ttk.Label(gopt, text=lab).grid(row=i, column=0, sticky="w", pady=2)
        ttk.Combobox(gopt, textvariable=var(key, vals[0]), values=vals, width=10,
                     state="readonly").grid(row=i, column=1, pady=2)
    ttk.Label(gopt, text="Empty scale = auto (0 to max)", foreground="#555"
              ).grid(row=7, column=0, columnspan=2, sticky="w")

    def opt_float(key):
        s = V[key].get().strip().replace(",", ".")
        return float(s) if s else None

    def print_bars():
        try:
            items = parse_items(gtxt.get("1.0", "end-1c").splitlines())
            vmin, vmax = opt_float("gmin"), opt_float("gmax")
        except ValueError as e:
            messagebox.showerror("Values", str(e))
            return
        if not items:
            messagebox.showerror("Values", "No values to plot.")
            return
        native = {"auto": None, "dots": False, "native": True}[V["grender"].get()]
        ttl, unit = V["gtitle"].get(), V["gunit"].get()
        style, size = V["gstyle"].get(), V["gsize"].get()

        def build(p):
            if ttl:
                p.title(ttl)
            p.bar_chart(items, vmin, vmax, unit, size, style, native)
        submit(build, "Bar chart")

    ttk.Button(gopt, text="Print bar chart", command=print_bars
               ).grid(row=8, column=0, columnspan=2, pady=(10, 0), sticky="ew")

    fr = ttk.LabelFrame(tab, text="Lines", padding=6)
    fr.pack(fill="x", pady=(8, 0))
    var("ltype", "dashed")
    var("lrow", "4")
    ttk.Label(fr, text="Type").pack(side="left")
    ttk.Combobox(fr, textvariable=V["ltype"], values=list(LINE_TYPES), width=8,
                 state="readonly").pack(side="left", padx=4)
    ttk.Label(fr, text="Row (1 bottom, 7 top)").pack(side="left", padx=(8, 2))
    ttk.Spinbox(fr, from_=1, to=7, textvariable=V["lrow"], width=3).pack(side="left")
    ttk.Button(fr, text="Print a line",
               command=lambda: submit(lambda p: p.rule(V["ltype"].get(), int(V["lrow"].get())),
                                      "Line", feed=0)).pack(side="left", padx=8)
    ttk.Button(fr, text="All 4 types",
               command=lambda: submit(lambda p: [p.rule(k) for k in LINE_TYPES], "Lines")
               ).pack(side="left")

    fr = ttk.LabelFrame(tab, text="Printer calibration", padding=6)
    fr.pack(fill="x", pady=(8, 0))
    var("scale", "" if cfg["scale_px"] is None else str(cfg["scale_px"]))
    var("aspect", str(cfg["aspect"]))
    var("lps", str(cfg.get("lines_per_second") or LINES_PER_SECOND))
    ttk.Label(fr, text="Bar 1.0 =").pack(side="left")
    ttk.Entry(fr, textvariable=V["scale"], width=5).pack(side="left", padx=(4, 2))
    ttk.Label(fr, text="dots   Aspect").pack(side="left", padx=(0, 4))
    ttk.Entry(fr, textvariable=V["aspect"], width=5).pack(side="left")
    ttk.Label(fr, text="  Speed").pack(side="left", padx=(0, 4))
    ttk.Entry(fr, textvariable=V["lps"], width=5).pack(side="left")
    ttk.Label(fr, text="lines/s").pack(side="left", padx=(2, 0))

    def save_scale():
        try:
            s = V["scale"].get().strip()
            cfg["scale_px"] = int(s) if s else None
            cfg["aspect"] = float(V["aspect"].get().replace(",", "."))
            lps = float(V["lps"].get().replace(",", "."))
            if lps <= 0:
                raise ValueError
            cfg["lines_per_second"] = lps
        except ValueError:
            messagebox.showerror("Value", "Positive number expected")
            return
        save_config(cfg)
        set_status("Calibration saved")

    ttk.Button(fr, text="Save", command=save_scale).pack(side="left", padx=8)
    ttk.Button(fr, text="Calibration page",
               command=lambda: submit(calibration_page, "Calibration")).pack(side="left")

    # ---------- Image tab ----------
    tab_image = tab = ttk.Frame(nb, padding=8)
    nb.add(tab, text="Image")
    ctl = ttk.Frame(tab)
    ctl.pack(side="left", fill="y")
    var("img", "")
    var("iw", "168")
    var("dither", True, tk.BooleanVar)
    var("thr", 128, tk.IntVar)
    var("iinv", False, tk.BooleanVar)
    var("irot", False, tk.BooleanVar)
    img_label = ttk.Label(tab, text="No image", anchor="center", relief="groove")
    img_label.pack(side="left", fill="both", expand=True, padx=(10, 0))

    def image_opts():
        return dict(width=int(V["iw"].get()), dither=V["dither"].get(), threshold=V["thr"].get(),
                    invert=V["iinv"].get(), rotate=V["irot"].get(),
                    aspect=float(V["aspect"].get().replace(",", ".") or 1.5))

    def update_preview(*_):
        path = V["img"].get()
        if not path:
            return
        try:
            from PIL import Image, ImageTk
            p = make()
            bw = p.prepare_image(path, **image_opts())
            gap = max(0, round(7 * (image_opts()["aspect"] - 1)))
            w, h = bw.size
            bands = (h + 6) // 7
            sim = Image.new("L", (DOTS, bands * (7 + gap)), 255)
            off = (DOTS - w) // 2
            for b in range(bands):
                sim.paste(bw.crop((0, b * 7, w, min(h, b * 7 + 7))).convert("L"),
                          (off, b * (7 + gap)))
            sim = sim.resize((sim.width * 2, sim.height * 2))
            photo = ImageTk.PhotoImage(sim)
            img_label.configure(image=photo, text="")
            img_label.image = photo
            set_status(f"Image: {w}x{h} dots, {bands} lines, "
                       f"~{bands / LINES_PER_SECOND:.0f} s")
        except Exception as e:
            img_label.configure(image="", text=f"Preview not available: {e}")

    def pick():
        path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                                                     ("All files", "*.*")])
        if path:
            V["img"].set(path)
            update_preview()

    ttk.Button(ctl, text="Choose an image...", command=pick).pack(fill="x")
    ttk.Label(ctl, textvariable=V["img"], wraplength=220, foreground="#555").pack(fill="x", pady=4)
    row = ttk.Frame(ctl)
    row.pack(fill="x", pady=2)
    ttk.Label(row, text="Width (dots)").pack(side="left")
    ttk.Spinbox(row, from_=16, to=168, textvariable=V["iw"], width=5, command=update_preview
                ).pack(side="right")
    ttk.Checkbutton(ctl, text="Dithering (photos)", variable=V["dither"], command=update_preview
                    ).pack(anchor="w")
    ttk.Label(ctl, text="Threshold (no dithering)").pack(anchor="w")
    ttk.Scale(ctl, from_=0, to=255, variable=V["thr"], command=lambda _: update_preview()
              ).pack(fill="x")
    ttk.Checkbutton(ctl, text="Invert", variable=V["iinv"], command=update_preview).pack(anchor="w")
    ttk.Checkbutton(ctl, text="Rotate 90°", variable=V["irot"], command=update_preview
                    ).pack(anchor="w")
    ttk.Label(ctl, text="The preview shows the line spacing\n(aspect setting, Charts tab)",
              foreground="#555").pack(anchor="w", pady=4)

    def print_image():
        path = V["img"].get()
        if not path:
            messagebox.showinfo("Image", "Choose an image first.")
            return
        o = image_opts()
        submit(lambda p: p.image(path, **o), "Image")

    ttk.Button(ctl, text="Print image", command=print_image).pack(fill="x", pady=(6, 14))

    # ---------- Custom characters tab ----------
    tab = ttk.Frame(nb, padding=8)
    nb.add(tab, text="Custom characters")
    cell = 30
    grid = [[False] * 7 for _ in range(7)]
    canvas = tk.Canvas(tab, width=7 * cell + 1, height=7 * cell + 1, background="white",
                       highlightthickness=0)
    canvas.pack(side="left", anchor="n")
    paint = {"value": True}

    def draw():
        canvas.delete("all")
        for r in range(7):
            for c in range(7):
                fill = "#222" if grid[r][c] else ("#f0f0f0" if c in (0, 6) else "white")
                canvas.create_rectangle(c * cell, r * cell, (c + 1) * cell, (r + 1) * cell,
                                        fill=fill, outline="#bbb")

    def at(event):
        c, r = event.x // cell, event.y // cell
        return (r, c) if 0 <= r < 7 and 0 <= c < 7 else None

    def press(event):
        rc = at(event)
        if rc:
            paint["value"] = not grid[rc[0]][rc[1]]
            grid[rc[0]][rc[1]] = paint["value"]
            draw()

    def drag(event):
        rc = at(event)
        if rc and grid[rc[0]][rc[1]] != paint["value"]:
            grid[rc[0]][rc[1]] = paint["value"]
            draw()

    canvas.bind("<Button-1>", press)
    canvas.bind("<B1-Motion>", drag)
    draw()

    side = ttk.Frame(tab, padding=(12, 0))
    side.pack(side="left", fill="y")
    ttk.Label(side, text="Character it stands for (e.g. € ê ŵ ☺)").pack(anchor="w")
    var("gch", "€")

    def known():
        return sorted(set(BUILTIN_GLYPHS) | set(cfg["glyphs"]))

    cb_g = ttk.Combobox(side, textvariable=V["gch"], values=known(), width=6)
    cb_g.pack(anchor="w", pady=2)

    def rows_now():
        return ["".join("#" if grid[r][c] else "." for c in range(7)) for r in range(7)]

    def load_glyph(*_):
        rows = cfg["glyphs"].get(V["gch"].get()) or BUILTIN_GLYPHS.get(V["gch"].get())
        if not rows:
            return
        cols = parse_pattern(rows)
        for r in range(7):
            for c in range(7):
                grid[r][c] = bool(cols[c] & (1 << (6 - r)))
        draw()

    def clear():
        for r in range(7):
            grid[r] = [False] * 7
        draw()

    def save_glyph():
        ch = V["gch"].get()
        if len(ch) != 1:
            messagebox.showerror("Character", "A single character")
            return
        if ord(ch) < 0x7F:
            messagebox.showwarning("ASCII", "ASCII character: the built-in font will be used "
                                            "instead. Pick a symbol rather.")
        cfg["glyphs"][ch] = rows_now()
        save_config(cfg)
        cb_g.configure(values=known())
        set_status(f"Glyph {ch} saved")

    def delete_glyph():
        if cfg["glyphs"].pop(V["gch"].get(), None) is not None:
            save_config(cfg)
            cb_g.configure(values=known())
            set_status("Glyph deleted")

    def test_glyph():
        rows, ch = rows_now(), V["gch"].get() or "?"

        def build(p):
            code = p.define_char(0x21, rows)
            p.write(f"Test {ch if ord(ch[0]) < 0x7F else ''}: ")
            p._emit([(3, code, "drc", ch)] * 8)
            p.newline()
        submit(build, "Glyph test")

    cb_g.bind("<<ComboboxSelected>>", load_glyph)
    for lab, fn in (("Load", load_glyph), ("Clear grid", clear),
                    ("Save", save_glyph), ("Delete", delete_glyph),
                    ("Print a test", test_glyph)):
        ttk.Button(side, text=lab, command=fn).pack(fill="x", pady=2)
    ttk.Label(side, foreground="#555", wraplength=240, justify="left",
              text="Click or drag to draw. The greyed columns (1 and 7) are the space "
                   "between characters. Saved glyphs are sent automatically as DRC "
                   "whenever the character appears in a text."
              ).pack(anchor="w", pady=8)

    # ---------- Tools tab ----------
    tab = ttk.Frame(nb, padding=8)
    nb.add(tab, text="Tools")
    fr = ttk.LabelFrame(tab, text="Character sets and demo", padding=6)
    fr.pack(fill="x")
    var("set", "runes")
    ttk.Combobox(fr, textvariable=V["set"], values=TEXT_SETS + ["drc"], width=8,
                 state="readonly").pack(side="left")
    ttk.Button(fr, text="Print table",
               command=lambda: submit(lambda p: p.sample(V["set"].get()), "Table")
               ).pack(side="left", padx=4)
    ttk.Button(fr, text="All sets",
               command=lambda: submit(lambda p: [p.sample(n).feed(1) for n in TEXT_SETS],
                                      "Tables")).pack(side="left", padx=4)
    ttk.Button(fr, text="Demo page", command=lambda: submit(demo_page, "Demo")
               ).pack(side="left", padx=4)
    ttk.Button(fr, text="Banner styles", command=lambda: submit(banner_samples, "Banners")
               ).pack(side="left", padx=4)

    fr = ttk.LabelFrame(tab, text="Commands", padding=6)
    fr.pack(fill="x", pady=(8, 0))
    ttk.Button(fr, text="Reset (NSR)",
               command=lambda: submit(lambda p: p.reset(), "Reset", reset=False, feed=0)
               ).pack(side="left")
    ttk.Button(fr, text="EOT (wait for end)",
               command=lambda: submit(lambda p: p.eot(), "EOT", reset=False, feed=0)
               ).pack(side="left", padx=4)
    var("raw", "ESC 28 4D t:ABC CR")
    ttk.Label(fr, text="Raw bytes").pack(side="left", padx=(12, 2))
    ttk.Entry(fr, textvariable=V["raw"], width=28).pack(side="left")

    def send_raw():
        try:
            data = parse_raw(V["raw"].get())
        except Exception as e:
            messagebox.showerror("Raw bytes", str(e))
            return
        submit(lambda p: p.raw(data), "Raw", reset=False, feed=0)

    ttk.Button(fr, text="Send", command=send_raw).pack(side="left", padx=4)

    fr = ttk.LabelFrame(tab, text="Log (diagnostic, simulation)", padding=6)
    fr.pack(fill="both", expand=True, pady=(8, 0))
    logbox = scrolledtext.ScrolledText(fr, height=10, font=("Courier", 10))
    logbox.pack(fill="both", expand=True)
    tab_tools = tab

    def log(msg):
        logbox.insert("end", msg + "\n")
        logbox.see("end")

    def do_diag():
        if printer_busy():
            return
        port, baud = V["port"].get().strip(), int(V["baud"].get())
        if not port:
            messagebox.showerror("Port", "Choose a serial port.")
            return
        disconnect()                                  # the diagnostic opens the port itself
        nb.select(tab_tools)
        log("--- Cable diagnostic ---")
        set_status("Diagnostic running...")

        def worker():
            try:
                out = diagnose(port, baud)
            except Exception as e:
                out = f"Error: {e}"
            root.after(0, lambda: (log(out), set_status("Diagnostic finished")))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- menu bar ----------
    def about():
        win = tk.Toplevel(root)
        win.title("About")
        win.resizable(False, False)
        win.transient(root)
        ttk.Label(win, text=f"{APP_NAME} {__version__}", font=("Segoe UI", 12, "bold")
                  ).pack(padx=24, pady=(16, 4))
        ttk.Label(win, justify="center",
                  text="Driver for the Brüel & Kjær 2318 graphics printer\n"
                       "with the ZI 0054 serial interface module."
                  ).pack(padx=24)
        link = tk.Label(win, text=PROJECT_URL, foreground="#1565c0", cursor="hand2",
                        font=("Segoe UI", 9, "underline"))
        link.pack(padx=24, pady=10)
        link.bind("<Button-1>", lambda _e: webbrowser.open(PROJECT_URL))
        ttk.Button(win, text="OK", command=win.destroy).pack(pady=(0, 14))
        win.grab_set()

    def on_close():
        stop()
        disconnect()
        root.destroy()

    menubar = tk.Menu(root)
    m = tk.Menu(menubar, tearoff=0)
    m.add_command(label="Import template...", command=lambda: (nb.select(tab_tpl), import_tpl()))
    m.add_command(label="Open image...", command=lambda: (nb.select(tab_image), pick()))
    m.add_command(label="Print demo page", command=lambda: submit(demo_page, "Demo"))
    m.add_command(label="Print calibration page",
                  command=lambda: submit(calibration_page, "Calibration"))
    m.add_separator()
    m.add_command(label="Exit", command=on_close)
    menubar.add_cascade(label="File", menu=m)
    m = tk.Menu(menubar, tearoff=0)
    m.add_command(label="Connect", command=lambda: connect())
    m.add_command(label="Disconnect", command=lambda: disconnect())
    m.add_command(label="Refresh ports", command=refresh_ports)
    m.add_separator()
    m.add_command(label="Cable diagnostic", command=lambda: do_diag())
    menubar.add_cascade(label="Connection", menu=m)
    m = tk.Menu(menubar, tearoff=0)
    m.add_command(label="About", command=about)
    menubar.add_cascade(label="Help", menu=m)
    root.configure(menu=menubar)
    root.protocol("WM_DELETE_WINDOW", on_close)
    V["port"].trace_add("write", lambda *_: update_indicators())

    # ---------- dependencies ----------
    def check_dependencies():
        missing = missing_packages()
        if not missing:
            return
        serial_missing = any(m.startswith("pyserial") for m in missing)
        msg = "Missing Python modules:\n  " + "\n  ".join(missing) + "\n\n"
        if serial_missing:
            msg += "pyserial is required to talk to the printer.\n"
        else:
            msg += "Only image printing needs it.\n"
        msg += f"\nEquivalent command:\n{pip_command()}\n\nInstall them now?"
        if not messagebox.askyesno("Dependencies", msg):
            if serial_missing:
                V["sim"].set(True)
                set_status("pyserial missing: simulation only")
            return
        set_status("Installing dependencies...")
        root.configure(cursor="watch")

        def worker():
            ok, out = install_packages()
            root.after(0, done, ok, out)

        def done(ok, out):
            root.configure(cursor="")
            still = missing_packages()
            if ok and not still:
                set_status("Dependencies installed")
                refresh_ports()
            elif ok:
                messagebox.showinfo("Dependencies", "Installation finished. Restart the program "
                                                    "for it to take effect.")
            else:
                messagebox.showerror("Installation failed", out)
                set_status("Dependency installation failed")

        threading.Thread(target=worker, daemon=True).start()

    update_indicators()
    root.after(300, check_dependencies)
    root.after(60, led_loop)
    root.after(200, tick)
    root.mainloop()


if __name__ == "__main__":
    main()
