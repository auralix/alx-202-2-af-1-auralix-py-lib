# SPDX-License-Identifier: MIT
"""Clients of the protocols the Auralix C Library defines.

``alx.c_lib.cli``: the command line interface (one command in, one JSON document out) over a serial
port. ``alx.c_lib.trace``: parsers for the trace output (boot banner, ``[LEVEL]`` lines). Further
protocols the C library defines (bus application layers, ...) belong here as well.

The host build of C sources and the C hooks of the mutation lane are verification mechanisms, not
protocols, and live in ``alx.verify`` (``host_build``, ``mutation_hooks``).
"""
