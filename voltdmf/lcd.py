"""Driver for a SparkFun serial 4x20 character LCD on the Pi's UART.

The PiCAN2 is SPI + GPIO25 only (``host/config.txt.snippet``), so the Pi's
hardware UART is free for the display. Wiring is one direction only: Pi
``TXD`` (GPIO14, header pin 8) -> LCD ``RX``, plus 5 V and GND. The Pi's
3.3 V TX is a valid logic high for the LCD's 5 V input; we never read back.

This targets the common subset of the two SparkFun families, so it works
without knowing which board is fitted:

* **SerLCD / OpenLCD** (black PCB, ATmega328P) -- current.
* **Serial Enabled LCD backpack** (older red PCB) -- retired.

Both display any printable byte as-is and both take HD44780 commands behind
a ``0xFE`` prefix. We only use two commands, identical on each:

* ``0xFE 0x01``               -- clear + home
* ``0xFE (0x80 | ddaddr)``    -- move cursor; 20x4 row addresses are the
                                HD44780 standard ``0x00 0x40 0x14 0x54``.

The only setting byte we send is the primary backlight level (``0x7C``
then ``128..157`` = 0..100%), which maps the same way on both boards --
dimming it is the cheapest fix for a board that resets under a sagging
5 V feed. Contrast / baud / splash config differ between the families and
are left alone; set those once with SparkFun's own utility.

No pyserial dependency: the port is configured with stdlib ``termios``
(raw, 8N1, ``CLOCAL``). A :class:`SerLcd` always maintains an in-memory
image of the screen (:meth:`snapshot`), and in ``dry_run`` mode that image
is all there is -- nothing opens the port -- which is what the unit tests
and ``tools/lcd.py --dry-run`` exercise.
"""

from __future__ import annotations

import os
import time

_CMD = 0xFE  # "next byte is an HD44780 command" on both board families
_CLEAR = 0x01
_DDRAM = 0x80  # OR with the target address to move the cursor

#: "next byte is a display setting" on both families. The one setting we
#: touch is the primary/white backlight: value 128..157 = 0..100% in 30
#: steps (same mapping on the old backpack and on OpenLCD). Dimming it is
#: the cheapest brown-out fix -- the backlight is most of the board's
#: current draw, and a sagging 5 V feed resets the on-board micro (the
#: "unplug it to recover" symptom).
_SETTING = 0x7C
_BL_MIN, _BL_MAX = 128, 157

#: The board eats serial for a beat after power-up while it draws its splash
#: screen. Write before that and the first bytes are lost. open() waits this
#: long; bump it with ``boot_wait`` if a fresh replug still drops the top line.
_BOOT_WAIT_S = 1.2

#: HD44780 DDRAM start address of each row on a 20x4 module.
ROW_ADDR = (0x00, 0x40, 0x14, 0x54)


def _baud_const(baud: int) -> int:
    import termios

    table = {
        1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800,
        9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
        57600: termios.B57600, 115200: termios.B115200,
    }
    try:
        return table[baud]
    except KeyError:
        raise ValueError(f"unsupported baud {baud}; pick one of {sorted(table)}")


class SerLcd:
    """A 4x20 SparkFun serial LCD. Use as a context manager.

    ``SerLcd(port, baud).open()`` configures the tty; ``dry_run=True`` skips
    the hardware and only keeps the screen image.
    """

    def __init__(
        self,
        port: str = "/dev/serial0",
        baud: int = 9600,
        *,
        cols: int = 20,
        rows: int = 4,
        backlight: int | None = None,
        boot_wait: float = _BOOT_WAIT_S,
        dry_run: bool = False,
    ) -> None:
        self.port = port
        self.baud = baud
        self.cols = cols
        self.rows = rows
        self.backlight = backlight
        self.boot_wait = boot_wait
        self.dry_run = dry_run
        self._fd: int | None = None
        self._img = [" " * cols for _ in range(rows)]

    # -- lifecycle -------------------------------------------------------
    def open(self) -> "SerLcd":
        if self.dry_run:
            return self
        import termios

        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = \
                termios.tcgetattr(fd)
            speed = _baud_const(self.baud)
            iflag = 0
            oflag = 0  # no OPOST: do not translate \n
            lflag = 0  # no echo, non-canonical
            cflag &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE
                       | termios.CRTSCTS)
            cflag |= termios.CS8 | termios.CLOCAL | termios.CREAD
            termios.tcsetattr(
                fd, termios.TCSANOW,
                [iflag, oflag, cflag, lflag, speed, speed, cc])
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        time.sleep(self.boot_wait)  # ride out the power-on splash
        if self.backlight is not None:
            self.set_backlight(self.backlight)
        self.clear()
        return self

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self) -> "SerLcd":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low level ----------------------------------------------------------
    def _out(self, data: bytes) -> None:
        if self.dry_run or self._fd is None:
            return
        os.write(self._fd, data)

    def _command(self, byte: int, settle: float = 0.002) -> None:
        self._out(bytes((_CMD, byte)))
        if settle:
            time.sleep(settle)

    # -- screen ops -------------------------------------------------------
    def set_backlight(self, pct: int) -> None:
        """Primary/white backlight, 0..100 %. Dim it if the board resets."""
        pct = max(0, min(100, int(pct)))
        val = _BL_MIN + round(pct / 100 * (_BL_MAX - _BL_MIN))
        self._out(bytes((_SETTING, val)))
        time.sleep(0.05)  # OpenLCD writes this one to EEPROM

    def clear(self) -> None:
        self._img = [" " * self.cols for _ in range(self.rows)]
        self._command(_CLEAR, settle=0.005)  # HD44780 clear needs ~1.5 ms+

    def write_at(self, row: int, col: int, text: str, *,
                 force: bool = False) -> None:
        """Write ``text`` at (row, col), clipped to the row. No wrap.

        Skips the serial write when the row image is already what we'd send
        (``force=True`` overrides) -- keeps the byte rate down on a
        once-a-few-seconds repaint, which also eases a marginal 5 V feed.
        """
        if not 0 <= row < self.rows:
            raise ValueError(f"row {row} out of range 0..{self.rows - 1}")
        col = max(0, col)
        text = text[: max(0, self.cols - col)]
        if not text:
            return
        line = self._img[row]
        new = (line[:col] + text + line[col + len(text):])[: self.cols]
        if new == line and not force:
            return
        self._img[row] = new
        self._command(_DDRAM | (ROW_ADDR[row] + col), settle=0.001)
        self._out(text.encode("ascii", "replace"))

    def line(self, row: int, text: str, *, force: bool = False) -> None:
        """Replace a whole row: left-justified, space-padded, clipped."""
        self.write_at(row, 0, text.ljust(self.cols)[: self.cols], force=force)

    def lines(self, *rows: str) -> None:
        """Set the whole screen from up to ``self.rows`` strings."""
        for r in range(self.rows):
            self.line(r, rows[r] if r < len(rows) else "")

    def refresh(self) -> None:
        """Re-send every row unconditionally. Recovers the display if the
        board reset under us (a skipped write would otherwise leave the
        in-memory image and the dark panel disagreeing)."""
        for r, txt in enumerate(self._img):
            self.write_at(r, 0, txt, force=True)

    def snapshot(self) -> list[str]:
        """Current in-memory image of the screen, one string per row."""
        return list(self._img)

    def render(self) -> str:
        """The screen image as an ASCII box -- for --dry-run and tests."""
        top = "+" + "-" * self.cols + "+"
        body = "\n".join(f"|{row}|" for row in self._img)
        return f"{top}\n{body}\n{top}"
