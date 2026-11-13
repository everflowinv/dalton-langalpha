"""OC-1 AI application weekly monitor: offline CLI, records, and path checks."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

from ptc_agent.agent.middleware.skills.dalton_registry import (
    _DALTON_SKILL_SPECS,
    read_skill_description,
)
from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-ai-app-weekly-monitor"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "ai_application_weekly_monitor_cli.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai_app"
PROFILE = "ai-app-weekly"
DAY = "2026-11-13"
AS_OF = "2026-11-13T15:00:00+08:00"
DOC_ID = "DoxExampleDocId20261113"
BANNED_IDS = ("1134818575211839498", "932169512197955636")
_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_OU_RE = re.compile(r"(?<![A-Za-z])ou_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
_FUND = "state/ai_app_db/companies/NET/fundamentals.jsonl"
_METRIC = "state/ai_app_db/companies/NET/metrics.jsonl"
_DUE = "state/ai-app-weekly/temp/ai-app-weekly/due-2026-11-13.json"
_REPORT = "state/ai-app-weekly/temp/ai-app-weekly/report-2026-11-13.md"
_RETURN = "state/ai-app-weekly/runs/2026-11-13/feishu-return.json"
_DOC = "state/ai-app-weekly/runs/2026-11-13/doc.json"


class _HistoryFrame:
    empty = True


class _Ticker:
    def history(self, period: str = "1y", auto_adjust: bool = True):
        return _HistoryFrame()


class _YFinance:
    def Ticker(self, symbol: str):
        return _Ticker()


def _load():
    name = "dalton_ai_app_weekly_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, CLI_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(module, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, json.loads(buffer.getvalue())


def _ok(code: int, payload: dict, label: str) -> dict:
    assert code == 0 and payload.get("ok") is True, {
        "command": label,
        "code": code,
        "error": payload.get("error"),
        "hint": payload.get("hint"),
    }
    return payload


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(src.read_bytes())


def _stub_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep self-test's yfinance import off the network. Screen below does not pass --with-price."""
    module = create_autospec(_YFinance, instance=True)

    def factory(_symbol: str):
        ticker = create_autospec(_Ticker, instance=True)
        frame = create_autospec(_HistoryFrame, instance=True)
        frame.empty = True
        ticker.history.return_value = frame
        return ticker

    module.Ticker.side_effect = factory
    monkeypatch.setitem(sys.modules, "yfinance", module)


def _seed(workspace: Path) -> None:
    workspace.mkdir()
    (workspace / "state/ai_app_db").mkdir(parents=True)
    (workspace / "state/ai_app_db/.migrated.json").write_text("{}\n", encoding="utf-8")
    _copy(FIXTURES / "net-2025q1-fundamental.jsonl", workspace / _FUND)
    fund = workspace / _FUND
    if not fund.read_bytes().endswith(b"\n"):
        fund.write_bytes(fund.read_bytes() + b"\n")
    _copy(FIXTURES / "snapshot-2026-11-06.json", workspace / "state/ai_app_db/snapshots/2026-11-06.json")
    _copy(FIXTURES / "due-2026-11-13.json", workspace / _DUE)
    _copy(FIXTURES / "feishu-return.json", workspace / _RETURN)
    _copy(FIXTURES / "feishu-return.json", workspace / _DOC)
    _copy(SKILL_ROOT / "templates" / "weekly-report.md", workspace / _REPORT)


def _ws(workspace: Path, *args: str) -> list[str]:
    return ["--json", *args, "--workspace-dir", str(workspace)]


def test_frontmatter_registry_and_profile() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    description = meta["description"].strip()
    assert description == read_skill_description(SKILL_NAME)
    assert len(description) <= 120
    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert len(_DALTON_SKILL_SPECS) == 44
    assert PROFILE in REPORT_PROFILES


