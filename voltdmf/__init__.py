"""Volt Drive Mode Fixer.

Automates Gen 1 Chevy Volt drive-mode switching over the CAN bus to keep the
EV pack out of the voltage-sag region that trips reduced-propulsion mode.

See DESIGN.md for the hardware/software design and the phased plan. Anything
in this package tagged ``UNCONFIRMED`` / ``TODO_CALIBRATE`` is a value that
requires on-vehicle signal discovery (DESIGN.md Phase C) before it can be
trusted.
"""

__version__ = "0.1.0"
