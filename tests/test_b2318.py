"""Byte-level tests for b2318.py. The expected bytes come from the examples in the
B&K 2318 + ZI 0054 instruction manual wherever the manual gives one."""
import pytest

import b2318 as b
from b2318 import Printer


def out(build, **kw):
    p = Printer(**kw)
    build(p)
    return p.take()


# ---------- manual examples ----------

@pytest.mark.parametrize("x, expected", [(0.5996, b"\x66\x58"), (0.5, b"\x60\x40")])
def test_relative_length(x, expected):
    assert b.rel_length(x) == expected


@pytest.mark.parametrize("build, expected", [
    (lambda p: p.hline(0.5, "full", 7), b"\x0e\x28\x47\x60\x40"),        # LINE example
    (lambda p: p.rule("dotdash", 4, newline=False), b"\x0e\x28\x7c"),     # LLINE example
    (lambda p: p.skip_chars(2), b"\x0e\x29\x42"),                         # CMRC example
    (lambda p: p.bar(0.5996), b"\x0e\x30\x66\x58"),                       # BWF example
    (lambda p: p.move(70), b"\x0e\x24\x7f\x24\x47"),                      # 63 + 7 dots
    (lambda p: p.move(-3), b"\x0e\x25\x43"),
    (lambda p: p.designate(0, "ru"), b"\x1b\x28\x4c"),                    # Russian example
    (lambda p: p.designate(1, "gr"), b"\x1b\x29\x4b"),                    # Greek into G1
    (lambda p: p.designate(1, "runes"), b"\x1b\x29\x4d"),                 # FrodeM's finding
    (lambda p: p.reset().eot(), b"\x1f\x04"),
])
def test_commands(build, expected):
    assert out(build) == expected


def test_dot_columns_arrow_from_manual():
    assert out(lambda p: p.dots(0x10, 0x20, 0x7F, 0x20, 0x10)) == bytes(
        [0x0E, 0x2A, 0x48, 0x40, 0x2A, 0x50, 0x40, 0x2A, 0x7F, 0x60,
         0x2A, 0x50, 0x40, 0x2A, 0x48, 0x40])


# ---------- text encoding ----------

@pytest.mark.parametrize("text, expected", [
    ("Hello", b"Hello\r"),
    ("café", b"caf\x1b\x2a\x45\x19\x7b\r"),                     # é from the French set, SS2
    ("été", b"\x1b\x2a\x45\x19\x7bt\x19\x7b\r"),
    ("ПРИВЕТ", b"\x1b\x2a\x4c\x1b\x6e\x70\x72\x69\x77\x65\x74\r"),  # KOI-7 via LS2
    ("ガ", b"\x1b\x2a\x4a\x1b\x6e\x36\x5e\r"),                    # カ + dakuten
    ("Σ", b"\x19\x72\r"),                                         # Greek is in G2 at power-up
    ("x\x1fy\x0e", b"xy\r"),                                      # control bytes filtered out
    ("A" * 30, b"A" * 24 + b"\r" + b"A" * 6 + b"\r"),
])
def test_line(text, expected):
    assert out(lambda p: p.line(text)) == expected


def test_word_wrap():
    assert out(lambda p: p.line("This sentence is long enough to be wrapped")) == \
        b"This sentence is long\renough to be wrapped\r"


def test_ascii_mode_transliterates():
    assert out(lambda p: p.line("café ê €\x1f\x9f"), mode="ascii") == b"cafe e EUR\r"


def test_drc_glyph_defined_once():
    d = out(lambda p: p.line("ê ê"))
    assert d.startswith(b"\x1bC\x7e") and d.count(b"\x1bC") == 1
    assert d.endswith(b"\x1d\x7e \x1d\x7e\r")


