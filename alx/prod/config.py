"""Load a single production configuration file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def load_config(path: str | Path) -> dict[str, Any]:
    """Read one JSON object without merging other configuration sources."""
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(value, dict):
        raise ValueError("Configuration must be a JSON object")
    return value
