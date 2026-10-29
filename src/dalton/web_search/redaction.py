"""Thin re-export wrapper around ``scripts/dalton/log_redaction.py``.

The standalone stdlib implementation lives in ``scripts/dalton/`` so the
host-side bridges and the export script can import it directly without any
``src.*`` package imports; this wrapper is how the backend's provider code
reaches the same rule set (P0 fix1: one redaction module for every side).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_standalone_redaction() -> ModuleType:
    module_name = "dalton_log_redaction"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    script_path = (
        Path(__file__).resolve().parents[3] / "scripts" / "dalton" / "log_redaction.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_redaction = _load_standalone_redaction()

clean_log_field = _redaction.clean_log_field
redact_text = _redaction.redact_text
http_status_from_text = _redaction.http_status_from_text

__all__ = ["clean_log_field", "redact_text", "http_status_from_text"]
