"""Signal bundle save, compare, ingest, and the admin route.

These checks use the real signal-memory CLI for the empty-history and
no-key paths. They do not call the embedding API.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import httpx
import pytest

REAL_ASYNC_CLIENT = httpx.AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
from scripts.dalton import ai_finance_access as access  # noqa: E402
from scripts.dalton import ai_finance_signals as signals  # noqa: E402
from scripts.dalton.ai_finance.ai_finance_daily_validate import validate_records  # noqa: E402
from scripts.dalton.ai_finance.ai_finance_signal_memory import (  # noqa: E402
    canonical_record,
    compare_records,
    connect_db,
    content_hash,
    ingest_records,
    stats,
)
from scripts.dalton.bridge_subprocess_env import build_nonarchive_subprocess_env  # noqa: E402
from scripts.dalton.market_digest_inputs import ChildResult, run_json_command  # noqa: E402
from src.dalton.access_tiers import is_internal_or_admin  # noqa: E402
from src.dalton.database import ai_finance as ai_finance_db  # noqa: E402
from src.dalton.database.ai_finance import authorize_ai_finance_execution  # noqa: E402
from src.dalton.database.automation_reports import lock_report_execution  # noqa: E402
from src.dalton.settings import (  # noqa: E402
    get_ai_finance_owner_user_id,
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
)
from src.dalton.tools import ai_finance_signals_tools as signal_tools  # noqa: E402

HKT = ZoneInfo("Asia/Hong_Kong")
BRIDGE_TOKEN = "bridge-mcp-token-secret"
ADMIN_TOKEN = "bridge-admin-token-secret"
OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "99999999-9999-4999-8999-999999999999"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
THREAD = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
AUTOMATION = "55555555-5555-4555-8555-555555555555"
EXECUTION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DATE = "2026-11-11"
DATE2 = "2026-11-12"
MARKER = "TRANSCRIPT-BODY-DO-NOT-STORE"
API_KEY = "gemini-test-key-value"
EVENT = "2026-11-11T12:00:00+08:00"


def _child(**overrides: Any) -> ChildResult:
    fields: dict[str, Any] = {
        "returncode": 0,
        "stdout": "",
        "stdout_truncated": False,
        "stderr_byte_count": 0,
        "stderr_truncated": False,
        "timed_out": False,
        "pid": 41,
        "still_running": False,
    }
    fields.update(overrides)
    return ChildResult(**fields)


def _earnings(summary: str = "AI revenue grew 中文") -> dict[str, Any]:
    doc_id = "doc-signal"
    return {
        "record_key": f"earnings:{doc_id}",
        "source_type": "earnings",
        "external_id": doc_id,
        "event_time": EVENT,
        "title": "Earnings",
        "entity": "Acme",
        "industry": "software",
        "summary": summary,
        "evidence": "call commentary",
        "signal_direction": "positive",
        "qa_concern": "margin",
        "management_response": "we are investing",
        "management_response_status": "direct",
        "tam_mapping": "automation software",
        "investment_implication": "supports the thesis",
        "tags": ["ai"],
        "signal_types": ["cost_productivity"],
    }


def _github(summary: str = "Repo momentum") -> dict[str, Any]:
    repo = "acme/alpha"
    return {
        "record_key": f"github:{repo}:{EVENT[:10]}",
        "source_type": "github",
        "external_id": repo,
        "event_time": EVENT,
        "title": "Repo",
        "entity": repo,
        "industry": "software",
        "summary": summary,
        "evidence": "stars",
        "signal_direction": "positive",
        "tam_mapping": "developer tools",
        "investment_implication": "track the project",
        "tags": ["github"],
        "signal_types": ["developer_adoption"],
        "url": "https://github.com/acme/alpha",
    }


def _bundle(summary: str = "AI revenue grew 中文") -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    decisions = {
        "records": [
            {
                "repo": "acme/alpha",
                "decision": "include",
                "reason": "eligible and material",
                "readme_url": "https://github.com/acme/alpha",
                "displayed": False,
            },
            {
                "repo": "acme/beta",
                "decision": "include",
                "reason": "eligible and not selected for a signal row",
                "readme_url": "https://github.com/acme/beta",
                "displayed": True,
            },
            {
                "repo": "acme/gamma",
                "decision": "exclude",
                "reason": "not eligible",
                "readme_url": "https://github.com/acme/gamma",
            },
        ]
    }
    dispositions = {
        "records": [
            {"doc_id": "doc-signal", "disposition": "signal", "reason": "material"},
            {"doc_id": "doc-watch", "disposition": "watchlist", "reason": "not yet"},
        ]
    }
    records = {"records": [_earnings(summary), _github()]}
    return decisions, dispositions, records


def _candidates() -> list[dict[str, Any]]:
    return [
        {"full_name": "acme/alpha", "history_verified": True, "report_eligible": True},
        {"full_name": "acme/beta", "history_verified": True, "report_eligible": True},
        {"full_name": "acme/gamma", "history_verified": False, "report_eligible": False},
    ]


def _scan() -> dict[str, Any]:
    return {
        "coverage": {"ai_candidates": 2},
        "candidates": [
            {
                "doc_id": "doc-signal",
                "context": MARKER,
                "full_text": MARKER,
                "full_text_path": "candidates/doc-signal.txt",
            },
            {
                "doc_id": "doc-watch",
                "context": MARKER,
                "full_text_path": "candidates/doc-watch.txt",
            },
        ],
    }


def _write_prepared(
    root: Path,
    batch_id: str,
    owner: str,
    *,
    profile: str = "ai-finance-daily",
    candidates: list[dict[str, Any]] | None = None,
    scan: dict[str, Any] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    batch = root / "batches" / batch_id
    batch.mkdir(parents=True)
    github_bytes = (json.dumps({"version": 3, "candidates": candidates or _candidates()}, ensure_ascii=False) + "\n").encode()
    earnings_bytes = (json.dumps(scan or _scan(), ensure_ascii=False) + "\n").encode()
    (batch / "github_candidates.json").write_bytes(github_bytes)
    (batch / "earnings_scan.json").write_bytes(earnings_bytes)
    github_sha = hashlib.sha256(github_bytes).hexdigest()
    earnings_sha = hashlib.sha256(earnings_bytes).hexdigest()
    started = datetime(int(batch_id[0:4]), int(batch_id[5:7]), int(batch_id[8:10]), 20, 5, tzinfo=HKT)
    scheduled = started.replace(minute=0)
    manifest = {
        "schema": 1,
        "batch_id": batch_id,
        "started_at": started.isoformat(),
        "window": {
            "timezone": "Asia/Hong_Kong",
            "start": started.isoformat(),
            "end": started.isoformat(),
            "hours": 24,
        },
        "artifacts": {
            "github_candidates": {"path": "github_candidates.json", "sha256": github_sha},
            "earnings_scan": {"path": "earnings_scan.json", "sha256": earnings_sha},
        },
        "candidates": [],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    (batch / "manifest.json").write_bytes(manifest_bytes)
    metadata = {
        "schema": 1,
        "batch_id": batch_id,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "stages": {
            "github": {"status": "complete", "artifact": "github_candidates.json", "sha256": github_sha},
            "earnings": {"status": "complete", "artifact": "earnings_scan.json", "sha256": earnings_sha},
        },
    }
    (batch / "metadata.json").write_bytes(json.dumps(metadata).encode())
    clock = {
        "schema": 1,
        "batch_id": batch_id,
        "started_at": started.isoformat(),
        "scheduled_at": scheduled.isoformat(),
        "first_execution_id": EXECUTION,
        "owner_user_id": owner,
        "profile": profile,
    }
    clocks = root / "batch_clocks"
    clocks.mkdir(exist_ok=True)
    (clocks / f"{batch_id}.json").write_text(json.dumps(clock), encoding="utf-8")


def _body(action: str, batch_id: str = DATE, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "batch_id": batch_id,
        "automation_execution_id": EXECUTION,
        "owner_user_id": OWNER,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "thread_id": THREAD,
        "profile": "ai-finance-daily",
    }
    payload.update(extra)
    return payload


def _env(home: Path, api_key: str | None = None) -> dict[str, str]:
    return signals.build_signal_subprocess_env(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "OPENAI_API_KEY": "sk-test-openai",
            "DALTON_BRIDGE_ADMIN_TOKEN": ADMIN_TOKEN,
            "GEMINI_API_KEY": "base-key-must-not-pass",
        },
        api_key=api_key,
        embedding_model="gemini-embedding-2",
        embedding_dim=768,
        repo_root=REPO_ROOT,
    )


def _python_link(directory: Path) -> Path:
    bin_dir = directory / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    link = bin_dir / "python"
    link.symlink_to(sys.executable)
    return link


def _run(
    root: Path,
    parsed: dict[str, Any],
    *,
    python: Path | None,
    env: dict[str, str] | None = None,
    command: Any = run_json_command,
    on_spawn: Any = None,
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    return signals.execute_signal(
        parsed,
        state_root=root,
        python_executable=python,
        embedding_model="gemini-embedding-2",
        embedding_dim=768,
        child_env=env,
        command=command,
        on_spawn=on_spawn,
        cancelled=cancelled,
    )


def _commit(root: Path, batch_id: str) -> dict[str, Any]:
    return json.loads((root / "signals" / batch_id / "commit.json").read_text(encoding="utf-8"))


def _rewrite_commit(root: Path, batch_id: str, **changes: Any) -> None:
    path = root / "signals" / batch_id / "commit.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(changes)
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(raw)


def _generation(root: Path, batch_id: str) -> Path:
    document = _commit(root, batch_id)
    return root / "signals" / batch_id / "g" / document["generation"]


def _compare_bytes_of(argv: list[str], summary: str) -> None:
    output = Path(argv[argv.index("--output") + 1])
    body = {
        "search_modes": ["empty_history"],
        "history_signals_before_ingest": 0,
        "history_embedded_signals_before_ingest": 0,
        "history_embedding_status_before_ingest": "empty",
        "comparisons": [{"current": {"summary": summary}, "matches": []}],
    }
    output.write_bytes((json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode())


def _stats_stdout() -> str:
    return json.dumps({
        "signals": 2,
        "embedded_signals": 0,
        "embedded_signals_any_model": 0,
        "embedding_status": "none",
        "embedding_coverage_pct": 0.0,
        "configured_embedding_model": "gemini-embedding-2",
        "configured_embedding_dim": 768,
        "signals_by_source": {"earnings": 1, "github": 1},
        "signals_by_direction": {"positive": 2},
        "embedding_models": {},
        "db_bytes": 1,
    })


def _absent(root: Path) -> None:
    assert not (root / "signals").exists()
    assert not (root / "ai_signal_memory.sqlite").exists()


def test_public_signatures_match_the_current_helpers() -> None:
    tool = signal_tools.create_ai_finance_signals_tools()[0]
    signature = inspect.signature(tool.coroutine)
    assert list(signature.parameters) == [
        "action",
        "batch_id",
        "github_decisions",
        "earnings_dispositions",
        "signal_records",
        "offset",
        "max_chars",
        "config",
    ]
    assert signature.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["github_decisions"].default is None
    assert signature.parameters["offset"].default == 0
    assert signature.parameters["max_chars"].default == 16000
    assert "period" not in inspect.signature(authorize_ai_finance_execution).parameters
    inspect.signature(validate_records).bind(
        Path("scan.json"),
        Path("dispositions.json"),
        Path("signals.json"),
        [],
        decisions_path=Path("decisions.json"),
    )
    inspect.signature(compare_records).bind(None, [], 8, model=None, dim=None)
    inspect.signature(ingest_records).bind(None, [], False, model=None, dim=None)
    inspect.signature(stats).bind(None, model=None, dim=None)
    inspect.signature(run_json_command).bind(
        ["python", "script"],
        env={},
        timeout_seconds=1,
        stdout_limit_bytes=8,
        stderr_limit_bytes=8,
        on_spawn=None,
        input_bytes=None,
    )
    inspect.signature(build_nonarchive_subprocess_env).bind(
        {},
        extra_env={},
        drop_keys=(),
        allow_keys=(),
        repo_root=REPO_ROOT,
    )
    inspect.signature(signals.execute_signal).bind(
        {"ok": True, "action": "read"},
        state_root=REPO_ROOT,
        python_executable=None,
        embedding_model="gemini-embedding-2",
        embedding_dim=768,
        child_env=None,
    )
    inspect.signature(signals.build_signal_subprocess_env).bind(
        {},
        api_key=None,
        embedding_model="gemini-embedding-2",
        embedding_dim=768,
        repo_root=REPO_ROOT,
    )
    assert signal_tools._HTTP_TIMEOUT_COMPARE_S == 960.0
    assert signal_tools._HTTP_TIMEOUT_STATS_S == 90.0
    assert signal_tools._HTTP_TIMEOUT_SAVE_S == 60.0


def test_operator_extension_and_path_owner_profile_stop_before_signal_io(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    link = _python_link(tmp_path)
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"state_root": str(root)}), encoding="utf-8")
    loaded = access.load_operator_config(config)
    assert loaded.python_executable is None
    assert loaded.embedding_model == "gemini-embedding-2"
    assert loaded.embedding_dim == 768
    config.write_text(json.dumps({
        "state_root": str(root),
        "python_executable": str(link),
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
    }), encoding="utf-8")
    loaded = access.load_operator_config(config)
    assert loaded.python_executable == link
    assert str(loaded.python_executable) == str(link)
    config.write_text(json.dumps({"state_root": str(root), "token": "x"}), encoding="utf-8")
    with pytest.raises(access.AccessError):
        access.load_operator_config(config)
    config.write_text(json.dumps({"state_root": str(root), "embedding_dim": True}), encoding="utf-8")
    with pytest.raises(access.AccessError):
        access.load_operator_config(config)
    hidden = tmp_path / ".openclaw" / "state"
    hidden.mkdir(parents=True)
    config.write_text(json.dumps({"state_root": str(hidden)}), encoding="utf-8")
    with pytest.raises(access.AccessError):
        access.load_operator_config(config)

    _write_prepared(root, DATE, OWNER)
    relative = _run(Path("relative-root"), signals.parse_signal_request(_body("save", **dict(zip(
        ("github_decisions", "earnings_dispositions", "signal_records"),
        _bundle(),
    )))), python=None)
    assert relative["code"] == "config_invalid"
    hidden_result = _run(hidden, signals.parse_signal_request(_body("stats")), python=link, env=_env(tmp_path))
    assert hidden_result["code"] == "config_invalid"
    _absent(hidden)
    wrong_owner_root = tmp_path / "other-owner"
    _write_prepared(wrong_owner_root, DATE, OTHER)
    decisions, dispositions, records = _bundle()
    wrong_owner = _run(
        wrong_owner_root,
        signals.parse_signal_request(_body(
            "save",
            github_decisions=decisions,
            earnings_dispositions=dispositions,
            signal_records=records,
        )),
        python=link,
    )
    assert wrong_owner["code"] == "owner_mismatch"
    _absent(wrong_owner_root)
    assert not (wrong_owner_root / "locks").exists()
    wrong_profile_root = tmp_path / "wrong-profile"
    _write_prepared(wrong_profile_root, DATE, OWNER, profile="market-digest-am")
    wrong_profile = _run(
        wrong_profile_root,
        signals.parse_signal_request(_body("compare")),
        python=link,
        env=_env(tmp_path),
    )
    assert wrong_profile["code"] == "clock_inconsistent"
    _absent(wrong_profile_root)
    mismatched = signals.parse_signal_request(_body("compare", profile="other-profile"))
    assert mismatched["code"] == "profile_mismatch"
    no_python = _run(root, signals.parse_signal_request(_body("compare")), python=None, env=_env(tmp_path))
    assert no_python["code"] == "interpreter_not_configured"
    assert not (root / "locks" / f"signals-{DATE}.lock").exists()
    assert not (root / "ai_signal_memory.sqlite").exists()

    bad_decisions = json.loads(json.dumps(decisions))
    bad_decisions["records"][0]["repo"] = "other/repo"
    unknown = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=bad_decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=link)
    assert unknown["code"] == "bundle_invalid"
    assert unknown["reason"] == "github_unknown_repo"
    _absent(root)

    ineligible = json.loads(json.dumps(decisions))
    ineligible["records"][2]["decision"] = "include"
    rejected = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=ineligible,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=link)
    assert rejected["reason"] == "github_not_eligible"
    _absent(root)

    duplicate_root = tmp_path / "duplicate-case"
    _write_prepared(duplicate_root, DATE, OWNER, candidates=[
        {"full_name": "Acme/Alpha", "history_verified": True, "report_eligible": True},
        {"full_name": "acme/alpha", "history_verified": True, "report_eligible": True},
    ])
    duplicate = _run(duplicate_root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=link)
    assert duplicate["reason"] == "github_duplicate_candidate"
    _absent(duplicate_root)

    partial = {"records": [dispositions["records"][0]]}
    mismatch = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=partial,
        signal_records=records,
    )), python=link)
    assert mismatch["reason"] == "disposition_mismatch"
    _absent(root)

    leaked = json.loads(json.dumps(records))
    leaked["records"][0]["metrics"] = [{"transcript": MARKER}]
    transcript = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=leaked,
    )), python=link)
    assert transcript["reason"] == "transcript_rejected"
    _absent(root)
    extra_key = {"records": [dict(dispositions["records"][0], body=MARKER), dispositions["records"][1]]}
    extra = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=extra_key,
        signal_records=records,
    )), python=link)
    assert extra["reason"] == "transcript_rejected"
    _absent(root)


def test_real_compare_snapshot_ingest_recovery_and_embedding_retry(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _write_prepared(root, DATE, OWNER)
    python = Path(sys.executable)
    env = _env(tmp_path)
    decisions, dispositions, records = _bundle()
    saved = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=python, env=env)
    assert saved["ok"] is True and saved["reused"] is False and saved["status"] == "open"
    assert saved["record_counts"]["signal_records"] == 2
    assert not (root / "ai_signal_memory.sqlite").exists()
    first_hash = saved["bundle_hash"]

    compared = _run(root, signals.parse_signal_request(_body("compare")), python=python, env=env)
    assert compared["reused"] is False
    assert compared["search_modes"] == ["empty_history"]
    assert compared["history_signals_before_ingest"] == 0
    assert compared["history_embedded_signals_before_ingest"] == 0
    assert compared["history_embedding_status_before_ingest"] == "empty"
    original_compare = (_generation(root, DATE) / "compare_result.json").read_bytes()
    assert hashlib.sha256(original_compare).hexdigest() == compared["content_sha256"]
    assert "中文" in original_compare.decode("utf-8")
    assert not (_generation(root, DATE) / "compare.json").exists()

    db_path = root / "ai_signal_memory.sqlite"
    conn = connect_db(db_path)
    extra = ingest_records(conn, [{
        "source_type": "earnings",
        "external_id": "extra-doc",
        "event_time": "2026-11-10T00:00:00+00:00",
        "summary": "unrelated history row",
        "signal_direction": "mixed",
    }], True)
    conn.close()
    assert extra["inserted"] == 1
    reused = _run(root, signals.parse_signal_request(_body("compare")), python=python, env=env)
    assert reused["reused"] is True
    assert reused["history_signals_before_ingest"] == 0
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == original_compare
    conn = connect_db(db_path)
    conn.execute("DELETE FROM signals_fts WHERE record_key = ?", ("earnings:extra-doc:2026-11-10",))
    conn.execute("DELETE FROM signals WHERE external_id = ?", ("extra-doc",))
    conn.close()

    changed = json.loads(json.dumps(records))
    changed["records"][0]["summary"] = "新的摘要"
    resaved = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=changed,
    )), python=python, env=env)
    assert resaved["reused"] is False
    assert resaved["bundle_hash"] != first_hash
    refreshed = _run(root, signals.parse_signal_request(_body("compare")), python=python, env=env)
    assert refreshed["reused"] is False
    assert refreshed["history_signals_before_ingest"] == 0
    assert refreshed["bundle_hash"] == resaved["bundle_hash"]
    stored = (_generation(root, DATE) / "compare_result.json").read_bytes()
    assert hashlib.sha256(stored).hexdigest() == refreshed["content_sha256"]
    pages: list[str] = []
    offset = 0
    digest = ""
    total = -1
    while True:
        page = _run(root, signals.parse_signal_request(_body("read", offset=offset, max_chars=64)), python=python)
        assert page["ok"] is True
        pages.append(page["text"])
        digest = page["content_sha256"]
        total = page["total_chars"]
        if page["next_offset"] is None:
            assert page["complete"] is True
            break
        assert page["complete"] is False
        assert page["next_offset"] == offset + len(page["text"])
        offset = page["next_offset"]
    text = "".join(pages)
    assert len(text) == total
    assert text == stored.decode("utf-8")
    assert hashlib.sha256(stored).hexdigest() == digest
    assert "新的摘要" in text
    assert str(root) not in text
    past = _run(root, signals.parse_signal_request(_body("read", offset=total + 1, max_chars=64)), python=python)
    assert past["code"] == "offset_past_end"
    assert "text" not in past

    ingested = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env)
    assert ingested["ok"] is True
    assert ingested["inserted"] == 2
    assert ingested["updated"] == 0
    assert ingested["signals"] == 2
    assert ingested["embedded_signals"] == 0
    assert ingested["embedding_status"] == "none"
    assert ingested["configured_embedding_model"] == "gemini-embedding-2"
    assert ingested["configured_embedding_dim"] == 768
    assert {item.rpartition(": ")[0] for item in ingested["embedding_errors"]} == {
        changed["records"][0]["record_key"],
        changed["records"][1]["record_key"],
    }
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == stored
    assert (_generation(root, DATE) / "outcome.json").is_file()
    assert not (_generation(root, DATE) / "ingest.json").exists()
    conn = connect_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM signals_fts").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE embedding IS NULL").fetchone()[0] == 2
    external_ids = {row[0] for row in conn.execute("SELECT external_id FROM signals")}
    conn.close()
    assert external_ids == {"doc-signal", "acme/alpha"}
    assert "doc-watch" not in external_ids
    for path in root.glob("ai_signal_memory.sqlite*"):
        assert MARKER.encode() not in path.read_bytes()

    calls: list[str] = []

    def boom(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        calls.append(argv[4])
        return _child(returncode=1)

    _rewrite_commit(root, DATE, status="ingesting")
    adopted = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=boom)
    assert calls == []
    assert adopted["reused"] is True
    assert adopted["inserted"] == 2
    assert adopted["status"] == "ingested"
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == stored

    outcome = _generation(root, DATE) / "outcome.json"
    outcome.unlink()
    _rewrite_commit(root, DATE, status="ingesting", outcome_sha256=None)
    recovered = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env)
    assert recovered["inserted"] == 0
    assert recovered["updated"] == 2
    assert recovered["signals"] == 2
    assert recovered["embedding_errors"]
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == stored
    conn = connect_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    hashes = {
        row["record_key"]: (row["content_hash"], bytes(row["embedding"]) if row["embedding"] is not None else None)
        for row in conn.execute("SELECT record_key, content_hash, embedding FROM signals")
    }
    blob = struct.pack("<768f", *([0.25] * 768))
    for record_key, (digest, embedding) in hashes.items():
        assert embedding is None
        canonical = canonical_record(next(item for item in changed["records"] if item["record_key"] == record_key))
        assert content_hash(canonical) == digest
        conn.execute(
            """UPDATE signals
               SET embedding = ?, embedding_dim = 768, embedding_model = ?
               WHERE record_key = ? AND content_hash = ?""",
            (blob, "gemini-embedding-2", record_key, digest),
        )
    conn.close()
    outcome.unlink()
    _rewrite_commit(root, DATE, status="ingesting", outcome_sha256=None)
    retried = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env)
    assert retried["ok"] is True
    assert retried["embedding_errors"] == []
    assert retried["embedded_signals"] == 2
    assert retried["embedding_status"] == "complete"
    assert retried["configured_embedding_dim"] == 768
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == stored
    conn = connect_db(db_path)
    for record_key, (digest, _embedding) in hashes.items():
        row = conn.execute(
            "SELECT content_hash, embedding FROM signals WHERE record_key = ?",
            (record_key,),
        ).fetchone()
        assert row["content_hash"] == digest
        assert bytes(row["embedding"]) == blob
    conn.close()

    repeated = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=boom)
    assert calls == []
    assert repeated["reused"] is True
    assert repeated["embedding_errors"] == []
    counted = _run(root, signals.parse_signal_request(_body("stats")), python=python, env=env, command=boom)
    assert calls == []
    assert counted["reused"] is True
    assert counted["signals"] == 2
    assert counted["configured_embedding_model"] == "gemini-embedding-2"
    assert counted["configured_embedding_dim"] == 768
    assert "db_bytes" not in counted
    assert "manifest_sha256" not in counted

    conn = connect_db(db_path)
    ingest_records(conn, [{
        "source_type": "earnings",
        "external_id": "after-ingest",
        "event_time": "2026-11-09T00:00:00+00:00",
        "summary": "row added after the snapshot",
        "signal_direction": "negative",
    }], True)
    conn.close()
    frozen_compare = _run(root, signals.parse_signal_request(_body("compare")), python=python, env=env)
    assert frozen_compare["reused"] is True
    assert frozen_compare["history_signals_before_ingest"] == 0
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == stored
    signal_bytes = (_generation(root, DATE) / "signal_records.json").read_bytes()
    replacement = json.loads(json.dumps(changed))
    replacement["records"][0]["summary"] = "should not replace the frozen bundle"
    frozen = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=replacement,
    )), python=python, env=env)
    assert frozen["code"] == "bundle_frozen"
    assert (_generation(root, DATE) / "signal_records.json").read_bytes() == signal_bytes
    assert (_generation(root, DATE) / "compare_result.json").read_bytes() == stored
    assert not (root / "locks" / "github-observation.lock").exists()


def test_public_ingest_retries_its_own_embedding_errors(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _write_prepared(root, DATE, OWNER)
    python = Path(sys.executable)
    env = _env(tmp_path)
    decisions, dispositions, records = _bundle()
    saved = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=python, env=env)
    assert saved["ok"] is True
    compared = _run(root, signals.parse_signal_request(_body("compare")), python=python, env=env)
    assert compared["ok"] is True and compared["reused"] is False
    generation = _generation(root, DATE)
    compare_bytes = (generation / "compare_result.json").read_bytes()
    bundle_bytes = {
        name: (generation / name).read_bytes()
        for name in ("github_decisions.json", "earnings_dispositions.json", "signal_records.json")
    }

    failed = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env)
    assert failed["ok"] is True and failed["reused"] is False
    assert failed["status"] == "ingested"
    assert failed["signals"] == 2 and failed["embedded_signals"] == 0
    assert failed["embedding_errors"]
    outcome = generation / "outcome.json"
    failed_outcome = outcome.read_bytes()
    commit = _commit(root, DATE)
    assert commit["status"] == "ingested"
    assert commit["outcome_sha256"] == hashlib.sha256(failed_outcome).hexdigest()
    assert commit["bundle_hash"] == saved["bundle_hash"]
    db_path = root / "ai_signal_memory.sqlite"
    conn = connect_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE embedding IS NULL").fetchone()[0] == 2
    hashes = {
        row["record_key"]: row["content_hash"]
        for row in conn.execute("SELECT record_key, content_hash FROM signals")
    }
    conn.close()
    frozen = {
        "bundle_hash": commit["bundle_hash"],
        "generation": commit["generation"],
        "compare_sha256": commit["compare_sha256"],
    }

    seen: list[list[str]] = []
    blob = struct.pack("<768f", *([0.25] * 768))

    def fill(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        seen.append(list(argv))
        action = argv[4]
        db = Path(argv[3])
        if action == "ingest":
            conn = connect_db(db)
            try:
                rows = conn.execute(
                    "SELECT record_key, content_hash FROM signals WHERE embedding IS NULL"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """UPDATE signals
                           SET embedding = ?, embedding_dim = 768, embedding_model = ?
                           WHERE record_key = ? AND content_hash = ?""",
                        (blob, "gemini-embedding-2", row["record_key"], row["content_hash"]),
                    )
            finally:
                conn.close()
            return _child(stdout=json.dumps({"inserted": 0, "updated": 2, "embedding_errors": []}))
        if action == "stats":
            conn = connect_db(db)
            try:
                payload = stats(conn)
            finally:
                conn.close()
            return _child(stdout=json.dumps(payload))
        return _child(returncode=1)

    filled = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=fill)
    assert [item[4] for item in seen] == ["ingest", "stats"]
    assert "--no-embeddings" not in seen[0]
    assert seen[0][3] == str(db_path)
    assert seen[0][seen[0].index("--input") + 1] == str(generation / "signal_records.json")
    assert filled["ok"] is True and filled["reused"] is False
    assert filled["embedding_errors"] == []
    assert filled["embedded_signals"] == 2
    assert filled["embedding_status"] == "complete"
    assert filled["status"] == "ingested"
    assert filled["bundle_hash"] == frozen["bundle_hash"]
    assert outcome.is_file()
    assert outcome.read_bytes() != failed_outcome
    stored = json.loads(outcome.read_text(encoding="utf-8"))
    assert stored["ingest"]["embedding_errors"] == []
    assert stored["bundle_hash"] == frozen["bundle_hash"]
    updated_commit = _commit(root, DATE)
    assert updated_commit["status"] == "ingested"
    assert updated_commit["generation"] == frozen["generation"]
    assert updated_commit["bundle_hash"] == frozen["bundle_hash"]
    assert updated_commit["compare_sha256"] == frozen["compare_sha256"]
    assert updated_commit["outcome_sha256"] == hashlib.sha256(outcome.read_bytes()).hexdigest()
    assert (generation / "compare_result.json").read_bytes() == compare_bytes
    for name, raw in bundle_bytes.items():
        assert (generation / name).read_bytes() == raw
    conn = connect_db(db_path)
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    for record_key, digest in hashes.items():
        row = conn.execute(
            "SELECT content_hash, embedding FROM signals WHERE record_key = ?",
            (record_key,),
        ).fetchone()
        assert row["content_hash"] == digest
        assert bytes(row["embedding"]) == blob
    counted = stats(conn)
    conn.close()
    assert counted["embedded_signals"] == 2
    assert counted["embedding_status"] == "complete"
    outcome_after = outcome.read_bytes()
    commit_after = (root / "signals" / DATE / "commit.json").read_bytes()

    again: list[str] = []

    def boom(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        again.append(argv[4])
        return _child(returncode=1)

    repeated = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=boom)
    assert again == []
    assert repeated["reused"] is True
    assert repeated["embedding_errors"] == []
    assert repeated["embedded_signals"] == 2
    assert outcome.read_bytes() == outcome_after
    assert (root / "signals" / DATE / "commit.json").read_bytes() == commit_after
    assert (generation / "compare_result.json").read_bytes() == compare_bytes


def test_retry_adopts_outcome_written_before_the_final_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state"
    _write_prepared(root, DATE, OWNER)
    python = Path(sys.executable)
    env = _env(tmp_path)
    decisions, dispositions, records = _bundle()
    saved = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=python, env=env)
    assert saved["ok"] is True
    compared = _run(root, signals.parse_signal_request(_body("compare")), python=python, env=env)
    assert compared["ok"] is True
    generation = _generation(root, DATE)
    compare_bytes = (generation / "compare_result.json").read_bytes()
    bundle_bytes = {
        name: (generation / name).read_bytes()
        for name in ("github_decisions.json", "earnings_dispositions.json", "signal_records.json")
    }
    failed = _run(root, signals.parse_signal_request(_body("ingest")), python=python, env=env)
    assert failed["ok"] is True and failed["embedding_errors"] and failed["embedded_signals"] == 0
    outcome = generation / "outcome.json"
    commit = _commit(root, DATE)
    frozen = {
        "bundle_hash": commit["bundle_hash"],
        "generation": commit["generation"],
        "compare_sha256": commit["compare_sha256"],
    }
    db_path = root / "ai_signal_memory.sqlite"
    conn = connect_db(db_path)
    before = {
        row["record_key"]: row["content_hash"]
        for row in conn.execute("SELECT record_key, content_hash FROM signals")
    }
    conn.close()
    assert len(before) == 2

    seen: list[str] = []
    blob = struct.pack("<768f", *([0.5] * 768))

    def fill(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        action = argv[4]
        seen.append(action)
        db = Path(argv[3])
        if action == "ingest":
            conn = connect_db(db)
            try:
                rows = conn.execute(
                    "SELECT record_key, content_hash FROM signals"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """UPDATE signals
                           SET embedding = ?, embedding_dim = 768, embedding_model = ?
                           WHERE record_key = ? AND content_hash = ?""",
                        (blob, "gemini-embedding-2", row["record_key"], row["content_hash"]),
                    )
            finally:
                conn.close()
            return _child(stdout=json.dumps({"inserted": 0, "updated": 2, "embedding_errors": []}))
        if action == "stats":
            conn = connect_db(db)
            try:
                payload = stats(conn)
            finally:
                conn.close()
            return _child(stdout=json.dumps(payload))
        return _child(returncode=1)

    real_replace = signals._replace_commit

    def fail_final(root_path: Path, current: dict[str, Any], updated: dict[str, Any]) -> bool:
        if updated.get("status") == "ingested":
            return False
        return real_replace(root_path, current, updated)

    monkeypatch.setattr(signals, "_replace_commit", fail_final)
    interrupted = _run(
        root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=fill,
    )
    monkeypatch.setattr(signals, "_replace_commit", real_replace)
    assert interrupted["code"] == "commit_inconsistent"
    assert seen == ["ingest", "stats"]
    mid = _commit(root, DATE)
    assert mid["status"] == "ingesting"
    assert mid["outcome_sha256"] is None
    assert mid["bundle_hash"] == frozen["bundle_hash"]
    assert mid["generation"] == frozen["generation"]
    assert mid["compare_sha256"] == frozen["compare_sha256"]
    written = json.loads(outcome.read_text(encoding="utf-8"))
    assert written["ingest"]["embedding_errors"] == []
    assert written["bundle_hash"] == frozen["bundle_hash"]
    assert (generation / "compare_result.json").read_bytes() == compare_bytes
    for name, raw in bundle_bytes.items():
        assert (generation / name).read_bytes() == raw
    outcome_bytes = outcome.read_bytes()
    conn = connect_db(db_path)
    filled_rows = {
        row["record_key"]: (row["content_hash"], bytes(row["embedding"]))
        for row in conn.execute("SELECT record_key, content_hash, embedding FROM signals")
    }
    conn.close()
    assert set(filled_rows) == set(before)
    for record_key, digest in before.items():
        assert filled_rows[record_key] == (digest, blob)

    calls: list[str] = []

    def boom(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        calls.append(argv[4])
        return _child(returncode=1)

    recovered = _run(
        root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=boom,
    )
    assert calls == []
    assert recovered["ok"] is True and recovered["reused"] is True
    assert recovered["status"] == "ingested"
    assert recovered["embedding_errors"] == []
    assert recovered["embedded_signals"] == 2
    assert recovered["signals"] == 2
    finished = _commit(root, DATE)
    assert finished["status"] == "ingested"
    assert finished["outcome_sha256"] == hashlib.sha256(outcome_bytes).hexdigest()
    assert finished["bundle_hash"] == frozen["bundle_hash"]
    assert finished["generation"] == frozen["generation"]
    assert finished["compare_sha256"] == frozen["compare_sha256"]
    assert outcome.read_bytes() == outcome_bytes
    assert (generation / "compare_result.json").read_bytes() == compare_bytes
    conn = connect_db(db_path)
    recovered_rows = {
        row["record_key"]: (row["content_hash"], bytes(row["embedding"]))
        for row in conn.execute("SELECT record_key, content_hash, embedding FROM signals")
    }
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    conn.close()
    assert recovered_rows == filled_rows
    commit_bytes = (root / "signals" / DATE / "commit.json").read_bytes()

    repeated = _run(
        root, signals.parse_signal_request(_body("ingest")), python=python, env=env, command=boom,
    )
    assert calls == []
    assert repeated["reused"] is True
    assert repeated["embedding_errors"] == []
    assert outcome.read_bytes() == outcome_bytes
    assert (root / "signals" / DATE / "commit.json").read_bytes() == commit_bytes
    assert (generation / "compare_result.json").read_bytes() == compare_bytes


def test_child_argv_keeps_the_interpreter_and_strips_secrets(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _write_prepared(root, DATE, OWNER)
    link = _python_link(tmp_path)
    captured: list[dict[str, Any]] = []

    def command(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        captured.append({
            "argv": list(argv),
            "env": dict(env),
            "timeout_seconds": timeout_seconds,
            "stdout_limit_bytes": stdout_limit_bytes,
            "stderr_limit_bytes": stderr_limit_bytes,
            "input_bytes": input_bytes,
            "on_spawn": on_spawn,
        })
        if argv[4] == "compare":
            _compare_bytes_of(argv, "中文信号")
            return _child(stdout="")
        raise AssertionError(argv)

    decisions, dispositions, records = _bundle("argv summary should stay in the file")
    _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=link)
    result = _run(
        root,
        signals.parse_signal_request(_body("compare")),
        python=link,
        env=_env(tmp_path, api_key=API_KEY),
        command=command,
    )
    assert result["ok"] is True
    argv = captured[0]["argv"]
    assert argv[0] == str(link)
    assert argv[1].endswith("ai_finance_signal_memory.py")
    assert Path(argv[1]).is_absolute()
    assert argv[2] == "--db"
    assert argv[3] == str(root / "ai_signal_memory.sqlite")
    assert argv[4] == "compare"
    assert argv[argv.index("--limit") + 1] == "8"
    assert "--output" in argv and "--input" in argv
    assert argv[argv.index("--input") + 1].endswith("signal_records.json")
    assert "--no-embeddings" not in argv
    assert captured[0]["input_bytes"] is None
    assert captured[0]["timeout_seconds"] == 900.0
    joined = "\n".join(argv)
    assert "argv summary should stay in the file" not in joined
    assert API_KEY not in joined
    child_env = captured[0]["env"]
    assert child_env["AI_SIGNAL_EMBEDDING_MODEL"] == "gemini-embedding-2"
    assert child_env["AI_SIGNAL_EMBEDDING_DIM"] == "768"
    assert child_env["GEMINI_API_KEY"] == API_KEY
    assert "OPENAI_API_KEY" not in child_env
    assert "DALTON_BRIDGE_ADMIN_TOKEN" not in child_env
    assert "base-key-must-not-pass" not in child_env.values()


def test_shared_database_lock_keeps_ingest_behind_compare(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _write_prepared(root, DATE, OWNER)
    _write_prepared(root, DATE2, OWNER)
    link = _python_link(tmp_path)
    env = _env(tmp_path)
    decisions, dispositions, records = _bundle()
    for batch_id in (DATE, DATE2):
        saved = _run(root, signals.parse_signal_request(_body(
            "save",
            batch_id=batch_id,
            github_decisions=decisions,
            earnings_dispositions=dispositions,
            signal_records=records,
        )), python=link, env=env)
        assert saved["ok"] is True
    date1_inside = threading.Event()
    release = threading.Event()
    ingest_entered = threading.Event()
    entered_at: list[float] = []
    released_at: list[float] = []

    def command(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        action = argv[4]
        if action == "compare":
            target = argv[argv.index("--input") + 1]
            _compare_bytes_of(argv, "lock")
            if f"/{DATE}/" in target:
                date1_inside.set()
                assert release.wait(5)
                released_at.append(time.monotonic())
            return _child()
        if action == "ingest":
            entered_at.append(time.monotonic())
            ingest_entered.set()
            return _child(stdout=json.dumps({"inserted": 2, "updated": 0, "embedding_errors": []}))
        if action == "stats":
            return _child(stdout=_stats_stdout())
        raise AssertionError(argv)

    prepared = _run(root, signals.parse_signal_request(_body("compare", batch_id=DATE2)), python=link, env=env, command=command)
    assert prepared["ok"] is True
    date2_bytes = (_generation(root, DATE2) / "compare_result.json").read_bytes()
    errors: list[BaseException] = []

    def date1_compare() -> None:
        try:
            result = _run(
                root,
                signals.parse_signal_request(_body("compare", batch_id=DATE)),
                python=link,
                env=env,
                command=command,
            )
            assert result["ok"] is True
        except BaseException as exc:  # noqa: BLE001 - the thread must report the failure
            errors.append(exc)

    def date2_ingest() -> None:
        try:
            result = _run(
                root,
                signals.parse_signal_request(_body("ingest", batch_id=DATE2)),
                python=link,
                env=env,
                command=command,
            )
            assert result["ok"] is True
        except BaseException as exc:  # noqa: BLE001 - the thread must report the failure
            errors.append(exc)

    first = threading.Thread(target=date1_compare)
    second = threading.Thread(target=date2_ingest)
    first.start()
    assert date1_inside.wait(5)
    second.start()
    time.sleep(0.4)
    assert not ingest_entered.is_set()
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert ingest_entered.is_set()
    assert entered_at[0] >= released_at[0]
    assert (_generation(root, DATE2) / "compare_result.json").read_bytes() == date2_bytes


def test_kill_owned_group_reaps_the_sleeper() -> None:
    started = threading.Event()

    def on_spawn(pid: int) -> None:
        started.set()
        signals.kill_owned_group(pid)

    result = run_json_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        timeout_seconds=5,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=1024,
        on_spawn=on_spawn,
        input_bytes=None,
    )
    assert started.is_set()
    assert result.still_running is False
    assert result.timed_out is False


class _NullCM:
    async def __aenter__(self) -> _NullCM:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Cursor:
    def __init__(self, clock: dict[str, Any]) -> None:
        self.clock = clock

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, sql: str, params: Any = None) -> None:
        del sql, params

    async def fetchone(self) -> dict[str, Any]:
        return dict(self.clock)


class _Conn:
    def __init__(self, clock: dict[str, Any]) -> None:
        self.clock = clock

    def transaction(self) -> _NullCM:
        return _NullCM()

    def cursor(self, row_factory: Any = None) -> _Cursor:
        del row_factory
        return _Cursor(self.clock)


class _Pool:
    def __init__(self, clock: dict[str, Any]) -> None:
        self.clock = clock
        self.held = 0
        self.max_held = 0

    def __call__(self, conn: Any = None) -> Any:
        del conn
        pool = self

        class _CM:
            async def __aenter__(self) -> _Conn:
                if pool.held != 0:
                    raise RuntimeError("max_size=1 exhausted")
                pool.held += 1
                pool.max_held = max(pool.max_held, pool.held)
                return _Conn(pool.clock)

            async def __aexit__(self, *_args: object) -> bool:
                pool.held -= 1
                return False

        return _CM()


def _config(**overrides: Any) -> dict[str, Any]:
    configurable = {
        "user_id": OWNER.upper(),
        "workspace_id": WORKSPACE.upper(),
        "thread_id": THREAD.upper(),
        "run_id": RUN.upper(),
        "turn_origin_automation_id": AUTOMATION.upper(),
        "automation_execution_id": EXECUTION.upper(),
    }
    configurable.update(overrides)
    return {"configurable": configurable}


def _row() -> dict[str, Any]:
    return {
        "automation_execution_id": EXECUTION,
        "automation_id": AUTOMATION,
        "status": "running",
        "conversation_thread_id": THREAD,
        "conversation_response_id": RUN,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "metadata": {
            "report_profile": {
                "profile": "ai-finance-daily",
                "report_targets": ["feishu:chat:oc_marketdigest"],
                "failure_targets": [],
            }
        },
    }


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> SimpleNamespace:
    clock = {
        "scheduled_at": datetime(2026, 11, 11, 20, 0, tzinfo=HKT),
        "started_at": datetime(2026, 11, 11, 20, 5, tzinfo=HKT),
    }
    pool = _Pool(clock)
    http_calls: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    owner_value: list[str | None] = [OWNER]
    internal_open = [True]
    real_auth = ai_finance_db.authorize_ai_finance_execution

    async def _lock(cur: Any, execution_id: str, *, for_update: bool) -> dict[str, Any]:
        del cur
        assert for_update is False
        assert execution_id == EXECUTION
        return _row()

    async def _auth(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return await real_auth(**kwargs)

    async def _internal(user_id: str | None) -> bool:
        return internal_open[0] and bool(user_id)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        http_calls.append({
            "timeout": kwargs.get("timeout"),
            "trust_env": kwargs.get("trust_env"),
            "held": pool.held,
        })
        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(ai_finance_db, "get_db_connection", pool)
    monkeypatch.setattr(
        ai_finance_db,
        "lock_report_execution",
        create_autospec(lock_report_execution, side_effect=_lock),
    )
    monkeypatch.setattr(
        signal_tools,
        "authorize_ai_finance_execution",
        create_autospec(real_auth, side_effect=_auth),
    )
    monkeypatch.setattr(
        signal_tools,
        "is_internal_or_admin",
        create_autospec(is_internal_or_admin, side_effect=_internal),
    )
    monkeypatch.setattr(
        signal_tools,
        "get_ai_finance_owner_user_id",
        create_autospec(get_ai_finance_owner_user_id, side_effect=lambda: owner_value[0]),
    )
    monkeypatch.setattr(
        signal_tools,
        "get_dalton_bridge_admin_token",
        create_autospec(get_dalton_bridge_admin_token, return_value=ADMIN_TOKEN),
    )
    monkeypatch.setattr(
        signal_tools,
        "get_dalton_bridge_base_url",
        create_autospec(get_dalton_bridge_base_url, return_value="http://bridge.invalid"),
    )
    monkeypatch.setattr(signal_tools.httpx, "AsyncClient", factory)
    return SimpleNamespace(
        pool=pool,
        http_calls=http_calls,
        seen=seen,
        owner_value=owner_value,
        internal_open=internal_open,
        tool=signal_tools.create_ai_finance_signals_tools()[0],
    )


def _host_compare() -> dict[str, Any]:
    return {
        "ok": True,
        "action": "compare",
        "code": "ok",
        "batch_id": DATE,
        "profile": "ai-finance-daily",
        "bundle_hash": "a" * 64,
        "manifest_sha256": "b" * 64,
        "status": "open",
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
        "reused": False,
        "search_modes": ["empty_history"],
        "history_signals_before_ingest": 0,
        "history_embedded_signals_before_ingest": 0,
        "history_embedding_status_before_ingest": "empty",
        "comparison_count": 2,
        "content_sha256": "c" * 64,
        "total_chars": 12,
    }


@pytest.mark.asyncio
async def test_tool_gate_releases_the_database_before_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["action"] == "compare"
        assert "github_decisions" not in body
        assert request.url.path == "/internal/ai-finance/signals"
        assert request.headers["authorization"] == f"Bearer {ADMIN_TOKEN}"
        return httpx.Response(200, json=_host_compare())

    installed = _install(monkeypatch, handler)
    chat = json.loads(await installed.tool.ainvoke(
        {"action": "compare", "batch_id": DATE},
        config={"configurable": {}},
    ))
    assert chat["code"] == "not_automation_run"
    installed.owner_value[0] = OTHER
    wrong_owner = json.loads(await installed.tool.ainvoke(
        {"action": "compare", "batch_id": DATE},
        config=_config(),
    ))
    assert wrong_owner["code"] == "not_allowed"
    installed.owner_value[0] = OWNER
    installed.internal_open[0] = False
    external = json.loads(await installed.tool.ainvoke(
        {"action": "stats", "batch_id": DATE},
        config=_config(),
    ))
    assert external["code"] == "not_allowed"
    installed.internal_open[0] = True
    early = json.loads(await installed.tool.ainvoke(
        {"action": "compare", "batch_id": DATE, "github_decisions": {"records": []}},
        config=_config(),
    ))
    assert early["code"] == "invalid_request"
    missing = json.loads(await installed.tool.ainvoke(
        {"action": "save", "batch_id": DATE, "github_decisions": {"records": []}},
        config=_config(),
    ))
    assert missing["code"] == "bundle_invalid"
    bad_batch = json.loads(await installed.tool.ainvoke(
        {"action": "read", "batch_id": "/tmp/secret"},
        config=_config(),
    ))
    assert bad_batch["code"] == "invalid_batch"
    assert installed.http_calls == []
    assert installed.seen == []

    result = json.loads(await installed.tool.ainvoke(
        {"action": "compare", "batch_id": DATE},
        config=_config(),
    ))
    assert result["ok"] is True
    assert result["history_signals_before_ingest"] == 0
    assert "comparisons" not in result
    assert "text" not in result
    assert installed.pool.held == 0
    assert installed.pool.max_held == 1
    assert installed.http_calls[0]["held"] == 0
    assert installed.http_calls[0]["trust_env"] is False
    assert installed.http_calls[0]["timeout"] == 960.0
    assert installed.seen[0]["execution_id"] == EXECUTION

    def hostile(_request: httpx.Request) -> httpx.Response:
        payload = _host_compare()
        payload["comparisons"] = [{"current": {"summary": MARKER}}]
        payload["path"] = "/tmp/state"
        return httpx.Response(200, json=payload)

    hostile_install = _install(monkeypatch, hostile)
    rejected = json.loads(await hostile_install.tool.ainvoke(
        {"action": "compare", "batch_id": DATE},
        config=_config(),
    ))
    assert rejected["code"] == "invalid_response"
    assert MARKER not in json.dumps(rejected)
    assert "/tmp/state" not in json.dumps(rejected)


def _bridge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: Path | None, env_text: str) -> rb.ResearchDataBridge:
    tmp_path.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "bridge.env"
    env_file.write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", OWNER)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    if config is None:
        monkeypatch.delenv("DALTON_AI_FINANCE_CONFIG", raising=False)
    else:
        monkeypatch.setenv("DALTON_AI_FINANCE_CONFIG", str(config))
    return rb.ResearchDataBridge(rb.BridgeConfig())


async def _drive(
    bridge: rb.ResearchDataBridge,
    *,
    path: str,
    body: bytes,
    token: str | None,
    method: str = "POST",
    content_type: str = "application/json",
) -> tuple[int, dict[str, Any]]:
    header_lines = ["Host: 127.0.0.1", f"Content-Type: {content_type}"]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        + "\r\n".join(header_lines)
        + f"\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    await rb._handle_client(bridge, reader, writer)
    written = b"".join(call.args[0] for call in writer.write.call_args_list)
    status = int(written.split(b"\r\n", 1)[0].decode("utf-8").split(" ")[1])
    parsed = json.loads(written.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    return status, parsed


@pytest.mark.asyncio
async def test_admin_route_rejects_tokens_and_runs_one_real_compare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: list[dict[str, Any]] = []
    original_handler = rb.ResearchDataBridge.handle_ai_finance_signals
    previous_key = os.environ.get("GEMINI_API_KEY")
    previous_openai = os.environ.get("OPENAI_API_KEY")

    async def _record(self: rb.ResearchDataBridge, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        del self
        called.append(body)
        return 200, {"ok": True}

    monkeypatch.setattr(rb.ResearchDataBridge, "handle_ai_finance_signals", _record)
    try:
        gate = _bridge(monkeypatch, tmp_path, None, "")
        missing, missing_body = await _drive(
            gate,
            path="/internal/ai-finance/signals",
            body=b"{}",
            token=None,
        )
        assert missing == 401
        assert missing_body == {"ok": False, "error": "Unauthorized"}
        forbidden, forbidden_body = await _drive(
            gate,
            path="/internal/ai-finance/signals",
            body=b"{}",
            token=BRIDGE_TOKEN,
        )
        assert forbidden == 403
        assert forbidden_body == {"ok": False, "error": "Forbidden"}
        assert called == []

        root = tmp_path / "state"
        _write_prepared(root, DATE, OWNER)
        link = _python_link(tmp_path)
        config = tmp_path / "operator.json"
        config.write_text(json.dumps({
            "state_root": str(root),
            "python_executable": str(link),
            "embedding_model": "gemini-embedding-2",
            "embedding_dim": 768,
        }), encoding="utf-8")
        bridge = _bridge(
            monkeypatch,
            tmp_path / "bridge-home",
            config,
            f"GEMINI_API_KEY={API_KEY}\nOPENAI_API_KEY=sk-should-not-reach-the-child\n",
        )
        monkeypatch.setattr(rb.ResearchDataBridge, "handle_ai_finance_signals", original_handler)
        seen_env: list[dict[str, str]] = []
        module = rb._ai_finance_signals()
        real_build = module.build_signal_subprocess_env

        def spy(base_env: dict[str, str], **kwargs: Any) -> dict[str, str]:
            built = real_build(base_env, **kwargs)
            seen_env.append(built)
            return built

        monkeypatch.setattr(module, "build_signal_subprocess_env", spy)
        decisions, dispositions, records = _bundle()
        save_body = _body(
            "save",
            github_decisions=decisions,
            earnings_dispositions=dispositions,
            signal_records=records,
        )
        status, payload = await _drive(
            bridge,
            path="/internal/ai-finance/signals",
            body=json.dumps(save_body).encode(),
            token=ADMIN_TOKEN,
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["status"] == "open"
        assert str(root) not in json.dumps(payload)
        assert API_KEY not in json.dumps(payload)
        extra = dict(save_body)
        extra["shell"] = "rm"
        rejected, rejected_body = await _drive(
            bridge,
            path="/internal/ai-finance/signals",
            body=json.dumps(extra).encode(),
            token=ADMIN_TOKEN,
        )
        assert rejected == 400
        assert rejected_body["code"] == "unknown_field"
        assert "rm" not in json.dumps(rejected_body)
        method, method_body = await _drive(
            bridge,
            path="/internal/ai-finance/signals",
            body=b"{}",
            token=ADMIN_TOKEN,
            method="GET",
        )
        assert method == 405
        assert method_body == {"ok": False, "error": "Method Not Allowed"}
        compare_status, compare_body = await _drive(
            bridge,
            path="/internal/ai-finance/signals",
            body=json.dumps(_body("compare")).encode(),
            token=ADMIN_TOKEN,
        )
        assert compare_status == 200
        assert compare_body["search_modes"] == ["empty_history"]
        assert compare_body["history_signals_before_ingest"] == 0
        assert "comparisons" not in compare_body
        assert API_KEY not in json.dumps(compare_body)
        assert seen_env
        assert seen_env[-1]["GEMINI_API_KEY"] == API_KEY
        assert seen_env[-1]["AI_SIGNAL_EMBEDDING_DIM"] == "768"
        assert "OPENAI_API_KEY" not in seen_env[-1]
        assert "DALTON_BRIDGE_ADMIN_TOKEN" not in seen_env[-1]
        assert (root / "ai_signal_memory.sqlite").is_file()
        wrong, wrong_body = await _drive(
            bridge,
            path="/internal/ai-finance/signals",
            body=json.dumps(_body("stats", owner_user_id=OTHER, user_id=OTHER)).encode(),
            token=ADMIN_TOKEN,
        )
        assert wrong == 409
        assert wrong_body["code"] == "owner_mismatch"
    finally:
        if previous_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = previous_key
        if previous_openai is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = previous_openai


@pytest.mark.asyncio
async def test_route_timeout_and_cancel_stop_one_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({
        "state_root": str(root),
        "python_executable": sys.executable,
    }), encoding="utf-8")
    previous_key = os.environ.get("GEMINI_API_KEY")
    bridge = _bridge(monkeypatch, tmp_path / "timeout-home", config, "")
    if previous_key is None:
        os.environ.pop("GEMINI_API_KEY", None)
    else:
        os.environ["GEMINI_API_KEY"] = previous_key
    module = rb._ai_finance_signals()
    calls = {"count": 0}
    cancelled_seen = threading.Event()

    def execute(parsed: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del parsed
        calls["count"] += 1
        kwargs["cancelled"].wait(5)
        cancelled_seen.set()
        return {"ok": False, "code": "signal_timeout", "error": "The signal operation did not finish before the timeout."}

    monkeypatch.setattr(module, "execute_signal", execute)
    monkeypatch.setattr(module, "operation_timeout_seconds", lambda action: 0.05)
    status, payload = await _drive(
        bridge,
        path="/internal/ai-finance/signals",
        body=json.dumps(_body("compare")).encode(),
        token=ADMIN_TOKEN,
    )
    assert status == 503
    assert payload["code"] == "signal_timeout"
    assert cancelled_seen.wait(2)
    assert calls["count"] == 1

    children: list[subprocess.Popen[bytes]] = []

    def blocking(parsed: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del parsed
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            children.append(proc)
            kwargs["on_spawn"](proc.pid)
            kwargs["cancelled"].wait(5)
            return {"ok": True, "action": "compare", "code": "ok"}
        finally:
            deadline = time.monotonic() + 2
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is not None:
                proc.wait(timeout=1)

    monkeypatch.setattr(module, "execute_signal", blocking)
    monkeypatch.setattr(module, "operation_timeout_seconds", lambda action: 30)
    task = asyncio.create_task(bridge.handle_ai_finance_signals(_body("compare")))
    owned: subprocess.Popen[bytes] | None = None
    try:
        for _ in range(50):
            if children:
                break
            await asyncio.sleep(0.05)
        assert len(children) == 1
        owned = children[0]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        deadline = time.monotonic() + 2
        while owned.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        returncode = owned.poll()
        assert returncode in {-signal.SIGTERM, -signal.SIGKILL}
        assert owned.wait(timeout=1) == returncode
        with pytest.raises(ProcessLookupError):
            os.kill(owned.pid, 0)
    finally:
        if owned is not None and owned.poll() is None:
            signals.kill_owned_group(owned.pid)
            try:
                owned.wait(timeout=2)
            except subprocess.TimeoutExpired:
                owned.kill()
                owned.wait(timeout=2)


_LEGAL_EMBEDDING_ERRORS = (
    "github:acme/token-agent:2026-11-11: RuntimeError",
    "github:acme/api-key:2026-11-11: HTTPError",
    "github:acme/secret-scan:2026-11-11: URLError",
    "earnings:key-risk: TimeoutError",
    "earnings:token-note: JSONDecodeError",
)
_SECRET_EMBEDDING_ERROR = (
    "github:acme/token-agent:2026-11-11: RuntimeError GEMINI_API_KEY=sk-live-secret-value"
)


def _ingest_tool_payload(errors: list[str]) -> dict[str, Any]:
    return {
        "ok": True,
        "action": "ingest",
        "code": "ok",
        "batch_id": DATE,
        "profile": signal_tools.PROFILE,
        "bundle_hash": "a" * 64,
        "manifest_sha256": "b" * 64,
        "status": "ingested",
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
        "reused": False,
        "inserted": 1,
        "updated": 0,
        "embedding_errors": errors,
        "signals": 1,
        "embedded_signals": 0,
        "embedding_status": "none",
        "embedding_coverage_pct": 0.0,
        "configured_embedding_model": "gemini-embedding-2",
        "configured_embedding_dim": 768,
    }


def _prepare_open_bundle(root: Path, command: Any) -> None:
    _write_prepared(root, DATE, OWNER)
    python = Path(sys.executable)
    decisions, dispositions, records = _bundle()
    saved = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=python)
    assert saved["ok"] is True
    compared = _run(
        root,
        signals.parse_signal_request(_body("compare")),
        python=python,
        env=_env(root),
        command=command,
    )
    assert compared["ok"] is True


def test_public_ingest_keeps_legal_embedding_identifiers(tmp_path: Path) -> None:
    root = tmp_path / "state"

    def command(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        action = argv[4]
        if action == "compare":
            _compare_bytes_of(argv, "token-agent")
            return _child()
        if action == "ingest":
            return _child(stdout=json.dumps({
                "inserted": 2,
                "updated": 0,
                "embedding_errors": list(_LEGAL_EMBEDDING_ERRORS),
            }))
        if action == "stats":
            return _child(stdout=_stats_stdout())
        return _child(returncode=1)

    _prepare_open_bundle(root, command)
    ingested = _run(
        root,
        signals.parse_signal_request(_body("ingest")),
        python=Path(sys.executable),
        env=_env(root),
        command=command,
    )
    assert ingested["ok"] is True
    assert ingested["embedding_errors"] == list(_LEGAL_EMBEDDING_ERRORS)
    assert "token-agent" in ingested["embedding_errors"][0]
    assert "api-key" in ingested["embedding_errors"][1]
    assert "secret-scan" in ingested["embedding_errors"][2]
    parsed = {"action": "ingest", "batch_id": DATE}
    accepted = signal_tools._accept_success(
        _ingest_tool_payload(list(_LEGAL_EMBEDDING_ERRORS)),
        parsed,
    )
    assert accepted is not None
    assert accepted["embedding_errors"] == list(_LEGAL_EMBEDDING_ERRORS)

    secret_root = tmp_path / "secret-state"
    marker = "sk-live-secret-value"

    def reject(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        action = argv[4]
        if action == "compare":
            _compare_bytes_of(argv, "secret")
            return _child()
        if action == "ingest":
            return _child(stdout=json.dumps({
                "inserted": 0,
                "updated": 2,
                "embedding_errors": [_SECRET_EMBEDDING_ERROR],
            }))
        return _child(returncode=1)

    _prepare_open_bundle(secret_root, reject)
    rejected = _run(
        secret_root,
        signals.parse_signal_request(_body("ingest")),
        python=Path(sys.executable),
        env=_env(secret_root),
        command=reject,
    )
    assert rejected["code"] == "signal_failed"
    rendered = json.dumps(rejected)
    assert marker not in rendered
    assert "GEMINI_API_KEY" not in rendered
    generation = _generation(secret_root, DATE)
    assert not (generation / "outcome.json").exists()
    malformed = (
        _SECRET_EMBEDDING_ERROR,
        "/Users/everflow/.ssh/id_secret: RuntimeError",
        "github:acme/token-agent:2026-11-11: token sk-live-secret-value",
        "earnings:../../secret: RuntimeError",
        "github:acme/token-agent:2026-13-40: RuntimeError",
    )
    for item in malformed:
        assert signal_tools._accept_success(_ingest_tool_payload([item]), parsed) is None
        assert signals._safe_embedding_error(item) is False


def _compare_over_two_mib() -> bytes:
    body = {
        "search_modes": ["fts"],
        "history_signals_before_ingest": 80,
        "history_embedded_signals_before_ingest": 0,
        "history_embedding_status_before_ingest": "none",
        "comparisons": [
            {
                "current": {"record_key": f"earnings:current-{index}", "evidence": "current"},
                "matches": [
                    {
                        "record_key": f"earnings:history-{hit}",
                        "summary": "historical",
                        "evidence": "中" + "e" * 3999,
                    }
                    for hit in range(8)
                ],
            }
            for index in range(80)
        ],
    }
    return (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode()


def test_compare_above_two_mib_is_readable_and_reused(tmp_path: Path) -> None:
    raw = _compare_over_two_mib()
    assert len(raw) > 2 * 1024 * 1024
    assert len(raw) <= signals._COMPARE_FILE_MAX
    root = tmp_path / "state"
    calls: list[str] = []

    def command(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        action = argv[4]
        calls.append(action)
        if action == "compare":
            Path(argv[argv.index("--output") + 1]).write_bytes(raw)
            return _child()
        return _child(returncode=1)

    _prepare_open_bundle(root, command)
    generation = _generation(root, DATE)
    stored = (generation / "compare_result.json").read_bytes()
    assert stored == raw
    assert calls == ["compare"]
    calls.clear()
    compared = _run(
        root,
        signals.parse_signal_request(_body("compare")),
        python=Path(sys.executable),
        env=_env(root),
        command=command,
    )
    assert calls == []
    assert compared["ok"] is True
    assert compared["reused"] is True
    assert compared["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert compared["total_chars"] == len(raw.decode("utf-8"))

    page = _run(
        root,
        signals.parse_signal_request(_body("read", offset=0, max_chars=16000)),
        python=Path(sys.executable),
    )
    assert page["ok"] is True
    assert page["next_offset"] is not None
    assert page["complete"] is False
    assert page["content_sha256"] == compared["content_sha256"]
    assert page["total_chars"] == compared["total_chars"]
    assert page["text"] == raw.decode("utf-8")[: len(page["text"])]
    assert len(json.dumps(page, ensure_ascii=False).encode()) <= signals.MAX_RESPONSE_BYTES
    assert signals.MAX_RESPONSE_BYTES == 128 * 1024
    assert signal_tools._MAX_HOST_RESPONSE_BYTES == 128 * 1024
    follow = _run(
        root,
        signals.parse_signal_request(_body("read", offset=page["next_offset"], max_chars=16000)),
        python=Path(sys.executable),
    )
    assert follow["ok"] is True
    assert follow["offset"] == page["next_offset"]
    assert follow["text"]
    assert follow["content_sha256"] == page["content_sha256"]
    assert (generation / "compare_result.json").read_bytes() == raw


def test_compare_refuses_file_over_the_stored_cap(tmp_path: Path) -> None:
    root = tmp_path / "state"

    def command(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        if argv[4] != "compare":
            return _child(returncode=1)
        path = Path(argv[argv.index("--output") + 1])
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.ftruncate(fd, signals._COMPARE_FILE_MAX + 1)
        finally:
            os.close(fd)
        return _child()

    _write_prepared(root, DATE, OWNER)
    python = Path(sys.executable)
    decisions, dispositions, records = _bundle()
    saved = _run(root, signals.parse_signal_request(_body(
        "save",
        github_decisions=decisions,
        earnings_dispositions=dispositions,
        signal_records=records,
    )), python=python)
    assert saved["ok"] is True
    refused = _run(
        root,
        signals.parse_signal_request(_body("compare")),
        python=python,
        env=_env(root),
        command=command,
    )
    assert refused["code"] == "signal_failed"
    generation = _generation(root, DATE)
    assert not (generation / "compare_result.json").exists()
    assert list(generation.glob(".compare-out-*")) == []
    assert _commit(root, DATE)["status"] == "open"
    assert _commit(root, DATE)["compare_sha256"] is None


def _compare_past_source_offset() -> bytes:
    """Indent-2 compare JSON past 32MiB, with each text field at the 4000-character limit.

    Quote characters expand to backslash-quote in the stored JSON. The saved
    bundle stays the compact accepted current; only historical matches repeat.
    """
    quoted = '"' * signals._MAX_STRING
    comparisons = []
    for index in range(64):
        matches = []
        for hit in range(signals.COMPARE_LIMIT):
            matches.append({
                "record_key": f"earnings:history-{index}-{hit}",
                "source_type": "earnings",
                "external_id": f"history-{index}-{hit}",
                "event_time": "2026-11-10T00:00:00+00:00",
                "title": quoted,
                "entity": quoted,
                "industry": quoted,
                "summary": quoted,
                "evidence": quoted,
                "qa_concern": quoted,
                "management_response": quoted,
                "tam_mapping": quoted,
                "investment_implication": quoted,
                "evidence_status": "supported",
                "signal_types": ["cost_productivity"],
                "signal_direction": "positive",
                "management_response_status": "direct",
                "url": "https://example.com/history",
                "embedding_similarity": None,
                "shared_tags": ["ai"],
            })
        comparisons.append({"current": _earnings("AI revenue grew"), "matches": matches})
    body = {
        "search_modes": ["fts"],
        "history_signals_before_ingest": 64 * signals.COMPARE_LIMIT,
        "history_embedded_signals_before_ingest": 0,
        "history_embedding_status_before_ingest": "none",
        "comparisons": comparisons,
    }
    return (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode()


def test_public_read_past_source_offset_reaches_compare_file(tmp_path: Path) -> None:
    raw = _compare_past_source_offset()
    full = raw.decode("utf-8")
    old_cap = 32 * 1024 * 1024
    bound = signals._READ_OFFSET_MAX
    assert old_cap == signals._SOURCE_MAX_BYTES
    assert bound == signals._COMPARE_FILE_MAX == signal_tools._MAX_OFFSET
    assert old_cap < len(full) <= bound
    assert len(raw) <= signals._COMPARE_FILE_MAX
    assert signals.MAX_CHARS == 32000
    assert signals.MAX_RESPONSE_BYTES == 128 * 1024
    assert signals._OBJECT_MAX_BYTES == 256 * 1024
    assert signal_tools._MAX_CHARS == 32000
    assert signal_tools._MAX_HOST_RESPONSE_BYTES == 128 * 1024
    loaded = json.loads(raw)
    sample = loaded["comparisons"][0]["matches"][0]
    long_fields = (
        "title",
        "entity",
        "industry",
        "summary",
        "evidence",
        "qa_concern",
        "management_response",
        "tam_mapping",
        "investment_implication",
    )
    assert len(loaded["comparisons"]) <= signals._MAX_RECORDS
    assert len(loaded["comparisons"][0]["matches"]) == signals.COMPARE_LIMIT
    assert len(loaded["comparisons"][0]["current"]["summary"]) < signals._MAX_STRING
    for key in long_fields:
        assert len(sample[key]) == signals._MAX_STRING
        assert '"' in sample[key]
    assert "\\\"" in full

    root = tmp_path / "state"

    def command(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Any,
        input_bytes: bytes | None,
    ) -> ChildResult:
        del env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn, input_bytes
        if argv[4] != "compare":
            return _child(returncode=1)
        Path(argv[argv.index("--output") + 1]).write_bytes(raw)
        return _child()

    _prepare_open_bundle(root, command)
    generation = _generation(root, DATE)
    assert (generation / "compare_result.json").read_bytes() == raw
    assert len((generation / "signal_records.json").read_bytes()) <= signals._OBJECT_MAX_BYTES
    assert _commit(root, DATE)["status"] == "open"

    offset = old_cap + 1
    page = _run(
        root,
        signals.parse_signal_request(_body("read", offset=offset, max_chars=signals.MAX_CHARS)),
        python=Path(sys.executable),
    )
    assert page["ok"] is True
    assert page["offset"] == offset
    assert page["max_chars"] == signals.MAX_CHARS
    assert 0 < len(page["text"]) <= signals.MAX_CHARS
    assert len(page["text"]) < page["total_chars"]
    assert page["text"] == full[offset:offset + len(page["text"])]
    assert page["text"] != full
    assert "\\" in page["text"]
    assert page["complete"] is False
    assert page["next_offset"] == offset + len(page["text"])
    assert old_cap < page["next_offset"] <= bound
    assert page["total_chars"] == len(full)
    assert page["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(json.dumps(page, ensure_ascii=False).encode()) <= signals.MAX_RESPONSE_BYTES
    accepted = signal_tools._accept_success(
        page,
        {"action": "read", "batch_id": DATE, "offset": offset, "max_chars": signals.MAX_CHARS},
    )
    assert accepted is not None
    assert accepted["next_offset"] == page["next_offset"]
    assert len(accepted["text"]) <= signals.MAX_CHARS

    follow = _run(
        root,
        signals.parse_signal_request(_body("read", offset=page["next_offset"], max_chars=signals.MAX_CHARS)),
        python=Path(sys.executable),
    )
    assert follow["ok"] is True
    assert follow["offset"] == page["next_offset"]
    assert 0 < len(follow["text"]) <= signals.MAX_CHARS
    assert follow["text"] == full[follow["offset"]:follow["offset"] + len(follow["text"])]
    assert follow["content_sha256"] == page["content_sha256"]
    assert len(json.dumps(follow, ensure_ascii=False).encode()) <= signals.MAX_RESPONSE_BYTES
    parsed_follow, refused_follow = signal_tools._parse_call(
        "read", DATE, None, None, None, page["next_offset"], signals.MAX_CHARS,
    )
    assert refused_follow is None
    assert parsed_follow is not None and parsed_follow["offset"] == page["next_offset"]

    at_bound = signals.parse_signal_request(_body("read", offset=bound, max_chars=signals.MAX_CHARS))
    assert at_bound["ok"] is True
    assert at_bound["offset"] == bound
    past_file = _run(root, at_bound, python=Path(sys.executable))
    assert past_file["code"] == "offset_past_end"
    assert "text" not in past_file
    assert past_file["total_chars"] == len(full)
    edge_parsed = {"action": "read", "batch_id": DATE, "offset": bound - 1, "max_chars": signals.MAX_CHARS}
    edge = signal_tools._accept_success(
        {
            "ok": True,
            "action": "read",
            "code": "ok",
            "batch_id": DATE,
            "profile": signal_tools.PROFILE,
            "bundle_hash": page["bundle_hash"],
            "offset": bound - 1,
            "max_chars": signals.MAX_CHARS,
            "total_chars": bound,
            "content_sha256": page["content_sha256"],
            "text": "x",
            "next_offset": bound,
            "complete": False,
        },
        edge_parsed,
    )
    assert edge is not None
    assert edge["next_offset"] == bound

    above = signals.parse_signal_request(_body("read", offset=bound + 1, max_chars=signals.MAX_CHARS))
    assert above["code"] == "invalid_offset"
    assert "text" not in above
    above_parsed, above_refused = signal_tools._parse_call(
        "read", DATE, None, None, None, bound + 1, signals.MAX_CHARS,
    )
    assert above_parsed is None
    assert above_refused is not None
    assert json.loads(above_refused)["code"] == "invalid_offset"
    assert signal_tools._accept_success(
        {
            "ok": True,
            "action": "read",
            "code": "ok",
            "batch_id": DATE,
            "profile": signal_tools.PROFILE,
            "bundle_hash": page["bundle_hash"],
            "offset": bound - 1,
            "max_chars": signals.MAX_CHARS,
            "total_chars": bound + 1,
            "content_sha256": page["content_sha256"],
            "text": "xy",
            "next_offset": bound + 1,
            "complete": False,
        },
        edge_parsed,
    ) is None
    assert (generation / "compare_result.json").read_bytes() == raw
    assert _commit(root, DATE)["status"] == "open"
    assert _commit(root, DATE)["compare_sha256"] == hashlib.sha256(raw).hexdigest()
