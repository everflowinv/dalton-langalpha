"""Reproduction & regression tests for BY4 Issue 2 (sandbox runtime):

Concurrent orphan sweep in ``wsfiles_transfer_runtime.pull`` must skip
``.wsfiles-relay-*`` staging files still referenced by an active restore pull,
while continuing to remove genuine unclaimed orphans.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from ptc_agent.core.sandbox import wsfiles_transfer_runtime as rt


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_concurrent_sweep_does_not_delete_claimed_relay_file_before_placement(
    tmp_path: Path, monkeypatch
):
    """If a second pull's _sweep_orphan_staging runs after pull #1 has passed
    its own sweep but before pull #1's _place_staged opens the staged file,
    pull #2 must skip the file (protected by flock ownership) so pull #1
    places it cleanly."""
    payload = b"relayed-file-bytes"
    staged_name = ".wsfiles-relay-active-1"
    (tmp_path / staged_name).write_bytes(payload)

    entered_place = threading.Event()
    sweep_finished = threading.Event()
    orig_place_staged = rt._place_staged

    def slow_place_staged(root: str, final: str, item: dict) -> dict:
        entered_place.set()
        assert sweep_finished.wait(timeout=5.0), "concurrent sweep did not finish"
        return orig_place_staged(root, final, item)

    monkeypatch.setattr(rt, "_place_staged", slow_place_staged)

    item = {
        "path": "data/report.txt",
        "kind": "file",
        "file": staged_name,
        "sha256": _sha(payload),
        "size": len(payload),
        "mode": 0o644,
        "mtime_ns": 1_700_000_000_000_000_000,
    }
    outcome: dict = {}

    def run_restore_pull():
        outcome["result"] = rt.pull({"root": str(tmp_path), "items": [item]})

    t = threading.Thread(target=run_restore_pull)
    t.start()
    try:
        assert entered_place.wait(timeout=5.0), "restore pull did not reach placement"
        # Concurrent cleanup pull with empty items runs _sweep_orphan_staging on the same root.
        rt.pull({"root": str(tmp_path), "items": []})
        sweep_finished.set()
    finally:
        sweep_finished.set()
        t.join(timeout=5.0)

    assert not t.is_alive()
    assert outcome["result"]["results"]["data/report.txt"]["status"] == "ok"
    assert (tmp_path / "data/report.txt").read_bytes() == payload
    assert not (tmp_path / staged_name).exists()


def test_unclaimed_unlocked_relay_orphan_is_still_swept(tmp_path: Path):
    """Genuine leftover .wsfiles-relay-* files not claimed or locked by any
    active restore are still cleaned up by a sweeping pull."""
    (tmp_path / ".wsfiles-relay-abandoned").write_bytes(b"stale-partial")
    rt.pull({"root": str(tmp_path), "items": []})
    assert not (tmp_path / ".wsfiles-relay-abandoned").exists()
