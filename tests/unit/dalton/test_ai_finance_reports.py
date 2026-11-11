"""Saved AI-finance daily, original card, and post-send seen state.

These checks stay on local files and fakes. They do not call a database,
an embedding API, or a delivery service.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import subprocess
import sys
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
from scripts.dalton import ai_finance_reports as reports  # noqa: E402
from scripts.dalton import ai_finance_signals as signals  # noqa: E402
from src.dalton.access_tiers import is_internal_or_admin  # noqa: E402
from src.dalton.database import ai_finance as ai_finance_db  # noqa: E402
from src.dalton.database.automation_reports import (  # noqa: E402
    automation_report_content_sha256,
    lock_report_execution,
)
from src.dalton.settings import (  # noqa: E402
    get_ai_finance_owner_user_id,
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
)
from src.dalton.tools import ai_finance_report_tools as report_tools  # noqa: E402
from src.dalton.tools import build_dalton_tools  # noqa: E402

HKT = ZoneInfo("Asia/Hong_Kong")
OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "99999999-9999-4999-8999-999999999999"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
THREAD = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
AUTOMATION = "55555555-5555-4555-8555-555555555555"
EXECUTION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DATE = "2099-01-15"
THROUGH = "2099-01-14"
NEWS_KEY = "ab" * 32
ANCIENT_KEY = "cd" * 32
BRIDGE_TOKEN = "bridge-mcp-token-secret"
ADMIN_TOKEN = "bridge-admin-token-secret"
MARKER = "CRON_RECOVERED_OK"
MARKDOWN = "\n".join([
    "## 📊 AI + 金融投资日报",
    "",
    "### 今日要闻",
    "1. **标题** — 摘要。（[来源](https://example.com/a)）",
    "",
    "### GitHub AI 项目动量",
    "1. **[acme/alpha](https://github.com/acme/alpha)**（总 stars 2,590；日增 +1,053）",
    "项目直接对应开发者工具的采用。",
    "相关公开说明见仓库 README。",
    "",
    "### Earnings Calls：AI 的直接业务影响",
    "本轮没有达到入库标准的电话会。",
    "#### 跨日共性与新趋势",
    "与上一交易日相比没有新的跨日主题。",
    "",
    "### 来源说明",
    "新闻来自公开链接，GitHub 数字来自官方完成日桶。",
    "",
])
BUNDLE_KEYS = {
    "ok",
    "batch_id",
    "profile",
    "owner_user_id",
    "manifest_sha256",
    "bundle_hash",
    "generation",
    "compare_sha256",
    "outcome_sha256",
    "github_candidates",
    "earnings_scan",
    "github_decisions",
    "earnings_dispositions",
    "signal_records",
    "compare",
    "ingest",
    "stats",
}


@pytest.fixture(autouse=True)
def _clear_report_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DALTON_AI_FINANCE_FIRST_REPORT_DATE", raising=False)


def _canon(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _started() -> datetime:
    return datetime(2099, 1, 15, 20, 5, tzinfo=HKT)


def _broadcast_at() -> float:
    return _started().timestamp()


def _candidate() -> dict[str, Any]:
    return {
        "full_name": "acme/alpha",
        "history_verified": True,
        "report_eligible": True,
        "github": {"stars": 2590},
        "significance": {"breakout_score": 1.5},
        "momentum": {
            "daily_gain": 1053,
            "phase": "breakout",
            "complete_through": THROUGH,
            "daily_velocity_3d": 100.0,
            "gain_7d": 2000,
        },
    }


def _decisions() -> dict[str, Any]:
    return {
        "records": [{
            "repo": "acme/alpha",
            "decision": "include",
            "reason": "eligible and material",
            "readme_url": "https://github.com/acme/alpha",
            "displayed": True,
        }]
    }


def _write_prepared(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    batch = root / "batches" / DATE
    batch.mkdir(parents=True)
    github_doc = {"version": 3, "complete_through": THROUGH, "continuing_hotspots": [], "candidates": [_candidate()]}
    scan = {"coverage": {"ai_candidates": 0}, "candidates": []}
    github_bytes = _canon(github_doc)
    earnings_bytes = _canon(scan)
    (batch / "github_candidates.json").write_bytes(github_bytes)
    (batch / "earnings_scan.json").write_bytes(earnings_bytes)
    started = _started()
    manifest = {
        "schema": 1,
        "batch_id": DATE,
        "started_at": started.isoformat(),
        "window": {"timezone": "Asia/Hong_Kong", "start": started.isoformat(), "end": started.isoformat(), "hours": 24},
        "artifacts": {
            "github_candidates": {"path": "github_candidates.json", "sha256": hashlib.sha256(github_bytes).hexdigest()},
            "earnings_scan": {"path": "earnings_scan.json", "sha256": hashlib.sha256(earnings_bytes).hexdigest()},
        },
        "candidates": [],
    }
    manifest_bytes = _canon(manifest)
    (batch / "manifest.json").write_bytes(manifest_bytes)
    metadata = {
        "schema": 1,
        "batch_id": DATE,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "stages": {
            "github": {"status": "complete", "artifact": "github_candidates.json", "sha256": hashlib.sha256(github_bytes).hexdigest()},
            "earnings": {"status": "complete", "artifact": "earnings_scan.json", "sha256": hashlib.sha256(earnings_bytes).hexdigest()},
        },
    }
    (batch / "metadata.json").write_bytes(_canon(metadata))
    clock = {
        "schema": 1,
        "batch_id": DATE,
        "started_at": started.isoformat(),
        "scheduled_at": started.replace(minute=0).isoformat(),
        "first_execution_id": EXECUTION,
        "owner_user_id": OWNER,
        "profile": "ai-finance-daily",
    }
    clocks = root / "batch_clocks"
    clocks.mkdir()
    (clocks / f"{DATE}.json").write_text(json.dumps(clock), encoding="utf-8")


def _save_bundle(root: Path) -> dict[str, Any]:
    parsed = signals.parse_signal_request({
        "action": "save",
        "batch_id": DATE,
        "automation_execution_id": EXECUTION,
        "owner_user_id": OWNER,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "thread_id": THREAD,
        "profile": "ai-finance-daily",
        "github_decisions": _decisions(),
        "earnings_dispositions": {"records": []},
        "signal_records": {"records": []},
    })
    saved = signals.execute_signal(
        parsed,
        state_root=root,
        python_executable=None,
        embedding_model="gemini-embedding-2",
        embedding_dim=768,
        child_env=None,
    )
    assert saved["ok"] is True, saved
    return saved


def _freeze(root: Path) -> None:
    path = root / "signals" / DATE / "commit.json"
    commit = json.loads(path.read_text(encoding="utf-8"))
    directory = root / "signals" / DATE / "g" / commit["generation"]
    compare = {
        "search_modes": ["empty_history"],
        "history_embedding_status_before_ingest": "empty",
        "history_signals_before_ingest": 0,
        "history_embedded_signals_before_ingest": 0,
        "comparisons": [],
    }
    outcome = {
        "schema": 1,
        "bundle_hash": commit["bundle_hash"],
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
        "ingest": {"inserted": 0, "updated": 0, "embedding_errors": []},
        "stats": {
            "signals": 0,
            "embedded_signals": 0,
            "embedded_signals_any_model": 0,
            "embedding_status": "empty",
            "embedding_coverage_pct": 0,
            "configured_embedding_model": "gemini-embedding-2",
            "configured_embedding_dim": 768,
            "signals_by_source": {},
            "signals_by_direction": {},
        },
    }
    compare_raw = _canon(compare)
    outcome_raw = _canon(outcome)
    (directory / "compare_result.json").write_bytes(compare_raw)
    (directory / "outcome.json").write_bytes(outcome_raw)
    commit["status"] = "ingested"
    commit["compare_sha256"] = hashlib.sha256(compare_raw).hexdigest()
    commit["outcome_sha256"] = hashlib.sha256(outcome_raw).hexdigest()
    path.write_bytes(_canon(commit))


def _ingested(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    _write_prepared(root)
    _save_bundle(root)
    _freeze(root)
    (root / "github_trending_history.json").write_bytes(b"pool-sentinel")
    return root


def _news(stamp: float = 1) -> dict[str, float]:
    return {NEWS_KEY: stamp}


def _save_daily(root: Path, markdown: str = MARKDOWN, news: dict[str, float] | None = None) -> dict[str, Any]:
    return reports.save_report(
        state_root=root,
        batch_id=DATE,
        owner_user_id=OWNER,
        markdown=markdown,
        news_events=_news() if news is None else news,
    )


def _commit(root: Path, saved: dict[str, Any]) -> dict[str, Any]:
    return reports.commit_report_seen(
        state_root=root,
        batch_id=DATE,
        owner_user_id=OWNER,
        body_sha256=saved["body_sha256"],
        content_sha256=saved["content_sha256"],
    )


def _signal_bytes(root: Path) -> dict[str, bytes]:
    commit = json.loads((root / "signals" / DATE / "commit.json").read_text(encoding="utf-8"))
    directory = root / "signals" / DATE / "g" / commit["generation"]
    names = (
        "commit.json",
        "github_decisions.json",
        "earnings_dispositions.json",
        "signal_records.json",
        "compare_result.json",
        "outcome.json",
    )
    found = {}
    for name in names:
        path = (root / "signals" / DATE / name) if name == "commit.json" else directory / name
        found[name] = path.read_bytes()
    return found


def test_signatures() -> None:
    for function in (signals.read_report_bundle, reports.save_report, reports.get_report, reports.commit_report_seen):
        for parameter in inspect.signature(function).parameters.values():
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    tool = report_tools.create_ai_finance_report_tools()[0]
    signature = inspect.signature(tool.coroutine)
    assert list(signature.parameters) == ["action", "batch_id", "markdown", "news_events", "config"]
    assert signature.parameters["config"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["markdown"].default is None
    assert signature.parameters["news_events"].default is None


def test_read_report_bundle_returns_the_ingested_objects_without_paths(tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    commit_path = root / "signals" / DATE / "commit.json"
    before = commit_path.read_bytes()
    bundle = signals.read_report_bundle(state_root=root, batch_id=DATE, owner_user_id=OWNER)
    assert bundle["ok"] is True
    assert BUNDLE_KEYS <= set(bundle)
    assert bundle["github_candidates"]["version"] == 3
    assert bundle["generation"]
    assert "started_at" in bundle
    assert str(root) not in json.dumps(bundle)
    assert "path" not in bundle
    assert commit_path.read_bytes() == before

    open_commit = json.loads(before.decode("utf-8"))
    open_commit["status"] = "open"
    commit_path.write_bytes(_canon(open_commit))
    not_ready = signals.read_report_bundle(state_root=root, batch_id=DATE, owner_user_id=OWNER)
    assert not_ready["ok"] is False
    assert not_ready["code"] == "not_ready"

    commit_path.write_bytes(before)
    compare = root / "signals" / DATE / "g" / bundle["generation"] / "compare_result.json"
    compare.write_bytes(compare.read_bytes() + b" ")
    corrupt = signals.read_report_bundle(state_root=root, batch_id=DATE, owner_user_id=OWNER)
    assert corrupt["ok"] is False
    assert corrupt["code"] == "commit_inconsistent"


def test_save_validates_the_daily_and_reuses_the_same_bytes(tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    before = _signal_bytes(root)
    pool = (root / "github_trending_history.json").read_bytes()
    saved = _save_daily(root)
    assert saved["ok"] is True, saved
    assert saved["reused"] is False
    assert saved["action"] == "save"
    assert saved["report_run_id"] == f"ai-finance:{DATE}"
    assert saved["news_count"] == 1
    assert saved["github_count"] == 1
    assert saved["card"]["schema"] == "2.0"
    assert saved["card"]["header"]["title"]["content"] == f"📊 AI + 金融投资日报 — {DATE}"
    assert "跨日共性与新趋势" in json.dumps(saved["card"], ensure_ascii=False)
    official = automation_report_content_sha256(
        profile="ai-finance-daily",
        content_kind="cards",
        cards=[saved["card"]],
        failure_text=None,
    )
    assert saved["content_sha256"] == official
    assert _signal_bytes(root) == before
    assert (root / "github_trending_history.json").read_bytes() == pool
    assert not (root / "seen.json").exists()
    assert not (root / "github_seen.json").exists()

    again = _save_daily(root, news={NEWS_KEY: 99})
    assert again["ok"] is True
    assert again["reused"] is True
    assert again["body_sha256"] == saved["body_sha256"]
    changed = MARKDOWN.replace("官方完成日桶。", "官方完成日桶改。")
    conflict = _save_daily(root, markdown=changed)
    assert conflict["code"] == "content_conflict"
    assert (root / "reports" / DATE / "body.md").read_text(encoding="utf-8") == MARKDOWN
    assert _signal_bytes(root) == before

    bad = _save_daily(root, markdown=MARKDOWN.replace("+1,053", "+1"))
    assert bad["code"] == "report_invalid"
    assert bad["violation_count"] >= 1
    dumped = json.dumps(bad)
    assert str(root) not in dumped
    assert "card" not in bad


def test_get_pages_the_saved_body(tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    saved = _save_daily(root)
    page = reports.get_report(
        state_root=root,
        batch_id=DATE,
        owner_user_id=OWNER,
        offset=0,
        max_chars=20,
    )
    assert page["ok"] is True
    assert page["text"] == MARKDOWN[:20]
    assert page["next_offset"] == 20
    assert page["complete"] is False
    assert page["card"]["header"]["title"]["content"] == saved["card"]["header"]["title"]["content"]
    assert "state_root" not in page


def test_commit_seen_freezes_the_broadcast_and_prunes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 10**12)
    root = _ingested(tmp_path)
    broadcast_at = _broadcast_at()
    recent = broadcast_at - 2 * 86400
    (root / "seen.json").write_bytes(_canon({
        NEWS_KEY: recent,
        ANCIENT_KEY: broadcast_at - 8 * 86400,
        "note": "keep-me",
    }))
    (root / "github_seen.json").write_bytes(_canon({
        "schema": 1,
        "repos": {
            "Acme/Alpha": {
                "last_ts": 10,
                "last_daily_gain": 1,
                "last_bucket": "quiet",
                "last_complete_through": "2099-01-01",
            },
            "stale/repo": {
                "last_ts": broadcast_at - 40 * 86400,
                "last_daily_gain": 1,
                "last_bucket": "quiet",
                "last_complete_through": "2098-01-01",
            },
        },
    }))
    saved = _save_daily(root)
    committed = _commit(root, saved)
    assert committed["ok"] is True, committed
    assert committed["seen_status"] == "complete"
    news = json.loads((root / "seen.json").read_text(encoding="utf-8"))
    assert news[NEWS_KEY] == recent
    assert ANCIENT_KEY not in news
    assert news["note"] == "keep-me"
    github = json.loads((root / "github_seen.json").read_text(encoding="utf-8"))
    assert github["schema"] == 1
    assert set(github["repos"]) == {"acme/alpha"}
    entry = github["repos"]["acme/alpha"]
    assert entry["last_ts"] == broadcast_at
    assert entry["last_ts"] != 10**12
    assert entry["last_daily_gain"] == 1053
    assert entry["last_bucket"] == "breakout"
    assert entry["last_complete_through"] == THROUGH
    assert (root / "github_trending_history.json").read_bytes() == b"pool-sentinel"


def test_newer_repo_state_survives_an_older_broadcast_and_a_repeat(tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    saved = _save_daily(root)
    original = {
        "last_ts": 42.5,
        "last_daily_gain": 9,
        "last_bucket": "quiet",
        "last_complete_through": "2099-06-01",
    }
    (root / "github_seen.json").write_bytes(_canon({"repos": {"acme/alpha": original}}))
    first = _commit(root, saved)
    assert first["ok"] is True, first
    assert json.loads((root / "github_seen.json").read_text(encoding="utf-8"))["repos"]["acme/alpha"] == original
    second = _commit(root, saved)
    assert second["reused"] is True
    assert json.loads((root / "github_seen.json").read_text(encoding="utf-8"))["repos"]["acme/alpha"] == original


def test_seen_commit_resumes_after_each_interrupted_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _ingested(tmp_path)
    saved = _save_daily(root)
    broadcast_at = _broadcast_at()
    (root / "github_seen.json").write_bytes(_canon({
        "repos": {
            "kept/repo": {
                "last_ts": broadcast_at,
                "last_daily_gain": 1,
                "last_bucket": "quiet",
                "last_complete_through": "2099-01-01",
            }
        }
    }))
    real = reports._atomic_write
    fail = {"name": "github_seen.json"}

    def wrapped(path: Path, data: bytes) -> None:
        if path.name == fail["name"]:
            raise OSError("disk")
        real(path, data)

    monkeypatch.setattr(reports, "_atomic_write", wrapped)
    first = _commit(root, saved)
    assert first["code"] == "seen_incomplete"
    assert NEWS_KEY in json.loads((root / "seen.json").read_text(encoding="utf-8"))
    assert "acme/alpha" not in json.loads((root / "github_seen.json").read_text(encoding="utf-8"))["repos"]
    assert not (root / "reports" / DATE / "seen_commit.json").exists()

    fail["name"] = "seen_commit.json"
    second = _commit(root, saved)
    assert second["code"] == "seen_incomplete"
    github = json.loads((root / "github_seen.json").read_text(encoding="utf-8"))
    assert github["repos"]["acme/alpha"]["last_ts"] == broadcast_at
    assert github["repos"]["kept/repo"]["last_complete_through"] == "2099-01-01"
    assert not (root / "reports" / DATE / "seen_commit.json").exists()

    fail["name"] = ""
    third = _commit(root, saved)
    assert third["ok"] is True
    assert third["reused"] is False
    snapshot = (root / "github_seen.json").read_bytes()
    fourth = _commit(root, saved)
    assert fourth["reused"] is True
    assert (root / "github_seen.json").read_bytes() == snapshot


def test_corrupt_seen_and_symlink_are_not_replaced(tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    saved = _save_daily(root)
    (root / "github_seen.json").write_text("{}\n", encoding="utf-8")
    rejected = _commit(root, saved)
    assert rejected["code"] == "commit_inconsistent"
    assert (root / "github_seen.json").read_text(encoding="utf-8") == "{}\n"
    (root / "github_seen.json").unlink()
    target = root / "news-target.json"
    target.write_text("{}\n", encoding="utf-8")
    (root / "seen.json").symlink_to(target)
    blocked = _commit(root, saved)
    assert blocked["code"] == "path_escape"
    assert (root / "seen.json").is_symlink()
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_closed_date_does_not_save_or_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _ingested(tmp_path)
    monkeypatch.setenv("DALTON_AI_FINANCE_FIRST_REPORT_DATE", "2099-02-01")
    closed = _save_daily(root)
    assert closed["code"] == "report_date_closed"
    assert not (root / "reports").exists()
    monkeypatch.delenv("DALTON_AI_FINANCE_FIRST_REPORT_DATE")
    saved = _save_daily(root)
    monkeypatch.setenv("DALTON_AI_FINANCE_FIRST_REPORT_DATE", "2099-02-01")
    blocked = _commit(root, saved)
    assert blocked["code"] == "report_date_closed"
    assert not (root / "seen.json").exists()
    monkeypatch.setenv("DALTON_AI_FINANCE_FIRST_REPORT_DATE", "tomorrow")
    invalid = _save_daily(root)
    assert invalid["code"] == "config_invalid"


def test_product_sources_do_not_name_the_closed_calendar_day() -> None:
    for relative in (
        "scripts/dalton/ai_finance_reports.py",
        "scripts/dalton/ai_finance_signals.py",
        "src/dalton/tools/ai_finance_report_tools.py",
    ):
        assert "2026-11-11" not in (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_renderer_imports_from_a_neutral_cwd(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "root, scripts, markdown = sys.argv[1:]\n"
        "sys.path = [item for item in sys.path if item not in {root, ''}]\n"
        "sys.path.insert(0, scripts)\n"
        "import ai_finance_reports\n"
        "card = ai_finance_reports.render_daily_card(markdown, '2099-01-15')\n"
        "title = card['header']['title']['content']\n"
        "assert title.endswith('2099-01-15'), title\n"
        "assert card['schema'] == '2.0'\n"
        "assert not any(name.startswith(('src.', 'ptc_agent', 'structlog')) for name in sys.modules)\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(REPO_ROOT), str(SCRIPTS_DALTON), MARKDOWN],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
        "metadata": {"report_profile": {"profile": "ai-finance-daily", "report_targets": ["feishu:chat:oc_test"], "failure_targets": []}},
    }


class _Delivery:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[Any] = []

    async def coroutine(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)


def _install(monkeypatch: pytest.MonkeyPatch, root: Path) -> SimpleNamespace:
    clock = {"scheduled_at": _started().replace(minute=0), "started_at": _started()}
    pool = _Pool(clock)
    http_calls: list[dict[str, Any]] = []
    seen: list[dict[str, Any]] = []
    owner_value: list[str | None] = [OWNER]
    internal_open = [True]
    delivery = _Delivery()
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
        http_calls.append({"timeout": kwargs.get("timeout"), "trust_env": kwargs.get("trust_env"), "held": pool.held})

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            assert request.url.path == "/internal/ai-finance/report"
            assert "state_root" not in body
            parsed = reports.parse_report_request(body)
            result = reports.execute_report(parsed, state_root=root)
            status = 200 if result.get("ok") is True else 409
            if isinstance(result.get("code"), str) and (result["code"] == "unknown_field" or result["code"].startswith("invalid")):
                status = 400
            return httpx.Response(status, json=result)

        kwargs["transport"] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(ai_finance_db, "get_db_connection", pool)
    monkeypatch.setattr(ai_finance_db, "lock_report_execution", create_autospec(lock_report_execution, side_effect=_lock))
    monkeypatch.setattr(report_tools, "authorize_ai_finance_execution", create_autospec(real_auth, side_effect=_auth))
    monkeypatch.setattr(report_tools, "is_internal_or_admin", create_autospec(is_internal_or_admin, side_effect=_internal))
    monkeypatch.setattr(report_tools, "get_ai_finance_owner_user_id", create_autospec(get_ai_finance_owner_user_id, side_effect=lambda: owner_value[0]))
    monkeypatch.setattr(report_tools, "get_dalton_bridge_admin_token", create_autospec(get_dalton_bridge_admin_token, return_value=ADMIN_TOKEN))
    monkeypatch.setattr(report_tools, "get_dalton_bridge_base_url", create_autospec(get_dalton_bridge_base_url, return_value="http://bridge.invalid"))
    monkeypatch.setattr(report_tools.httpx, "AsyncClient", factory)
    monkeypatch.setattr(report_tools, "create_automation_report_tools", lambda: [delivery])
    return SimpleNamespace(
        pool=pool,
        http_calls=http_calls,
        seen=seen,
        owner_value=owner_value,
        internal_open=internal_open,
        delivery=delivery,
        tool=report_tools.create_ai_finance_report_tools()[0],
    )


def _pending() -> str:
    return json.dumps({"ok": True, "found": True, "status": "pending", "delivery_confirmed": False, "profile": "ai-finance-daily"})


def _sent(root: Path) -> str:
    saved = json.loads((root / "reports" / DATE / "report.json").read_text(encoding="utf-8"))
    return json.dumps({
        "ok": True,
        "found": True,
        "profile": "ai-finance-daily",
        "content_kind": "cards",
        "report_run_id": f"ai-finance:{DATE}",
        "content_sha256": saved["content_sha256"],
        "status": "sent",
        "delivery_confirmed": True,
        "execution_id": OTHER,
    })


@pytest.mark.asyncio
async def test_tool_gate_releases_the_database_before_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    installed = _install(monkeypatch, root)
    missing = json.loads(await installed.tool.ainvoke({"action": "status", "batch_id": DATE}, config={"configurable": {}}))
    assert missing["code"] == "not_automation_run"
    installed.owner_value[0] = OTHER
    wrong = json.loads(await installed.tool.ainvoke({"action": "submit", "batch_id": DATE, "markdown": MARKDOWN, "news_events": _news()}, config=_config()))
    assert wrong["code"] == "not_allowed"
    installed.owner_value[0] = OWNER
    installed.internal_open[0] = False
    external = json.loads(await installed.tool.ainvoke({"action": "finalize", "batch_id": DATE}, config=_config()))
    assert external["code"] == "not_allowed"
    installed.internal_open[0] = True
    early = json.loads(await installed.tool.ainvoke({"action": "finalize", "batch_id": DATE, "markdown": "x"}, config=_config()))
    assert early["code"] == "invalid_request"
    assert installed.http_calls == []
    monkeypatch.setenv("DALTON_AI_FINANCE_FIRST_REPORT_DATE", "2099-02-01")
    closed = json.loads(await installed.tool.ainvoke({"action": "submit", "batch_id": DATE, "markdown": MARKDOWN, "news_events": _news()}, config=_config()))
    assert closed["code"] == "report_date_closed"
    assert installed.http_calls == []
    assert not (root / "reports").exists()


@pytest.mark.asyncio
async def test_submit_keeps_the_report_until_exact_delivery_and_finalize(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    installed = _install(monkeypatch, root)
    installed.delivery.queue.extend([_pending(), _pending(), _pending()])
    pending = json.loads(await installed.tool.ainvoke(
        {"action": "submit", "batch_id": DATE, "markdown": MARKDOWN, "news_events": _news()},
        config=_config(),
    ))
    assert pending["code"] == "report_unconfirmed"
    assert pending["delivery_confirmed"] is False
    assert pending["status"] == "pending"
    assert "marker" not in pending
    assert "card" not in pending
    assert installed.pool.held == 0
    assert installed.http_calls[0]["held"] == 0
    assert installed.http_calls[0]["trust_env"] is False
    assert installed.http_calls[0]["timeout"] == 30.0
    assert installed.http_calls[1]["timeout"] == 60.0
    submit_call = installed.delivery.calls[1]
    assert submit_call["action"] == "submit"
    assert submit_call["material"] == "yes"
    assert submit_call["report_run_id"] == f"ai-finance:{DATE}"
    assert submit_call["cards"] == [json.loads((root / "reports" / DATE / "card.json").read_text(encoding="utf-8"))]
    assert not (root / "seen.json").exists()
    body = (root / "reports" / DATE / "body.md").read_bytes()

    installed.delivery.queue.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await installed.tool.ainvoke({"action": "status", "batch_id": DATE}, config=_config())
    assert (root / "reports" / DATE / "body.md").read_bytes() == body
    assert not (root / "seen.json").exists()

    installed.delivery.queue.extend([
        json.dumps({"ok": True, "status": "unknown", "delivery_confirmed": False}),
        _pending(),
        _pending(),
    ])
    unknown = json.loads(await installed.tool.ainvoke({"action": "submit", "batch_id": DATE}, config=_config()))
    assert unknown["delivery_confirmed"] is False
    assert (root / "reports" / DATE / "body.md").read_bytes() == body

    hold = json.dumps({
        "ok": True,
        "profile": "other",
        "content_kind": "cards",
        "report_run_id": f"ai-finance:{DATE}",
        "content_sha256": "c" * 64,
        "status": "sent",
        "delivery_confirmed": True,
    })
    calls_before = len(installed.delivery.calls)
    installed.delivery.queue.append(hold)
    unconfirmed = json.loads(await installed.tool.ainvoke({"action": "finalize", "batch_id": DATE}, config=_config()))
    assert unconfirmed["code"] == "report_unconfirmed"
    assert len(installed.delivery.calls) == calls_before + 1
    assert not (root / "github_seen.json").exists()

    installed.delivery.queue.extend([_sent(root), _sent(root), _sent(root)])
    sent = json.loads(await installed.tool.ainvoke({"action": "submit", "batch_id": DATE}, config=_config()))
    assert sent["code"] == "seen_pending"
    assert sent["delivery_confirmed"] is True
    assert "marker" not in sent
    assert not (root / "seen.json").exists()
    final = json.loads(await installed.tool.ainvoke({"action": "finalize", "batch_id": DATE}, config=_config()))
    assert final["marker"] == MARKER
    assert final["delivery_confirmed"] is True
    assert final["seen_status"] == "complete"
    assert final["status"] == "sent"
    seen = json.loads((root / "seen.json").read_text(encoding="utf-8"))
    github = json.loads((root / "github_seen.json").read_text(encoding="utf-8"))
    assert seen[NEWS_KEY] == _broadcast_at()
    assert github["repos"]["acme/alpha"]["last_ts"] == _broadcast_at()
    repeat = json.loads(await installed.tool.ainvoke({"action": "finalize", "batch_id": DATE}, config=_config()))
    assert repeat["marker"] == MARKER
    assert repeat["reused"] is True
    assert json.loads((root / "github_seen.json").read_text(encoding="utf-8")) == github


@pytest.mark.asyncio
async def test_hash_mismatch_does_not_submit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    installed = _install(monkeypatch, root)
    monkeypatch.setattr(report_tools, "automation_report_content_sha256", lambda **kwargs: "ab" * 32)
    result = json.loads(await installed.tool.ainvoke(
        {"action": "submit", "batch_id": DATE, "markdown": MARKDOWN, "news_events": _news()},
        config=_config(),
    ))
    assert result["code"] == "state_corrupt"
    assert installed.delivery.calls == []
    assert (root / "reports" / DATE / "body.md").read_text(encoding="utf-8") == MARKDOWN
    assert not (root / "seen.json").exists()


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
@pytest.mark.parametrize("tier", ["internal", "admin", None])
def test_report_tool_mounts_for_internal_admin_and_unresolved_tier(role: str, tier: str | None) -> None:
    names = {item.name for item in build_dalton_tools(role, user_tier=tier)}
    assert "ai_finance_report" in names


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
def test_external_tier_does_not_mount_the_report_tool(role: str) -> None:
    names = {item.name for item in build_dalton_tools(role, user_tier="external")}
    assert "ai_finance_report" not in names


def _bridge(monkeypatch: pytest.MonkeyPatch, home: Path, config: Path) -> rb.ResearchDataBridge:
    env_file = home / "bridge.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("DALTON_TEST=1\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(home / "usage.json"))
    monkeypatch.setenv("DALTON_AI_FINANCE_OWNER_USER_ID", OWNER)
    monkeypatch.setenv("DALTON_AI_FINANCE_CONFIG", str(config))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _report_body(action: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "action": action,
        "batch_id": DATE,
        "automation_execution_id": EXECUTION,
        "owner_user_id": OWNER,
        "user_id": OWNER,
        "workspace_id": WORKSPACE,
        "thread_id": THREAD,
        "profile": "ai-finance-daily",
    }
    payload.update(extra)
    return payload


async def _drive(bridge: rb.ResearchDataBridge, *, body: bytes, token: str | None, method: str = "POST") -> tuple[int, dict[str, Any]]:
    header_lines = ["Host: 127.0.0.1", "Content-Type: application/json"]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    raw = (
        f"{method} /internal/ai-finance/report HTTP/1.1\r\n"
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
async def test_admin_report_route_saves_and_hides_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"state_root": str(root)}), encoding="utf-8")
    bridge = _bridge(monkeypatch, tmp_path / "home", config)
    denied, denied_body = await _drive(bridge, body=b"{}", token=None)
    assert denied == 401
    forbidden, forbidden_body = await _drive(bridge, body=b"{}", token=BRIDGE_TOKEN)
    assert forbidden == 403
    assert denied_body["ok"] is False
    assert forbidden_body["ok"] is False
    method, method_body = await _drive(bridge, body=b"{}", token=ADMIN_TOKEN, method="GET")
    assert method == 405
    assert method_body == {"ok": False, "error": "Method Not Allowed"}
    extra = _report_body("save", markdown=MARKDOWN, news_events=_news(), shell="rm")
    rejected, rejected_body = await _drive(bridge, body=json.dumps(extra).encode(), token=ADMIN_TOKEN)
    assert rejected == 400
    assert rejected_body["code"] == "unknown_field"
    assert str(root) not in json.dumps(rejected_body)
    status, payload = await _drive(
        bridge,
        body=json.dumps(_report_body("save", markdown=MARKDOWN, news_events=_news())).encode(),
        token=ADMIN_TOKEN,
    )
    assert status == 200, payload
    assert payload["card"]["schema"] == "2.0"
    assert payload["owner_user_id"] == OWNER
    assert str(root) not in json.dumps(payload)
    page_status, page = await _drive(
        bridge,
        body=json.dumps(_report_body("get", offset=0, max_chars=12)).encode(),
        token=ADMIN_TOKEN,
    )
    assert page_status == 200
    assert page["text"] == MARKDOWN[:12]
    assert "state_root" not in page
    commit_status, committed = await _drive(
        bridge,
        body=json.dumps(_report_body(
            "commit_seen",
            body_sha256=payload["body_sha256"],
            content_sha256=payload["content_sha256"],
        )).encode(),
        token=ADMIN_TOKEN,
    )
    assert commit_status == 200, committed
    assert committed["seen_status"] == "complete"
    assert "card" not in committed
    assert str(root) not in json.dumps(committed)


@pytest.mark.asyncio
async def test_initial_submit_returns_validation_details_without_sending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _ingested(tmp_path)
    installed = _install(monkeypatch, root)
    result = json.loads(await installed.tool.ainvoke(
        {"action": "submit", "batch_id": DATE, "markdown": "# Incomplete report", "news_events": _news()},
        config=_config(),
    ))
    assert result["code"] == "report_invalid"
    assert result["ok"] is False
    assert result["violations"]
    assert result["violation_count"] >= len(result["violations"])
    assert all(len(item) <= 180 for item in result["violations"])
    assert installed.delivery.calls == []
    assert not (root / "reports" / DATE / "report.json").exists()
    assert not (root / "seen.json").exists()