def test_skill_tree_has_no_recipient() -> None:
    seen = False
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        seen = True
        text = path.read_text(encoding="utf-8")
        for banned in BANNED_IDS:
            assert banned not in text, path.name
        assert _OC_RE.search(text) is None, path.name
        assert _OU_RE.search(text) is None, path.name
    assert seen


def test_offline_commands_and_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_prices(monkeypatch)
    module = _load()
    workspace = tmp_path / "ws"
    _seed(workspace)
    fund = workspace / _FUND
    original_fund = fund.read_bytes()
    assert b'"period": "2025Q1"' in original_fund
    assert b"378.6" in original_fund

    _ok( *_run(module, _ws(workspace, "self-test")), "self-test")
    plan = _ok(*_run(module, _ws(workspace, "plan", "--as-of", AS_OF)), "plan")
    assert plan["universe_size"] == 290
    _ok(*_run(module, _ws(workspace, "diff", "--against", "2026-11-06")), "diff")
    _ok(*_run(module, _ws(workspace, "screen")), "screen")
    _ok(*_run(module, _ws(workspace, "reconcile", "--expected", "NET")), "reconcile")
    _ok(*_run(module, _ws(workspace, "reconcile", "--expected-file", _DUE)), "reconcile-file")
    _ok(*_run(module, _ws(workspace, "validate", "--file", _REPORT)), "validate")

    status = _ok(*_run(module, _ws(workspace, "doc-status", "--date", DAY)), "doc-status")
    assert status["create"] is False
    assert status["stored"] is True
    assert status["body_incomplete"] is False
    assert status["url"] == f"https://feishu.cn/docx/{DOC_ID}"
    locked = (workspace / _DOC).read_bytes()
    saved = _ok(*_run(module, _ws(workspace, "save-doc", "--date", DAY, "--file", _RETURN)), "save-doc")
    assert saved["stored"] is False and saved["skipped"] is True and saved["create"] is False
    assert (workspace / _DOC).read_bytes() == locked

    added = _ok(
        *_run(
            module,
            _ws(
                workspace,
                "add-fundamental",
                "--company", "Cloudflare",
                "--ticker", "NET",
                "--track", "tob_software",
                "--period", "2026Q4",
                "--currency", "USD",
                "--total-revenue-orig-mn", "500",
                "--total-revenue-usd-mn", "500",
                "--revenue-yoy-pct", "30",
                "--evidence-grade", "A",
                "--source-url", "https://www.cloudflare.com/",
                "--source-date", DAY,
                "--quote", "Fourth quarter revenue totaled 500 million.",
            ),
        ),
        "add-fundamental",
    )
    assert added["record"]["period"] == "2026Q4"
    grown = fund.read_bytes()
    assert grown.startswith(original_fund)
    assert b'"period": "2025Q1"' in grown and b"378.6" in grown

    code, duplicate = _run(
        module,
        _ws(
            workspace,
            "add-fundamental",
            "--company", "Cloudflare",
            "--ticker", "NET",
            "--track", "tob_software",
            "--period", "2025Q1",
            "--currency", "USD",
            "--total-revenue-usd-mn", "999",
            "--revenue-yoy-pct", "1",
            "--evidence-grade", "A",
            "--source-url", "https://www.cloudflare.com/",
            "--source-date", DAY,
            "--quote", "should not be written",
        ),
    )
    assert code == 1 and duplicate["ok"] is False
    assert fund.read_bytes() == grown

    _ok(
        *_run(
            module,
            _ws(
                workspace,
                "add-metric",
                "--company", "Cloudflare",
                "--ticker", "NET",
                "--track", "tob_software",
                "--metric", "ai_revenue",
                "--period", "2026Q4",
                "--value", "100",
                "--unit", "USD_mn",
                "--value-usd-mn", "100",
                "--definition", "AI revenue disclosed as a separate line",
                "--ai-basis", "company_ai_line",
                "--evidence-grade", "A",
                "--source-url", "https://www.cloudflare.com/",
                "--source-date", DAY,
                "--quote", "AI revenue was 100 million.",
            ),
        ),
        "add-metric",
    )
    metric = workspace / _METRIC
    stored = metric.read_bytes()
    code, again = _run(
        module,
        _ws(
            workspace,
            "add-metric",
            "--company", "Cloudflare",
            "--ticker", "NET",
            "--track", "tob_software",
            "--metric", "ai_revenue",
            "--period", "2026Q4",
            "--value", "999",
            "--unit", "USD_mn",
            "--value-usd-mn", "999",
            "--definition", "AI revenue disclosed as a separate line",
            "--ai-basis", "company_ai_line",
            "--evidence-grade", "A",
            "--source-url", "https://www.cloudflare.com/",
            "--source-date", DAY,
            "--quote", "should not be written",
        ),
    )
    assert code == 1 and again["ok"] is False
    assert metric.read_bytes() == stored

    screened = _ok(*_run(module, _ws(workspace, "screen")), "screen-after-add")
    assert any(
        row.get("ticker") == "NET" and row.get("metric") == "ai_revenue" and row.get("period") == "2026Q4"
        for row in screened["results"]
    )
    snap = _ok(
        *_run(module, _ws(workspace, "snapshot", "--write", "--as-of", AS_OF)),
        "snapshot",
    )
    assert snap["written"] is True and snap["date"] == DAY
    assert snap["path"] == f"state/ai_app_db/snapshots/{DAY}.json"
    assert snap["path"] in snap["message"]
    assert not Path(snap["path"]).is_absolute()
    assert (workspace / snap["path"]).is_file()


