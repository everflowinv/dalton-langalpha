from __future__ import annotations

import os
from pathlib import Path


def workspace_root(explicit: str | Path | None = None) -> Path:
    """Resolve the active Dalton research home directory from explicit arg or env."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser()
    for key in ("OPENCLAW_WORKSPACE", "DALTON_RESEARCH_HOME"):
        val = os.environ.get(key, "").strip()
        if val:
            return Path(val).expanduser()
    return Path.home() / "Library" / "Application Support" / "Everflow" / "research"
