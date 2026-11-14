"""The index-crowding overlay keeps cached crowding values the vendor no longer returns."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts/dalton/openclaw_overlays/skills/index-crowding-research/scripts/fetch_history.py"
)


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The overlay script imports its sibling from the OpenClaw-synced skill directory.
    stub = types.ModuleType("openclaw_workspace")
    stub.workspace_root = lambda _file: tmp_path  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openclaw_workspace", stub)
    spec = importlib.util.spec_from_file_location("fetch_history_overlay", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("nested", [False, True])
def test_known_value_survives_a_null_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool) -> None:
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_RANKING", {})
    rows = [{"tradeDt": "2026-11-10", "value": 81.5, "close": 1.0}]
    cached = {"code": 0, "data": {"data": rows}} if nested else {"code": 0, "data": rows}
    path = tmp_path / "399971_SZ.json"
    path.write_text(json.dumps(cached))

    fresh = {"code": 0, "data": [{"tradeDt": "2026-11-10", "value": None, "close": 1.0}]}
    out = mod.keep_known_crowding("399971.SZ", fresh, path)

    kept = {r["tradeDt"]: r for r in out["data"]}
    assert kept["2026-11-10"]["value"] == 81.5


@pytest.mark.parametrize("body", [{"code": 0, "data": None}, {"code": 0, "data": {"data": None}}])
def test_malformed_vendor_body_leaves_the_cache_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict
) -> None:
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_RANKING", {})
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    cache = tmp_path / "399971_SZ.json"
    original = {"code": 0, "data": [{"tradeDt": "2026-11-10", "value": 81.5}]}
    cache.write_text(json.dumps(original))

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return body

    class _Session:
        def get(self, *_a, **_k) -> _Resp:
            return _Resp()

    monkeypatch.setattr(mod, "session", lambda: _Session())
    with pytest.raises(RuntimeError, match="no data list"):
        mod.fetch("399971.SZ", refresh=True)
    assert json.loads(cache.read_text()) == original
