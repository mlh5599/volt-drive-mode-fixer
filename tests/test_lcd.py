"""SerLcd screen-image tests. dry_run=True keeps the in-memory image only,
so none of this touches a serial port -- the image is what tools/lcd.py
--dry-run prints and what the ride-along dashboard lays out.
"""

import pytest

from voltdmf.lcd import ROW_ADDR, SerLcd


def _lcd():
    return SerLcd(dry_run=True).open()


def _capturing_lcd():
    """A dry-run LCD whose serial writes are captured instead of dropped."""
    lcd = SerLcd(dry_run=True).open()
    sent = bytearray()
    lcd._out = sent.extend
    return lcd, sent


def test_blank_after_construction():
    assert _lcd().snapshot() == [" " * 20] * 4


def test_lines_pad_and_clip_each_row():
    lcd = _lcd()
    lcd.lines("short", "x" * 40, "row two")
    snap = lcd.snapshot()
    assert snap[0] == "short".ljust(20)
    assert snap[1] == "x" * 20          # clipped to width
    assert snap[2] == "row two".ljust(20)
    assert snap[3] == " " * 20          # missing arg -> blank row


def test_write_at_overlays_without_wrapping():
    lcd = _lcd()
    lcd.line(0, "-" * 20)
    lcd.write_at(0, 15, "ABCDEFG")      # would run off the end
    assert lcd.snapshot()[0] == "-" * 15 + "ABCDE"


def test_write_at_rejects_bad_row():
    lcd = _lcd()
    with pytest.raises(ValueError):
        lcd.write_at(4, 0, "x")


def test_clear_restores_blank_image():
    lcd = _lcd()
    lcd.lines("a", "b", "c", "d")
    lcd.clear()
    assert lcd.snapshot() == [" " * 20] * 4


def test_render_is_a_boxed_grid():
    lcd = _lcd()
    lcd.line(1, "hi")
    box = lcd.render().splitlines()
    assert box[0] == "+" + "-" * 20 + "+"
    assert box[2] == "|" + "hi".ljust(20) + "|"
    assert box[-1] == "+" + "-" * 20 + "+"
    assert len(box) == 6                # top + 4 rows + bottom


def test_row_addresses_are_the_hd44780_20x4_standard():
    assert ROW_ADDR == (0x00, 0x40, 0x14, 0x54)


def test_dry_run_never_opens_a_port():
    lcd = SerLcd(port="/dev/does-not-exist", dry_run=True)
    lcd.open()          # must not raise
    lcd.lines("ok")
    lcd.close()


def test_set_backlight_scales_0_100_onto_128_157():
    lcd, sent = _capturing_lcd()
    lcd.set_backlight(0)
    lcd.set_backlight(100)
    lcd.set_backlight(60)
    assert list(sent) == [0x7C, 128, 0x7C, 157, 0x7C, 145]


def test_set_backlight_clamps_out_of_range():
    lcd, sent = _capturing_lcd()
    lcd.set_backlight(-20)
    lcd.set_backlight(500)
    assert list(sent) == [0x7C, 128, 0x7C, 157]


def test_line_skips_the_write_when_the_row_is_unchanged():
    lcd, sent = _capturing_lcd()
    lcd.line(0, "hello")
    assert sent, "first write must go out"
    sent.clear()
    lcd.line(0, "hello")
    assert sent == b"", "identical repaint must not touch the wire"
    lcd.line(0, "hello", force=True)
    assert sent, "force=True must write anyway"


def test_refresh_re_sends_every_row():
    lcd, sent = _capturing_lcd()
    lcd.lines("a", "b", "c", "d")
    sent.clear()
    lcd.refresh()
    # one cursor command (0xFE ...) per row, plus the row text
    assert sent.count(0xFE) == 4
    assert b"a" + b" " * 19 in sent
