# SPDX-License-Identifier: MIT
"""Clients of the protocols the Auralix C Library defines.

``alx.c_lib.cli``: the command line interface (one command in, one JSON document out) over a serial
port. ``alx.c_lib.trace``: parsers for the trace output (boot banner, ``[LEVEL]`` lines). Further
protocols the C library defines (bus application layers, ...) belong here as well.

``alx.c_lib.host_build``: the other side of the same relationship - the host build that turns C
sources into the DLL a pytest suite drives through ctypes (toolchain, rebuild check, compile
database, the one-step and two-step recipes, the sanitizer and coverage variants). Every C
repository with host tests needs it, and none of it is specific to one.
"""
