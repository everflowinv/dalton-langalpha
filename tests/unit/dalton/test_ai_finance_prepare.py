"""Offline batch preparation. Collectors are local doubles, not live calls."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from scripts.dalton.ai_finance.prepare import PrepareError, prepare_batch
import scripts.dalton.ai_finance.prepare as prepare_mod

HKT = ZoneInfo("Asia/Hong_Kong")
FIXED = datetime(2026, 10, 11, 12, 0, tzinfo=HKT)
BATCH = "batch-20261011"


def _window(started_at: datetime = FIXED, hours: int = 24) -> dict[str, Any]:
    end = started_at.astimezone(HKT)
    start = end - timedelta(hours=hours)
    return {
        "timezone": "Asia/Hong_Kong",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours": hours,
    }


def _earnings(request: dict[str, Any], *, doc_id: str = "doc1", body: str = "AI body") -> dict[str, Any]:
    (request["candidate_dir"] / f"{doc_id}.txt").write_text(body, encoding="utf-8")
    return {
        "window": request["window"],
        "candidates": [{"doc_id": doc_id, "full_text_path": f"candidates/{doc_id}.txt"}],
    }


def test_rejects_bad_batch_id_and_relative_root(tmp_path: Path) -> None:
    with pytest.raises(PrepareError):
        prepare_batch(state_root=tmp_path, batch_id="../outside", started_at=FIXED)
    assert not (tmp_path / "batches").exists()

    missing = tmp_path / "missing"
    with pytest.raises(PrepareError):
        prepare_batch(state_root=missing, batch_id=BATCH, started_at=FIXED)
    assert not missing.exists()

    relative = Path("ai-finance-core-relative-not-a-root")
    try:
        with pytest.raises(PrepareError):
            prepare_batch(state_root=relative, batch_id=BATCH, started_at=FIXED)
        assert not relative.exists()
    finally:
        if relative.exists():
            shutil.rmtree(relative)

    blocked = tmp_path / ".openclaw" / "state"
    blocked.mkdir(parents=True)
    with pytest.raises(PrepareError):
        prepare_batch(state_root=blocked, batch_id=BATCH, started_at=FIXED)
    assert not (blocked / "batches").exists()


def test_success_is_reused_and_a_different_clock_is_not(tmp_path: Path) -> None:
    calls: list[str] = []
    seen = tmp_path / "github_seen.json"
    seen.write_bytes(b'{"repos": {}}\n')
    seen_bytes = seen.read_bytes()

    def github(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("github")
        assert request["now"] == FIXED
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("earnings")
        assert request["now"] == FIXED
        return _earnings(request)

    first = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert first["ok"] is True
    assert first["reused"] == []
    assert first["manifest"] == "manifest.json"
    batch = tmp_path / "batches" / BATCH
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"]["github_candidates"]["path"] == "github_candidates.json"
    assert manifest["candidates"][0]["path"] == "candidates/doc1.txt"
    assert (batch / manifest["candidates"][0]["path"]).read_text(encoding="utf-8") == "AI body"
    metadata_bytes = (batch / "metadata.json").read_bytes()
    github_inode = (batch / "github_candidates.json").stat().st_ino
    manifest_inode = (batch / "manifest.json").stat().st_ino

    second = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert second["ok"] is True
    assert second["reused"] == ["github", "earnings"]
    assert calls == ["github", "earnings"]
    assert (batch / "github_candidates.json").stat().st_ino == github_inode
    assert (batch / "manifest.json").stat().st_ino == manifest_inode

    equivalent = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED.astimezone(timezone.utc),
        github_collector=github,
        earnings_collector=earnings,
    )
    assert equivalent["ok"] is True
    assert equivalent["reused"] == ["github", "earnings"]
    assert (batch / "metadata.json").read_bytes() == metadata_bytes

    shifted = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED + timedelta(hours=1),
        github_collector=github,
        earnings_collector=earnings,
    )
    assert shifted["ok"] is False
    assert shifted["errors"] == ["started_at_conflict"]
    shorter = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        hours=12,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert shorter["ok"] is False
    assert shorter["errors"] == ["started_at_conflict"]
    assert calls == ["github", "earnings"]
    assert (batch / "metadata.json").read_bytes() == metadata_bytes
    assert seen.read_bytes() == seen_bytes
    assert list(tmp_path.rglob("*.sqlite")) == []


def test_failed_earnings_retries_only_that_stage(tmp_path: Path) -> None:
    seen = tmp_path / "github_seen.json"
    seen.write_bytes(b'{"repos":{"keep":{"last_ts":1}}}\n')
    seen_bytes = seen.read_bytes()
    calls: list[tuple[str, datetime]] = []
    state = {"fail": True}

    def github(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(("github", request["now"]))
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append(("earnings", request["now"]))
        if state["fail"]:
            raise RuntimeError("scanner down")
        return _earnings(request)

    first = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    batch = tmp_path / "batches" / BATCH
    github_bytes = (batch / "github_candidates.json").read_bytes()
    assert first["ok"] is False
    assert first["stages"]["github"] == "complete"
    assert first["stages"]["earnings"] == "failed"
    assert first["errors"] == ["earnings_failed:RuntimeError"]
    assert not (batch / "manifest.json").exists()

    state["fail"] = False
    second = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert second["ok"] is True
    assert second["reused"] == ["github"]
    assert [name for name, _moment in calls] == ["github", "earnings", "earnings"]
    assert calls[-1][1] == FIXED
    assert (batch / "github_candidates.json").read_bytes() == github_bytes
    assert seen.read_bytes() == seen_bytes
    assert list(tmp_path.rglob("*.sqlite")) == []


def test_completion_metadata_failure_does_not_recollect_earnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "github_seen.json"
    seen.write_bytes(b'{"repos": {}}\n')
    seen_bytes = seen.read_bytes()
    calls: list[str] = []

    def github(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("github")
        assert request["now"] == FIXED
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("earnings")
        assert request["now"] == FIXED
        body = "first-body" if calls.count("earnings") == 1 else "recollected-body"
        return _earnings(request, body=body)

    real_write = prepare_mod._atomic_write
    armed = {"once": True}

    def flaky_write(path: Path, payload: bytes) -> None:
        if armed["once"] and path.name == "metadata.json":
            parsed = json.loads(payload)
            if parsed["stages"]["earnings"].get("status") == "complete":
                armed["once"] = False
                raise OSError("completion metadata")
        real_write(path, payload)

    monkeypatch.setattr(prepare_mod, "_atomic_write", flaky_write)
    first = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    batch = tmp_path / "batches" / BATCH
    artifact = batch / "earnings_scan.json"
    candidate = batch / "candidates" / "doc1.txt"
    saved_artifact = artifact.read_bytes()
    artifact_inode = artifact.stat().st_ino
    saved_candidate = candidate.read_bytes()
    stored = json.loads((batch / "metadata.json").read_text(encoding="utf-8"))
    assert first["ok"] is False
    assert first["inconsistent"] is False
    assert first["errors"] == ["earnings_failed:OSError"]
    assert first["stages"]["github"] == "complete"
    assert first["stages"]["earnings"] == "failed"
    assert first["reused"] == []
    assert calls == ["github", "earnings"]
    assert not (batch / "manifest.json").exists()
    assert isinstance(stored["stages"]["earnings"]["sha256"], str)
    assert stored["stages"]["earnings"]["candidate_files"]["candidates/doc1.txt"]
    assert candidate.read_text(encoding="utf-8") == "first-body"

    second = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    published = json.loads((batch / "metadata.json").read_text(encoding="utf-8"))
    assert second["ok"] is True
    assert second["inconsistent"] is False
    assert second["reused"] == ["github", "earnings"]
    assert second["stages"]["earnings"] == "complete"
    assert calls == ["github", "earnings"]
    assert artifact.read_bytes() == saved_artifact
    assert artifact.stat().st_ino == artifact_inode
    assert candidate.read_bytes() == saved_candidate
    assert candidate.read_text(encoding="utf-8") == "first-body"
    assert published["stages"]["earnings"]["sha256"] == hashlib.sha256(saved_artifact).hexdigest()
    assert published["stages"]["earnings"]["sha256"] == stored["stages"]["earnings"]["sha256"]
    assert seen.read_bytes() == seen_bytes
    assert list(tmp_path.rglob("*.sqlite")) == []


def test_pending_artifact_without_completion_record_is_not_recollected(tmp_path: Path) -> None:
    seen = tmp_path / "github_seen.json"
    seen.write_bytes(b'{"repos": {}}\n')
    seen_bytes = seen.read_bytes()
    calls: list[str] = []

    def github(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("github")
        assert request["now"] == FIXED
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("earnings")
        assert request["now"] == FIXED
        body = "first-body" if calls.count("earnings") == 1 else "recollected-body"
        return _earnings(request, body=body)

    prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    batch = tmp_path / "batches" / BATCH
    artifact = batch / "earnings_scan.json"
    candidate = batch / "candidates" / "doc1.txt"
    saved_artifact = artifact.read_bytes()
    artifact_inode = artifact.stat().st_ino
    saved_candidate = candidate.read_bytes()
    metadata_path = batch / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["stages"]["earnings"]["status"] = "pending"
    metadata["stages"]["earnings"]["sha256"] = None
    metadata["stages"]["earnings"]["candidate_files"] = {}
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    pending_metadata = metadata_path.read_bytes()
    manifest = (batch / "manifest.json").read_bytes()
    before = list(calls)

    result = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert result["ok"] is False
    assert result["inconsistent"] is True
    assert result["errors"] == ["earnings_uncommitted"]
    assert result["stages"]["github"] == "complete"
    assert result["stages"]["earnings"] == "pending"
    assert result["reused"] == ["github"]
    assert result["manifest"] is None
    assert calls == before
    assert artifact.read_bytes() == saved_artifact
    assert artifact.stat().st_ino == artifact_inode
    assert candidate.read_bytes() == saved_candidate
    assert candidate.read_text(encoding="utf-8") == "first-body"
    assert metadata_path.read_bytes() == pending_metadata
    assert (batch / "manifest.json").read_bytes() == manifest
    assert seen.read_bytes() == seen_bytes
    assert list(tmp_path.rglob("*.sqlite")) == []


def test_corrupt_artifact_is_not_rewritten_or_refetched(tmp_path: Path) -> None:
    calls: list[str] = []

    def github(_request: dict[str, Any]) -> dict[str, Any]:
        calls.append("github")
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("earnings")
        return _earnings(request)

    prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    batch = tmp_path / "batches" / BATCH
    artifact = batch / "github_candidates.json"
    flipped = bytearray(artifact.read_bytes())
    flipped[-2] ^= 0x1
    artifact.write_bytes(flipped)
    saved = artifact.read_bytes()
    metadata = (batch / "metadata.json").read_bytes()
    result = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert result["ok"] is False
    assert result["inconsistent"] is True
    assert calls == ["github", "earnings"]
    assert artifact.read_bytes() == saved
    assert (batch / "metadata.json").read_bytes() == metadata


def test_manifest_mismatch_and_symlink_candidate_are_not_success(tmp_path: Path) -> None:
    calls: list[str] = []

    def github(_request: dict[str, Any]) -> dict[str, Any]:
        calls.append("github")
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("earnings")
        return _earnings(request)

    prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    batch = tmp_path / "batches" / BATCH
    manifest_path = batch / "manifest.json"
    flipped = bytearray(manifest_path.read_bytes())
    flipped[-2] ^= 0x1
    manifest_path.write_bytes(flipped)
    saved_manifest = manifest_path.read_bytes()
    metadata = (batch / "metadata.json").read_bytes()
    mismatch = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert mismatch["ok"] is False
    assert mismatch["inconsistent"] is True
    assert manifest_path.read_bytes() == saved_manifest
    assert (batch / "metadata.json").read_bytes() == metadata
    assert calls == ["github", "earnings"]

    broken = tmp_path / "broken-meta"
    broken.mkdir()
    meta = broken / "batches" / BATCH / "metadata.json"
    meta.parent.mkdir(parents=True)
    meta.write_text("{", encoding="utf-8")
    raw = meta.read_bytes()
    invalid = prepare_batch(
        state_root=broken,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    assert invalid["inconsistent"] is True
    assert invalid["ok"] is False
    assert meta.read_bytes() == raw
    assert calls == ["github", "earnings"]


def test_symlink_candidate_is_not_accepted(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret body", encoding="utf-8")
    calls: list[str] = []

    def github(_request: dict[str, Any]) -> dict[str, Any]:
        calls.append("github")
        return {"version": 3, "candidates": []}

    def earnings(request: dict[str, Any]) -> dict[str, Any]:
        calls.append("earnings")
        link = request["candidate_dir"] / "doc1.txt"
        link.symlink_to(outside)
        return {
            "window": request["window"],
            "candidates": [{"doc_id": "doc1", "full_text_path": "candidates/doc1.txt"}],
        }

    result = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        github_collector=github,
        earnings_collector=earnings,
    )
    batch = tmp_path / "batches" / BATCH
    assert result["ok"] is False
    assert (batch / "candidates" / "doc1.txt").is_symlink()
    assert not (batch / "manifest.json").exists()
    for path in batch.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert b"secret body" not in path.read_bytes()

    ready = prepare_batch(
        state_root=tmp_path,
        batch_id="batch-regular",
        started_at=FIXED,
        github_collector=github,
        earnings_collector=lambda request: _earnings(request),
    )
    assert ready["ok"] is True
    regular = tmp_path / "batches" / "batch-regular"
    target = tmp_path / "swap.txt"
    target.write_text("secret body", encoding="utf-8")
    candidate = regular / "candidates" / "doc1.txt"
    candidate.unlink()
    candidate.symlink_to(target)
    metadata = (regular / "metadata.json").read_bytes()
    swapped = prepare_batch(
        state_root=tmp_path,
        batch_id="batch-regular",
        started_at=FIXED,
        github_collector=github,
        earnings_collector=lambda request: _earnings(request),
    )
    assert swapped["ok"] is False
    assert swapped["inconsistent"] is True
    assert candidate.is_symlink()
    assert (regular / "metadata.json").read_bytes() == metadata


def test_default_collectors_receive_the_frozen_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_collect(**kwargs: Any) -> dict[str, Any]:
        seen["collect"] = kwargs
        return {"candidates": []}

    def fake_scan(args: Any) -> dict[str, Any]:
        seen["scan"] = args
        return {"window": _window(), "candidates": []}

    monkeypatch.setattr(prepare_mod, "collect_trending", fake_collect)
    monkeypatch.setattr(prepare_mod, "scan", fake_scan)
    result = prepare_batch(
        state_root=tmp_path,
        batch_id=BATCH,
        started_at=FIXED,
        transport={"url": "http://127.0.0.1/rpc"},
    )
    assert result["ok"] is True
    assert seen["collect"]["output"] is None
    assert seen["collect"]["update_state_file"] is True
    assert seen["collect"]["now"] == FIXED
    assert seen["collect"]["state_file"] == tmp_path / "github_trending_history.json"
    assert seen["collect"]["seen_file"] == tmp_path / "github_seen.json"
    assert seen["scan"].now == FIXED
    assert seen["scan"].hours == 24
    assert seen["scan"].max_documents == 80
    assert seen["scan"].query is None
    assert seen["scan"].transport == {"url": "http://127.0.0.1/rpc"}
    assert seen["scan"].relative_root == tmp_path / "batches" / BATCH
    assert not (tmp_path / "github_seen.json").exists()
    assert list(tmp_path.rglob("*.sqlite")) == []


def test_package_has_no_legacy_credential_paths() -> None:
    import scripts.dalton.ai_finance as package

    root = Path(package.__file__).resolve().parent
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))
    assert "8950" not in text
    assert "Path.home()" not in text
    assert ".openclaw/.env" not in text
    assert callable(package.prepare_batch)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(digest) == 64