@pytest.mark.parametrize("build, expected", [
    (lambda p: p.wide().line("ABCDEFGHIJKLMNOP"), b"\x1bKABCDEFGHIJKL\rMNOP\r"),
    (lambda p: p.align("right").line("right"), b"\x1b&>right\r"),
    (lambda p: p.align("center").line("center"), b" " * 9 + b"center\r"),
    (lambda p: (p.align("right").line("a"), p.rule("full")), b"\x1b&>a\r\x1b&<\x0e\x28\x64\r"),
    (lambda p: (p.rule("full"), p.line("ok")), b"\x0e\x28\x64\r\x0fok\r"),
    (lambda p: (p.raw_charset("runes"), p.line("ABC"), p.raw_charset(None), p.line("D")),
     b"\x1b(MABC\r\x1b(BD\r"),
    (lambda p: p.kv("Total", "42"), b"Total" + b" " * 17 + b"42\r"),
    (lambda p: p.title("TITLE"), b"\x1bK\x1bH   TITLE    \r\x1bL\x1bI"),
    (lambda p: p.table([["F", "dB"], ["1k", "94"], ["250", "88.5"]]),
     b"F     dB\r1k    94\r250 88.5\r"),
])
def test_layout(build, expected):
    assert out(build) == expected


def test_sample_table():
    d = out(lambda p: p.sample("runes"))
    assert b"\x1b*M" in d and b"40 \x1bn@ABCDEFGHIJKLMNO\r" in d


# ---------- DRC and graphics ----------

def test_define_char():
    p = Printer()
    code = p.define_char(0x23, [".......", ".....##", "......#", ".....#.", "....#..",
                                "...#...", "......."])
    assert code == 0x23 and p.take()[:3] == b"\x1bC#"
    cols = b.parse_pattern(b.BUILTIN_GLYPHS["€"])
    assert len(cols) == 7 and cols[0] == cols[6] == 0


def test_band_skips_blank_columns():
    assert out(lambda p: p.band([0] * 10 + [0x7F] + [0] * 5)) == b"\x0e\x24\x4a\x2a\x7f\x60\r"


def test_bar_chart():
    assert out(lambda p: p.bar_chart([("63 Hz", 72), ("125", 36)])).count(b"\r") == 4
    assert b"\x0e\x30" in out(lambda p: p.bar_chart([("a", 10)], native=True, axis=False),
                              scale_px=160)
    # min and max entered the wrong way round still give bars
    d = out(lambda p: p.bar_chart([("a", 50)], vmin=100, vmax=0, axis=False))
    assert d.count(b"\x2a") > 10


def test_strip_chart():
    p = Printer()
    prev = None
    for i, v in enumerate([0, 5, 10, 3]):
        prev = p.strip_point(v, 0, 10, prev, i)
    assert p.take().count(b"\r") == 4


def test_image():
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    im = Image.new("L", (300, 150), 255)
    ImageDraw.Draw(im).ellipse((20, 10, 280, 140), fill=0)
    p = Printer()
    bw = p.prepare_image(im, dither=False)
    assert bw.size == (168, round(150 * 168 / 300 / 1.5))
    p.bitmap(bw)
    assert p.take().count(b"\r") == (bw.size[1] + 6) // 7


@pytest.mark.parametrize("page", [b.demo_page, b.calibration_page])
def test_full_pages_are_7_bit(page):
    p = Printer()
    p.reset()
    page(p)
    assert all(x < 0x80 for x in p.take())


# ---------- templates ----------

TEMPLATE = r"""# desc
@field who "Operator" "VD"
@title T
@kv Date | {date}
@kv Who | {who}
@kv | | .
Price {{x}}: {price}
@rule dotted 2
@table
A | 1
@end
@bars unit=dB
a = 10
@end
@charset runes
ABC
@charset auto
\@literal
"""


def test_template_render():
    t = b.Template(TEMPLATE)
    assert t.description == "desc" and list(t.fields) == ["who", "price"]
    assert t.fields["who"] == {"label": "Operator", "default": "VD"}
    d = t.render(Printer(), {"price": "12"}).take()
    for part in (b"VD", b"Price {x}: 12\r", b"\x1b(MABC\r", b"@literal\r",
                 b"." * 24 + b"\r", b"\x0e\x28\x6a\r"):
        assert part in d


@pytest.mark.parametrize("bad", ["@foo", "@table\nx", "@end", "@bars\na=1"])
def test_template_syntax_errors(bad):
    with pytest.raises(ValueError):
        b.Template(bad)


def test_template_preview():
    pp = b.Template(TEMPLATE).render(b.PreviewPrinter(), {"price": "12"})
    assert pp.out[0] == (" " * 10 + "T " + " " * 12, True)


@pytest.mark.parametrize("path", b.list_templates(), ids=lambda p: p.name)
def test_bundled_templates(path):
    t = b.Template.load(path)
    d = t.render(Printer()).take()
    assert d and all(x < 0x80 for x in d)
    t.render(b.PreviewPrinter())


