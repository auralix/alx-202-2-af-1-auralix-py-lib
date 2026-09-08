# SPDX-License-Identifier: MIT
"""Exception types of the Auralix Python Library.

All derive from ``RuntimeError`` so callers that catch ``RuntimeError`` keep working.
"""


class AlxError(RuntimeError):
    """Base class of every error raised by the library."""


class ProbeError(AlxError):
    """A probe operation failed: no probe or target, tool error, bad verify, missing read-back."""


class InstrumentError(AlxError):
    """A bench instrument did not answer, answered wrongly, or refused an unsafe request."""
