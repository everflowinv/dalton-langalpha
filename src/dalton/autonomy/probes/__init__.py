"""Zero-token preflight probe types and registry (PK.16)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProbeSignal:
    """A single zero-token signal detected by a preflight probe."""

    probe: str
    signal_key: str
    priority: str  # "P0" | "P1" | "P2"
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
