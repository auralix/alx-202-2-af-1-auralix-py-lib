# SPDX-License-Identifier: MIT
"""The firmware artifact and its runtime state, independent of any product.

``alx.fw.live_watch``: variables by name through the debug probe while the core runs. Siblings when
a test needs them: symbols and types from the ELF, flash and RAM usage from the linker map, identity
of an image file.
"""
