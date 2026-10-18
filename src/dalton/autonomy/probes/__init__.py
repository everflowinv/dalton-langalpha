"""Zero-token preflight probe types and registry (PK.16, G4.1).

Company signal sources are strictly limited to 4 probes:
- `sec`: SEC EDGAR submissions feed by CIK (`seen_accessions`)
- `internal`: Researchdata bridge new docs (`sales-notes`, `alphaengine`, `guidepoint`)
- `agenda`: Due validations (`next_validations`), confirmed `watch.events`, `wake_conditions`, and active `open_questions`
- `earnings`: Confirmed earnings windows (`watch.events`) T-3 / T0 / T+1
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

COMPANY_PROBES: tuple[str, ...] = ("sec", "internal", "agenda", "earnings")


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


__all__ = ["COMPANY_PROBES", "ProbeSignal"]