# ---------- misc ----------

def test_parse_raw():
    assert b.parse_raw('ESC 28 4D t:AB "x y" 0x0D0A CR') == b"\x1b(MABx y\r\n\r"


def test_estimate_seconds():
    # 8 short lines: printing dominates, 1.25 s per line
    assert b.estimate_seconds(b"A\r" * 8, 1200) == pytest.approx(10.0, abs=0.05)
    # 600 bytes for one line: transmission dominates (5 s), then the line prints
    assert b.estimate_seconds(b"A" * 599 + b"\r", 1200) == pytest.approx(6.25)


# ---------- serial pacing (POSIX pseudo-terminal, skipped on Windows) ----------

def _pty_link(baud):
    os = pytest.importorskip("os")
    if not hasattr(os, "openpty"):
        pytest.skip("needs a pseudo-terminal")
    pytest.importorskip("serial")
    import threading
    import tty
    master, slave = os.openpty()
    tty.setraw(slave)
    got = bytearray()

    def reader():
        while True:
            try:
                got.extend(os.read(master, 4096))
            except OSError:
                break

    threading.Thread(target=reader, daemon=True).start()
    return b.Link(os.ttyname(slave), baud), master, got


def test_send_is_paced_at_the_baud_rate():
    import time
    link, master, got = _pty_link(1200)
    sent = []
    t0 = time.perf_counter()
    assert link.send(b"A" * 48, progress=sent.append) == 48
    elapsed = time.perf_counter() - t0
    time.sleep(0.05)
    link.close()
    assert 0.38 <= elapsed < 0.8               # 48 bytes x 10 bits / 1200 baud = 0.4 s
    assert bytes(got) == b"A" * 48 and sent[-1] == 48
    assert link.activity == 3          # one count per 16-byte write


def test_send_spaces_bytes_at_9600():
    import time
    link, master, got = _pty_link(9600)
    t0 = time.perf_counter()
    link.send(b"B" * 20)
    elapsed = time.perf_counter() - t0
    link.close()
    assert elapsed >= 0.057                    # 3 ms between start bits


def test_send_waits_for_xon():
    import os
    import threading
    import time
    link, master, got = _pty_link(1200)
    os.write(master, b"\x13")                  # XOFF: printer buffer full
    time.sleep(0.05)
    threading.Timer(0.5, lambda: os.write(master, b"\x11")).start()
    t0 = time.perf_counter()
    assert link.send(b"C" * 8) == 8
    assert time.perf_counter() - t0 >= 0.45
    stop = threading.Event()
    os.write(master, b"\x13")
    time.sleep(0.05)
    threading.Timer(0.2, stop.set).start()
    assert link.send(b"D" * 8, cancel=stop) == 0
    link.close()


# ---------- settings file ----------

def test_settings_next_to_the_program(monkeypatch):
    monkeypatch.delenv("B2318_CONFIG", raising=False)
    assert b._config_path() == b.APP_DIR / "bk2318-printer.json"


def test_old_settings_are_moved(monkeypatch, tmp_path):
    import json
    monkeypatch.delenv("B2318_CONFIG", raising=False)
    new, old = tmp_path / "bk2318-printer.json", tmp_path / ".b2318.json"
    old.write_text(json.dumps({"port": "COM4", "baud": 4800}), encoding="utf-8")
    monkeypatch.setattr(b, "CONFIG_PATH", new)
    monkeypatch.setattr(b, "LEGACY_CONFIG", old)
    cfg = b.load_config()
    assert cfg["port"] == "COM4" and cfg["baud"] == 4800 and cfg["aspect"] == 1.5
    assert new.is_file() and not old.exists()


def test_soft_reset_restores_power_up_sets_without_nsr():
    p = Printer()
    p.wide().inverse().align("right").designate(2, "fr").invoke(2)
    p.take()
    d = p.soft_reset().take()
    assert b"\x1f" not in d
    assert d == b"\x0f\x1b(B\x1b-W\x1b*K\x1b/{\x1bL\x1bI\x1b&<"
    assert p.g == b.POWER_UP_G and p.gl == 0 and not p.is_wide and p.alignment == "left"
    assert out(lambda q: (q.soft_reset(), q.line("Σ")))[-3:] == b"\x19\x72\r"   # Greek in G2 again