def test_missing_workspace_dir_is_rejected(tmp_path: Path) -> None:
    module = _load()
    code, payload = _run(module, ["--json", "self-test"])
    assert code == 1 and payload["ok"] is False
    assert payload["error"] == "missing --workspace-dir"
    code, missing = _run(module, ["--json", "plan", "--workspace-dir", str(tmp_path / "absent")])
    assert code == 1 and missing["ok"] is False
    assert missing["error"] == "workspace not found"


def test_writes_do_not_escape(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "state")
    _copy(FIXTURES / "feishu-return.json", workspace / "feishu-return.json")
    code, payload = _run(
        module,
        _ws(workspace, "save-doc", "--date", DAY, "--file", "feishu-return.json"),
    )
    assert code == 1 and payload["ok"] is False
    assert "path must stay inside the workspace" in payload["error"]
    assert list(outside.iterdir()) == []

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "state").mkdir()
    code, escaped = _run(module, _ws(plain, "validate", "--file", "../secret.md"))
    assert code == 1 and escaped["ok"] is False
    assert "path must stay inside the workspace" in escaped["error"]
    assert not (tmp_path / "secret.md").exists()


def _put_json(workspace: Path, relative: str, payload: dict) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_feishu_incomplete_and_failed_doc_returns(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    _seed(workspace)

    incomplete_day = "2026-11-20"
    incomplete_file = f"state/ai-app-weekly/runs/{incomplete_day}/feishu-return.json"
    _put_json(
        workspace,
        incomplete_file,
        {
            "ok": False,
            "error": "content_insert_failed",
            "message": "文档已创建但正文没写进去",
            "document_id": DOC_ID,
            "url": f"https://feishu.cn/docx/{DOC_ID}",
        },
    )
    saved = _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", incomplete_day, "--file", incomplete_file)),
        "incomplete-save",
    )
    assert saved["stored"] is True and saved["create"] is False and saved["body_incomplete"] is True
    doc = workspace / f"state/ai-app-weekly/runs/{incomplete_day}/doc.json"
    assert doc.read_bytes() == (workspace / incomplete_file).read_bytes()
    assert not (workspace / f"state/ai-app-weekly/runs/{incomplete_day}/doc.failed.json").exists()
    status = _ok(*_run(module, _ws(workspace, "doc-status", "--date", incomplete_day)), "incomplete-status")
    assert status["create"] is False and status["stored"] is True and status["body_incomplete"] is True
    assert status["url"] == f"https://feishu.cn/docx/{DOC_ID}"

    missing_day = "2026-11-27"
    missing_file = f"state/ai-app-weekly/runs/{missing_day}/feishu-return.json"
    _put_json(workspace, missing_file, {"ok": False, "error": "bridge_down"})
    opened = _ok(*_run(module, _ws(workspace, "doc-status", "--date", missing_day)), "no-doc-yet")
    assert opened["create"] is True
    first = _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", missing_day, "--file", missing_file)),
        "fail-once",
    )
    assert first["create"] is True and first["attempts"] == 1 and first["stored"] is True
    assert not (workspace / f"state/ai-app-weekly/runs/{missing_day}/doc.json").exists()
    failed_path = workspace / f"state/ai-app-weekly/runs/{missing_day}/doc.failed.json"
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["attempts"] == 1
    assert len(failed_record["fingerprints"]) == 1
    failed_bytes = failed_path.read_bytes()
    midway = _ok(*_run(module, _ws(workspace, "doc-status", "--date", missing_day)), "after-one-failure")
    assert midway["create"] is True and midway["attempts"] == 1
    same = _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", missing_day, "--file", missing_file)),
        "fail-same-payload",
    )
    assert same["create"] is True and same["attempts"] == 1 and same["skipped"] is True
    assert failed_path.read_bytes() == failed_bytes
    other_file = f"state/ai-app-weekly/runs/{missing_day}/feishu-return-2.json"
    _put_json(workspace, other_file, {"ok": False, "error": "bridge_down_other"})
    second = _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", missing_day, "--file", other_file)),
        "fail-twice",
    )
    assert second["create"] is False and second["reason"] == "create_failed_twice" and second["attempts"] == 2
    assert not (workspace / f"state/ai-app-weekly/runs/{missing_day}/doc.json").exists()
    failed_twice = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_twice["attempts"] == 2 and len(failed_twice["fingerprints"]) == 2
    stopped = _ok(*_run(module, _ws(workspace, "doc-status", "--date", missing_day)), "create-failed-twice")
    assert stopped["create"] is False and stopped["reason"] == "create_failed_twice"

    empty_day = "2026-12-04"
    empty_file = f"state/ai-app-weekly/runs/{empty_day}/feishu-return.json"
    _put_json(
        workspace,
        empty_file,
        {"ok": False, "error": "content_insert_failed", "document_id": "", "message": "无可用 id"},
    )
    empty = _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", empty_day, "--file", empty_file)),
        "empty-document-id",
    )
    assert empty["create"] is True and empty["attempts"] == 1
    assert not (workspace / f"state/ai-app-weekly/runs/{empty_day}/doc.json").exists()
    assert (workspace / f"state/ai-app-weekly/runs/{empty_day}/doc.failed.json").is_file()

    plain_day = "2026-12-18"
    plain_file = f"state/ai-app-weekly/runs/{plain_day}/feishu-return.json"
    _put_json(workspace, plain_file, {"ok": True, "message": "created without id"})
    plain = _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", plain_day, "--file", plain_file)),
        "ok-without-url",
    )
    assert plain["create"] is True and plain["attempts"] == 1
    assert not (workspace / f"state/ai-app-weekly/runs/{plain_day}/doc.json").exists()
    assert (workspace / f"state/ai-app-weekly/runs/{plain_day}/doc.failed.json").is_file()


