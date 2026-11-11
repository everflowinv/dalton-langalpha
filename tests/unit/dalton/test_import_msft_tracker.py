"""Settled behavior for the offline MSFT tracker importer."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dalton.import_msft_tracker import import_msft_tracker

SNAP = "2026-11-09_2250.json"
COMMITTED_AT = "2026-11-09T22:50:00-05:00"
RUN_ID = "20261109-2250"
SOURCE_LOG = f"2026-11-09 22:50 | {RUN_ID} | material=yes | source note\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _snapshot(root: Path, seen: str = '["sec:1"]\n') -> None:
    last_run = {
        "run_id": RUN_ID,
        "committed_at": COMMITTED_AT,
        "since": "2026-11-08",
        "until": "2026-11-09",
        "snapshot_file": f"/old/state/stock-tracker/MSFT/consensus/{SNAP}",
        "material": "yes",
    }
    _write(root / "seen.json", seen)
    _write(root / "last_run.json", json.dumps(last_run) + "\n")
    _write(root / "updates.log", SOURCE_LOG)
    _write(root / "thesis.md", "source thesis\n")
    _write(root / "deliveries.json", "{}\n")
    _write(root / "consensus" / SNAP, '{"earnings_estimate":{"0y":{"avg":1.25}}}\n')


def _tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        files[str(path.relative_to(root))] = path.read_bytes()
    return files


def test_dry_run_apply_repeat_preserves_newer_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    _snapshot(source)
    newer = {
        "ticker": "MSFT",
        "run_id": "20261110-0100",
        "committed_at": "2026-11-10T01:00:00-05:00",
        "since": "2026-11-09",
        "until": "2026-11-10",
        "snapshot_file": "data/consensus/2026-11-10_0100.json",
        "material": "no",
        "summary": "target note",
        "ae_covered_until": "2026-11-10",
    }
    _write(workspace / "research/tracker/last_run.json", json.dumps(newer) + "\n")
    _write(workspace / "research/tracker/seen.json", '["wiki:keep"]\n')
    _write(
        workspace / "research/tracker/updates.log",
        "2026-11-10 01:00 | 20261110-0100 | material=no | target note\n",
    )
    _write(workspace / "thesis.md", "rendered thesis\n")
    _write(workspace / "data/thesis.json", '{"version":1}\n')
    source_before = _tree(source)
    workspace_before = _tree(workspace)

    dry = import_msft_tracker(source, workspace, apply=False)
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["paths"]["written"] == []
    assert dry["last_run_action"] == "keep_newer"
    assert dry["counts"]["seen_merged"] == 2
    assert _tree(source) == source_before
    assert _tree(workspace) == workspace_before

    applied = import_msft_tracker(source, workspace, apply=True)
    assert applied["ok"] is True
    assert applied["paths"]["written"]
    seen = json.loads((workspace / "research/tracker/seen.json").read_text())
    assert seen == ["sec:1", "wiki:keep"]
    assert (workspace / "research/tracker/last_run.json").read_bytes() == workspace_before[
        "research/tracker/last_run.json"
    ]
    assert (workspace / "thesis.md").read_text() == "rendered thesis\n"
    assert (workspace / "data/thesis.json").read_text() == '{"version":1}\n'
    consensus = (workspace / "data/consensus" / SNAP).read_bytes()
    assert consensus == (source / "consensus" / SNAP).read_bytes()
    assert b"period_end" not in consensus
    archive = workspace / "research/tracker/imported-msft"
    assert (archive / "thesis.md").read_text() == "source thesis\n"
    manifest = json.loads((archive / "manifest.json").read_text())
    assert manifest["source_committed_at"] == COMMITTED_AT
    assert "imported_at" not in manifest
    log = (workspace / "research/tracker/updates.log").read_text()
    assert log.startswith("2026-11-10 01:00 | 20261110-0100 | material=no | target note\n")
    assert log.count(RUN_ID) == 1
    assert _tree(source) == source_before

    after = _tree(workspace)
    repeat = import_msft_tracker(source, workspace, apply=True)
    assert repeat["ok"] is True
    assert repeat["paths"]["written"] == []
    assert _tree(workspace) == after
    assert (workspace / "research/tracker/updates.log").read_text().count(RUN_ID) == 1


def test_overlapping_roots_write_nothing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _snapshot(source)
    before = _tree(source)
    same = import_msft_tracker(source, source, apply=True)
    assert same["ok"] is False
    assert same["paths"]["written"] == []
    assert any(item["reason"] == "overlapping_roots" for item in same["conflicts"])
    assert _tree(source) == before

    nested = source / "ws"
    nested.mkdir()
    nested_before = _tree(source)
    inside = import_msft_tracker(source, nested, apply=True)
    assert inside["ok"] is False
    assert _tree(source) == nested_before

    workspace = tmp_path / "workspace"
    inner = workspace / "snap"
    _snapshot(inner)
    outer_before = _tree(workspace)
    outside = import_msft_tracker(inner, workspace, apply=True)
    assert outside["ok"] is False
    assert _tree(workspace) == outer_before

    stock = tmp_path / "stock"
    msft = stock / "MSFT"
    _snapshot(msft)
    _write(stock / "x_pool.json", "{}\n")
    _write(stock / "x_authors.json", "{}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    allowed = import_msft_tracker(msft, elsewhere, stock, apply=False)
    assert allowed["ok"] is True
    assert not (elsewhere / "research").exists()
    stock_before = _tree(stock)
    shared_workspace = import_msft_tracker(msft, stock, stock, apply=True)
    assert shared_workspace["ok"] is False
    assert shared_workspace["paths"]["written"] == []
    assert _tree(stock) == stock_before


def test_target_custom_metadata_retained(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    _snapshot(source)
    _write(
        shared / "x_pool.json",
        json.dumps(
            {
                "Ada": {
                    "name": "Ada",
                    "active": True,
                    "tickers": ["MSFT", "AAPL"],
                    "bio": "analyst",
                    "api_key": "do-not-copy",
                },
                "Bob": {
                    "name": "Bob",
                    "active": True,
                    "tickers": ["ALL"],
                    "bearer_token": "do-not-copy",
                },
            }
        ),
    )
    _write(
        shared / "x_authors.json",
        json.dumps(
            {
                "ada": {"handle": "Ada", "name": "Ada", "access_token": "do-not-copy"},
                "bob": {"handle": "Bob", "name": "Bob"},
            }
        ),
    )
    _write(
        workspace / "research/tracker/x_pool.json",
        json.dumps(
            {
                "Ada": {
                    "name": "Ada",
                    "active": True,
                    "tickers": ["MSFT", "AAPL"],
                    "token_supply_estimate": 42,
                }
            }
        ),
    )
    _write(
        workspace / "research/tracker/x_authors.json",
        json.dumps({"Ada": {"handle": "Ada", "name": "Ada", "token_supply_estimate": 42}}),
    )

    result = import_msft_tracker(source, workspace, shared, apply=True)
    assert result["ok"] is True
    pool = json.loads((workspace / "research/tracker/x_pool.json").read_text())
    authors = json.loads((workspace / "research/tracker/x_authors.json").read_text())
    assert pool["Ada"]["token_supply_estimate"] == 42
    assert pool["Ada"]["bio"] == "analyst"
    assert "api_key" not in pool["Ada"]
    assert pool["bob"]["name"] == "Bob"
    assert "bearer_token" not in pool["bob"]
    assert authors["Ada"]["token_supply_estimate"] == 42
    assert "access_token" not in authors["Ada"]
    assert "access_token" not in json.dumps(authors)


def test_consensus_conflict_blocks_all_writes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    _snapshot(source)
    _write(workspace / "data/consensus" / SNAP, '{"earnings_estimate":{"0y":{"avg":9}}}\n')
    _write(workspace / "research/tracker/seen.json", '["keep-me"]\n')
    source_before = _tree(source)
    before = _tree(workspace)
    result = import_msft_tracker(source, workspace, apply=True)
    assert result["ok"] is False
    assert result["paths"]["written"] == []
    assert any(item["reason"] == "consensus_conflict" for item in result["conflicts"])
    assert _tree(workspace) == before
    assert _tree(source) == source_before


def test_invalid_seen_and_timestamp(tmp_path: Path) -> None:
    bad_seen_source = tmp_path / "bad-seen"
    bad_seen_ws = tmp_path / "bad-seen-ws"
    _snapshot(bad_seen_source, seen="[1]\n")
    bad_seen_ws.mkdir()
    seen_result = import_msft_tracker(bad_seen_source, bad_seen_ws, apply=True)
    assert seen_result["ok"] is False
    assert any(item["reason"] == "invalid_seen" for item in seen_result["conflicts"])
    assert _tree(bad_seen_ws) == {}

    bad_time_source = tmp_path / "bad-time"
    bad_time_ws = tmp_path / "bad-time-ws"
    _snapshot(bad_time_source)
    last_run = json.loads((bad_time_source / "last_run.json").read_text())
    last_run["committed_at"] = "2026-11-09T22:50:00"
    _write(bad_time_source / "last_run.json", json.dumps(last_run) + "\n")
    bad_time_ws.mkdir()
    time_result = import_msft_tracker(bad_time_source, bad_time_ws, apply=True)
    assert time_result["ok"] is False
    assert any(item["reason"] == "invalid_timestamp" for item in time_result["conflicts"])
    assert _tree(bad_time_ws) == {}


def test_escaping_target_symlink_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    _snapshot(source)
    outside.mkdir()
    _write(outside / "secret.txt", "leave-me\n")
    workspace.mkdir()
    (workspace / "research").symlink_to(outside, target_is_directory=True)
    source_before = _tree(source)
    result = import_msft_tracker(source, workspace, apply=True)
    assert result["ok"] is False
    assert result["paths"]["written"] == []
    assert any(item["reason"] == "symlink_escape" for item in result["conflicts"])
    assert _tree(outside) == {"secret.txt": b"leave-me\n"}
    assert not (outside / "tracker").exists()
    assert _tree(source) == source_before


def test_working_thesis_copies_once_and_keeps_edits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    _snapshot(source)
    _write(workspace / "thesis.md", "rendered thesis\n")
    _write(workspace / "data/thesis.json", '{"version":1}\n')
    before = _tree(workspace)
    source_before = _tree(source)

    dry = import_msft_tracker(source, workspace, apply=False)
    assert dry["ok"] is True
    assert dry["dry_run"] is True
    assert dry["paths"]["written"] == []
    assert dry["working_thesis"] == {
        "path": "research/tracker/thesis.md",
        "action": "created",
    }
    assert "research/tracker/thesis.md" in dry["paths"]["planned"]
    assert _tree(workspace) == before
    assert _tree(source) == source_before

    applied = import_msft_tracker(source, workspace, apply=True)
    assert applied["ok"] is True
    assert applied["working_thesis"]["action"] == "created"
    working = workspace / "research/tracker/thesis.md"
    archive = workspace / "research/tracker/imported-msft/thesis.md"
    source_thesis = (source / "thesis.md").read_bytes()
    assert working.read_bytes() == source_thesis
    assert archive.read_bytes() == source_thesis
    assert (workspace / "thesis.md").read_text() == "rendered thesis\n"
    assert (workspace / "data/thesis.json").read_text() == '{"version":1}\n'

    working.write_text("edited working thesis\n", encoding="utf-8")
    edited = working.read_bytes()
    source_after_edit = _tree(source)
    repeat_dry = import_msft_tracker(source, workspace, apply=False)
    assert repeat_dry["ok"] is True
    assert repeat_dry["working_thesis"]["action"] == "reused"
    assert repeat_dry["paths"]["written"] == []
    assert working.read_bytes() == edited

    repeat = import_msft_tracker(source, workspace, apply=True)
    assert repeat["ok"] is True
    assert repeat["working_thesis"] == {
        "path": "research/tracker/thesis.md",
        "action": "reused",
    }
    assert repeat["paths"]["written"] == []
    assert working.read_bytes() == edited
    assert archive.read_bytes() == source_thesis
    assert (workspace / "thesis.md").read_text() == "rendered thesis\n"
    assert (workspace / "data/thesis.json").read_text() == '{"version":1}\n'
    assert _tree(source) == source_after_edit
