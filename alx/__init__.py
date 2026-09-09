# SPDX-License-Identifier: MIT
"""Auralix Python Library: bench and HIL mechanisms shared by the device repos.

Packages by family: ``alx.debug_probe`` (the debug probe facade and one adapter per tool, e.g.
``jlink``), ``alx.psu`` (one adapter per power supply model), ``alx.c_lib`` (clients of the Auralix
C Library protocols: ``cli``, ``trace``), ``alx.fw`` (the firmware artifact and its runtime state:
``live_watch``), ``alx.verify`` (evidence of a run and the lane gates). Single tools stay top level:
``alx.serial_logger`` records a UART for days. ``alx.errors`` holds the exception hierarchy. Product
knowledge (MCU names, memory maps, ports, policy values) never lives here; the device repo passes it
in.
"""

__version__ = "0.2.0"
