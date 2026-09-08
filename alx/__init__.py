# SPDX-License-Identifier: MIT
"""Auralix Python Library: bench and HIL mechanisms shared by the device repos.

Each equipment class has a facade module (``alx.debug_probe``) and one adapter per tool
(``alx.jlink``). ``alx.cli`` speaks the Auralix C Library CLI over a serial port, ``alx.ram_view``
reads firmware variables through a probe, ``alx.serial_logger`` records a UART for days, ``alx.hil``
holds the pytest evidence helpers. Product knowledge (MCU names, memory maps, ports, policy values)
never lives here; it is passed in by the device repo that uses the library.
"""

__version__ = "0.1.0"