def test_relative_workspace_dir_reads_and_writes_same_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    _seed(workspace)
    monkeypatch.chdir(tmp_path)
    module = _load()
    day = "2026-11-20"
    saved = _ok(
        *_run(module, ["--json", "save-doc", "--date", day, "--file", _RETURN, "--workspace-dir", "ws"]),
        "relative-save",
    )
    dest = workspace / f"state/ai-app-weekly/runs/{day}/doc.json"
    assert saved["stored"] is True
    assert dest.is_file()
    assert dest.read_bytes() == (workspace / _RETURN).read_bytes()
    status = _ok(
        *_run(module, ["--json", "doc-status", "--date", day, "--workspace-dir", "ws"]),
        "relative-status",
    )
    assert status["stored"] is True and status["create"] is False
    assert status["url"] == f"https://feishu.cn/docx/{DOC_ID}"
    assert dest.read_bytes() == (workspace / _RETURN).read_bytes()

    linked = tmp_path / "linked-parent"
    os.symlink(tmp_path, linked)
    code, rejected = _run(
        module,
        ["--json", "doc-status", "--date", day, "--workspace-dir", str(linked / "ws")],
    )
    assert code == 1 and rejected["ok"] is False
    assert rejected["error"] == "refusing symlink in workspace path"


def test_retier_apply_is_rejected(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    _seed(workspace)
    universe = SKILL_ROOT / "references" / "universe.json"
    before = universe.read_bytes()
    code, payload = _run(module, _ws(workspace, "retier", "--apply"))
    assert code == 1 and payload["ok"] is False
    assert "references/universe.json" in payload["hint"]
    assert universe.read_bytes() == before
    dry = _ok(*_run(module, _ws(workspace, "retier")), "retier")
    assert dry["applied"] is False
    assert "试算，未写入" in dry["message"]
    assert universe.read_bytes() == before


def test_rank_ledger_and_archive_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_prices(monkeypatch)
    module = _load()
    workspace = tmp_path / "ws"
    _seed(workspace)
    rank = _ok(*_run(module, _ws(workspace, "ai-monetization", "--by", "yoy")), "ai-monetization")
    assert rank["basis"] == "yoy" and "boards" in rank
    headline = _ok(*_run(module, _ws(workspace, "headline-monetization")), "headline-monetization")
    assert "results" in headline and "by_growth" in headline
    ledger = _ok(
        *_run(
            module,
            _ws(workspace, "coverage-ledger", "--write", "--since", "2025Q1", "--until", "2026Q3"),
        ),
        "coverage-ledger",
    )
    assert ledger["since"] == "2025Q1" and ledger["until"] == "2026Q3"
    assert (workspace / "state/ai_app_db/coverage.json").is_file()
    archived = _ok(
        *_run(module, _ws(workspace, "archive-report", "--date", DAY, "--file", _REPORT)),
        "archive-report",
    )
    assert archived["archived"] is True
    assert archived["path"] == f"research-outputs/ai-application-weekly/{DAY}.md"
    copied = workspace / archived["path"]
    assert copied.read_bytes() == (workspace / _REPORT).read_bytes()


def test_archive_and_snapshot_follow_doc_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_prices(monkeypatch)
    module = _load()
    workspace = tmp_path / "ws"
    _seed(workspace)

    incomplete_day = "2026-11-20"
    incomplete_as_of = "2026-11-20T15:00:00+08:00"
    incomplete_file = f"state/ai-app-weekly/runs/{incomplete_day}/feishu-return.json"
    _put_json(
        workspace,
        incomplete_file,
        {
            "ok": False,
            "error": "content_insert_failed",
            "document_id": DOC_ID,
            "message": "文档已创建但正文没写进去",
        },
    )
    _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", incomplete_day, "--file", incomplete_file)),
        "seed-incomplete-doc",
    )
    doc_path = workspace / f"state/ai-app-weekly/runs/{incomplete_day}/doc.json"
    doc_bytes = doc_path.read_bytes()
    prior_snapshot = workspace / "state/ai_app_db/snapshots/2026-11-06.json"
    prior_bytes = prior_snapshot.read_bytes()
    report_bytes = (workspace / _REPORT).read_bytes()
    archive_path = workspace / f"research-outputs/ai-application-weekly/{incomplete_day}.md"
    snap_path = workspace / f"state/ai_app_db/snapshots/{incomplete_day}.json"

    code, archived = _run(
        module,
        _ws(workspace, "archive-report", "--date", incomplete_day, "--file", _REPORT),
    )
    assert code == 1 and archived["ok"] is False and archived["reason"] == "body_incomplete"
    code, snap = _run(
        module,
        _ws(workspace, "snapshot", "--write", "--as-of", incomplete_as_of),
    )
    assert code == 1 and snap["ok"] is False and snap["reason"] == "body_incomplete"
    # --no-doc is only for periods with no document record at all.
    code, snap_no_doc = _run(
        module,
        _ws(workspace, "snapshot", "--write", "--no-doc", "--as-of", incomplete_as_of),
    )
    assert code == 1 and snap_no_doc["reason"] == "body_incomplete"
    assert doc_path.read_bytes() == doc_bytes
    assert prior_snapshot.read_bytes() == prior_bytes
    assert (workspace / _REPORT).read_bytes() == report_bytes
    assert not archive_path.exists()
    assert not snap_path.exists()

    failed_day = "2026-12-04"
    failed_file = f"state/ai-app-weekly/runs/{failed_day}/feishu-return.json"
    _put_json(workspace, failed_file, {"ok": False, "error": "bridge_down"})
    _ok(
        *_run(module, _ws(workspace, "save-doc", "--date", failed_day, "--file", failed_file)),
        "seed-one-failure",
    )
    code, incomplete_create = _run(
        module,
        _ws(workspace, "archive-report", "--date", failed_day, "--file", _REPORT),
    )
    assert code == 1 and incomplete_create["reason"] == "create_incomplete"
    assert not (workspace / f"research-outputs/ai-application-weekly/{failed_day}.md").exists()

    broken_day = "2026-12-18"
    broken_doc = workspace / f"state/ai-app-weekly/runs/{broken_day}/doc.json"
    broken_doc.parent.mkdir(parents=True, exist_ok=True)
    broken_doc.write_text("{", encoding="utf-8")
    code, unreadable = _run(
        module,
        _ws(workspace, "archive-report", "--date", broken_day, "--file", _REPORT),
    )
    assert code == 1 and unreadable["reason"] == "doc_record_unreadable"
    assert broken_doc.read_text(encoding="utf-8") == "{"
    assert not (workspace / f"research-outputs/ai-application-weekly/{broken_day}.md").exists()

    complete_archive = workspace / f"research-outputs/ai-application-weekly/{DAY}.md"
    complete_snap = workspace / f"state/ai_app_db/snapshots/{DAY}.json"
    archived_ok = _ok(
        *_run(module, _ws(workspace, "archive-report", "--date", DAY, "--file", _REPORT)),
        "archive-complete",
    )
    assert archived_ok["archived"] is True
    assert complete_archive.read_bytes() == report_bytes
    snap_ok = _ok(
        *_run(module, _ws(workspace, "snapshot", "--write", "--as-of", AS_OF)),
        "snapshot-complete",
    )
    assert snap_ok["written"] is True and snap_ok["path"] == f"state/ai_app_db/snapshots/{DAY}.json"
    assert complete_snap.is_file()

    quiet_day = "2026-12-11"
    quiet_as_of = "2026-12-11T15:00:00+08:00"
    quiet_snap = workspace / f"state/ai_app_db/snapshots/{quiet_day}.json"
    code, missing = _run(
        module,
        _ws(workspace, "snapshot", "--write", "--as-of", quiet_as_of),
    )
    assert code == 1 and missing["ok"] is False and missing["reason"] == "doc_missing"
    assert not quiet_snap.exists()
    quiet = _ok(
        *_run(module, _ws(workspace, "snapshot", "--write", "--no-doc", "--as-of", quiet_as_of)),
        "snapshot-no-doc",
    )
    assert quiet["written"] is True and quiet["date"] == quiet_day
    assert quiet_snap.is_file()
    assert prior_snapshot.read_bytes() == prior_bytes
