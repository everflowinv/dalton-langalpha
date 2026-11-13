"""Richard earnings weekly scan: roll, record, calendar apply, and summary."""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import yaml

from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-richard-earnings"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "richard_earnings.py"
PROFILE = "richard-earnings"
DESCRIPTION = "无人值守的 Richard 财报日期周扫：按 105 家名单核查官方披露日期，写入日历并提交纯文本汇总。"
ET = ZoneInfo("America/New_York")
FIXED_NOW = datetime(2026, 11, 14, 7, 0, tzinfo=ET)
SCAN_DAY = "2026-11-14"
BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")
BANNED_RECIPIENTS = (
    "932169512197955636",
    "1481256083589697566",
    "richard.lu.everflowinv@gmail.com",
    "ou_",
    "oc_",
)
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
_ORIGINALS: dict[str, Any] = {}
MARKER = "Created by Dalton earnings tracker"


def _load():
    name = "dalton_richard_earnings_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, CLI_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _ORIGINALS["call_research"] = module.call_research
    _ORIGINALS["now_et"] = module.now_et
    return module


def _restore(module) -> None:
    module.call_research = _ORIGINALS["call_research"]
    module.now_et = _ORIGINALS["now_et"]


def _run(module, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, json.loads(buffer.getvalue())


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    return workspace


def _prepare(module, responses: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    _restore(module)
    module.now_et = create_autospec(_ORIGINALS["now_et"], return_value=FIXED_NOW)
    calls: list[dict[str, Any]] = []
    if responses is None:
        return calls
    queue = list(responses)

    def _fake(**arguments: Any) -> dict[str, Any]:
        calls.append(arguments)
        if not queue:
            raise AssertionError("unexpected research call")
        return queue.pop(0)

    module.call_research = create_autospec(_ORIGINALS["call_research"], side_effect=_fake)
    return calls


def _argv(workspace: Path, command: str, *extra: str) -> list[str]:
    return ["--json", command, "--workspace-dir", str(workspace), *extra]


def _write_watch(workspace: Path, payload: dict[str, Any]) -> None:
    path = workspace / "state" / "richard-earnings" / "watchlist.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_watch(workspace: Path) -> dict[str, Any]:
    path = workspace / "state" / "richard-earnings" / "watchlist.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _record_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "ticker": "AFRM",
        "name": "Scan Name",
        "status": "announced",
        "earnings_date": "2026-11-20",
        "timing": "BMO",
        "quarter_label": "Q2 FY2027",
        "confidence": "high",
        "source_url": "https://example.test/ir",
        "evidence": "IR press release",
        "notes": None,
        "group": "A",
    }
    body.update(overrides)
    return body


def _write_input(workspace: Path, payload: dict[str, Any], name: str = "row.json") -> str:
    relative = f"in/{name}"
    path = workspace / "in" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return relative


def _record(module, workspace: Path, payload: dict[str, Any], name: str = "row.json") -> tuple[int, dict]:
    relative = _write_input(workspace, payload, name)
    return _run(module, _argv(workspace, "record", "--input", relative))


def _list_ok(events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ok": True, "status": "ok", "data": {"events": events or [], "event_count": len(events or [])}}


def _insert_ok(event_id: str = "evt-new-1") -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "data": {
            "id": event_id,
            "summary": "created",
            "description": "created",
            "start": {"date": "2026-11-20"},
            "end": {"date": "2026-11-21"},
        },
    }


def _delete_ok(event_id: str) -> dict[str, Any]:
    return {"ok": True, "status": "ok", "data": {"deleted": True, "id": event_id}}


def _confirmed(ticker: str, **overrides: Any) -> dict[str, Any]:
    entry = {
        "ticker": ticker,
        "name": ticker,
        "date": "2026-11-01",
        "timing": "BMO",
        "quarter": "Q1 2026",
        "calendar_event_id": "evt-old-1",
        "confidence": "high",
        "source": "https://example.test/old",
    }
    entry.update(overrides)
    return entry


def test_frontmatter_registry_profile_and_hard_rule() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )

    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    description = meta["description"].strip()
    assert description == DESCRIPTION
    assert description == read_skill_description(SKILL_NAME)
    assert len(description) <= 120
    assert "触发词" not in description
    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert len(_DALTON_SKILL_SPECS) == 45
    assert PROFILE in REPORT_PROFILES
    body, _heading, rules = text.partition("## 撰写硬规则")
    assert text.count("## 撰写硬规则") == 1
    for phrase in BANNED_PROSE:
        assert phrase not in body
        assert phrase in rules
    assert rules.strip().endswith("沿用原有子任务权限与限制。")
    assert "muse-spark-1.3-oauth" in body
    script = CLI_PATH.read_text(encoding="utf-8")
    assert '"method": "tools/call"' not in script
    assert "openclaw gateway" not in script


def test_skill_tree_has_no_recipient_or_real_email() -> None:
    seen = False
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        seen = True
        text = path.read_text(encoding="utf-8")
        for banned in BANNED_RECIPIENTS:
            assert banned not in text, path.name
    assert seen


def test_self_test_and_list(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    calls = _prepare(module, [_list_ok()])
    code, body = _run(module, _argv(workspace, "self-test"))
    assert code == 0
    assert body["ok"] is True
    assert body["calendar_ok"] is True
    assert body["universe_ok"] is True
    assert body["universe_count"] == 105
    assert body["watchlist_exists"] is False
    assert body["report_run_id"] == "richard-earnings-2026-11-14"
    assert calls[0]["route"] == "gcal"
    assert calls[0]["operation"] == "list_events"
    assert calls[0]["limit"] == 1
    assert calls[0]["time_min"] == "2026-11-14T00:00:00-05:00"
    assert calls[0]["time_max"] == "2026-11-15T00:00:00-05:00"
    _prepare(module, [])
    code, listed = _run(module, _argv(workspace, "list"))
    assert code == 0
    assert listed["count"] == 105
    assert listed["groups"] == {"A": 27, "B": 34, "C": 22, "D": 22}
    names = {row["ticker"]: row["name"] for row in listed["companies"]}
    assert names["SPGI"] == "S&P Global"
    assert names["2513.HK"] == "Zhipu 智谱 (Z.ai)"
    assert names["601100.SH"] == "Hengli Hydraulic"
    assert names["BRK.B"] == "Berkshire Hathaway"


def test_roll_moves_expired_and_keeps_same_day(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    _write_watch(
        workspace,
        {
            "updated": "2026-10-01",
            "calendar": "person@example.test",
            "source_sheet": "weekly",
            "confirmed": [
                _confirmed("PDD", name="PDD Holdings", quarter="Q2 2026 / H1 2026", date="2026-11-01"),
                _confirmed("AFRM", name="Affirm", quarter="FQ4 2026", date="2026-11-13"),
                _confirmed("MPNGY", name="Meituan", quarter="2026 中期", date="2026-08-20"),
                _confirmed("WMT", name="Walmart", quarter="2026 全年业绩", date="2026-02-19"),
                _confirmed("AMZN", name="Amazon", quarter="Q3 2026", date="2026-11-14"),
                _confirmed("MSFT", name="Microsoft", date="soon"),
                _confirmed("GOOGL", name="Alphabet (Google)", date="2026-12-01"),
            ],
            "pending": [{"ticker": "AFRM", "name": "Affirm", "note": "旧备注"}],
            "reported": [],
        },
    )
    code, body = _run(module, _argv(workspace, "roll"))
    dumped = json.dumps(body, ensure_ascii=False)
    assert code == 0
    assert body["moved_count"] == 4
    assert "person@example.test" not in dumped
    notes = {item["ticker"]: item["note"] for item in body["moved"]}
    assert notes["PDD"] == "下一期 Q3 2026"
    assert notes["AFRM"] == "下一期 FQ1 2027"
    assert notes["MPNGY"] == "下一期 2026 全年业绩"
    assert notes["WMT"] == "下一期 2027 中期"
    saved = _read_watch(workspace)
    assert saved["calendar"] == "person@example.test"
    assert saved["source_sheet"] == "weekly"
    assert saved["updated"] == SCAN_DAY
    assert {item["ticker"] for item in saved["confirmed"]} == {"AMZN", "MSFT", "GOOGL"}
    assert [item["ticker"] for item in saved["pending"]].count("AFRM") == 1
    affirm = next(item for item in saved["pending"] if item["ticker"] == "AFRM")
    assert affirm["note"] == "下一期 FQ1 2027"
    reported = next(item for item in saved["reported"] if item["ticker"] == "AFRM")
    assert reported["calendar_event_id"] == "evt-old-1"
    assert reported["date"] == "2026-11-13"


def test_roll_skips_watchlist_write_when_nothing_expired(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    payload = {
        "updated": "2026-10-01",
        "confirmed": [_confirmed("AMZN", name="Amazon", date="2026-11-14")],
        "pending": [],
        "reported": [],
    }
    _write_watch(workspace, payload)
    before = (workspace / "state" / "richard-earnings" / "watchlist.json").read_bytes()
    code, body = _run(module, _argv(workspace, "roll"))
    after = (workspace / "state" / "richard-earnings" / "watchlist.json").read_bytes()
    assert code == 0
    assert body["moved"] == []
    assert before == after
    roll = json.loads(
        (workspace / "state" / "richard-earnings" / "scans" / SCAN_DAY / "roll.json").read_text(
            encoding="utf-8"
        )
    )
    assert roll["moved"] == []


def test_record_validates_fields_and_rejects_path_escape(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    missing = _record_body()
    missing.pop("earnings_date")
    code, body = _record(module, workspace, missing, "missing.json")
    assert code == 1
    assert body["error"] == "missing_fields"
    code, body = _record(module, workspace, _record_body(status="maybe"), "bad-status.json")
    assert code == 1
    assert body["error"] == "invalid_status"
    code, body = _record(module, workspace, _record_body(earnings_date="2026-11-13"), "past.json")
    assert code == 1
    assert body["error"] == "invalid_announced"
    assert not (workspace / "state" / "richard-earnings" / "scans" / SCAN_DAY / "A.jsonl").exists()
    code, body = _record(module, workspace, _record_body(), "ok.json")
    assert code == 0
    assert body["status"] == "recorded"
    code, again = _record(module, workspace, _record_body(), "dup.json")
    assert code == 0
    assert again["status"] == "already_recorded"
    stored = (
        workspace / "state" / "richard-earnings" / "scans" / SCAN_DAY / "A.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(stored) == 1
    code, failed = _record(
        module,
        workspace,
        _record_body(
            ticker="AAPL",
            name="Apple",
            group="B",
            status="scan_failed",
            earnings_date=None,
            timing=None,
            confidence=None,
            quarter_label=None,
            source_url=None,
            evidence=None,
        ),
        "failed.json",
    )
    assert code == 0
    assert failed["status"] == "recorded"
    assert json.loads(
        (workspace / "state" / "richard-earnings" / "scans" / SCAN_DAY / "B.jsonl").read_text(
            encoding="utf-8"
        )
    )["status"] == "scan_failed"
    code, escaped = _run(module, _argv(workspace, "record", "--input", "../secret.json"))
    assert code == 1
    assert escaped["ok"] is False
    assert "workspace" in escaped["error"]
    code, absolute = _run(module, _argv(workspace, "record", "--input", "/tmp/outside.json"))
    assert code == 1
    assert absolute["ok"] is False
    assert "workspace" in absolute["error"]


def test_progress_dedupes_counts_and_orders_the_queue(tmp_path: Path) -> None:
    module = _load()
    deduped = _workspace(tmp_path)
    _prepare(module)
    day_dir = deduped / "state" / "richard-earnings" / "scans" / SCAN_DAY
    day_dir.mkdir(parents=True)
    line = json.dumps(_record_body(), ensure_ascii=False)
    (day_dir / "A.jsonl").write_text(line + "\n" + line + "\n", encoding="utf-8")
    code, body = _run(module, _argv(deduped, "progress"))
    assert code == 0
    assert body["counts"] == {"A": 1, "B": 0, "C": 0, "D": 0}
    assert body["unique"] == 1
    assert body["gate_open"] is False

    ordered = _workspace(tmp_path / "ordered")
    _write_watch(
        ordered,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed("AFRM", name="Affirm", date="2026-12-01"),
                _confirmed("MSFT", name="Microsoft", date="soon"),
            ],
            "pending": [
                {"ticker": "AAPL", "name": "Apple", "note": "下一期"},
                {"ticker": "GOOGL", "name": "Alphabet (Google)", "note": "下一期"},
            ],
            "reported": [],
        },
    )
    code, body = _run(module, _argv(ordered, "progress"))
    assert code == 0
    tickers = [item["ticker"] for item in body["remaining"]]
    assert (
        tickers.index("GOOGL")
        < tickers.index("AFRM")
        < tickers.index("AMZN")
        < tickers.index("MSFT")
        < tickers.index("AAPL")
    )
    assert body["next"]["ticker"] == "GOOGL"


def test_progress_gate_uses_start_time_and_full_coverage(tmp_path: Path) -> None:
    module = _load()
    early = _workspace(tmp_path)
    _prepare(module)
    started = {
        "started_at": (FIXED_NOW - timedelta(minutes=74)).isoformat(),
        "date": SCAN_DAY,
    }
    started_path = early / "state" / "richard-earnings" / "scans" / SCAN_DAY / "started.json"
    started_path.parent.mkdir(parents=True)
    started_path.write_text(json.dumps(started), encoding="utf-8")
    code, closed = _run(module, _argv(early, "progress"))
    assert code == 0
    assert closed["gate_open"] is False
    assert closed["started_at"] == started["started_at"]

    opened_ws = _workspace(tmp_path / "open")
    ready = {
        "started_at": (FIXED_NOW - timedelta(minutes=75)).isoformat(),
        "date": SCAN_DAY,
    }
    ready_path = opened_ws / "state" / "richard-earnings" / "scans" / SCAN_DAY / "started.json"
    ready_path.parent.mkdir(parents=True)
    ready_path.write_text(json.dumps(ready), encoding="utf-8")
    code, opened = _run(module, _argv(opened_ws, "progress"))
    assert code == 0
    assert opened["gate_open"] is True
    code, again = _run(module, _argv(opened_ws, "progress"))
    assert again["started_at"] == ready["started_at"]

    full = _workspace(tmp_path / "full")
    universe = json.loads((SKILL_ROOT / "references" / "watchlist_universe.json").read_text(encoding="utf-8"))
    by_group: dict[str, list[str]] = {"A": [], "B": [], "C": [], "D": []}
    for row in universe:
        by_group[row["group"]].append(row["ticker"])
    scan_dir = full / "state" / "richard-earnings" / "scans" / SCAN_DAY
    scan_dir.mkdir(parents=True)
    for group, tickers in by_group.items():
        lines = [
            json.dumps(
                {
                    "ticker": ticker,
                    "name": ticker,
                    "status": "not_announced",
                    "earnings_date": None,
                    "timing": None,
                    "quarter_label": None,
                    "confidence": None,
                    "source_url": None,
                    "evidence": None,
                    "notes": None,
                    "group": group,
                },
                ensure_ascii=False,
            )
            for ticker in tickers
        ]
        (scan_dir / f"{group}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, done = _run(module, _argv(full, "progress"))
    assert code == 0
    assert done["unique"] == 105
    assert done["counts"] == {"A": 27, "B": 34, "C": 22, "D": 22}
    assert done["gate_open"] is True
    assert done["remaining"] == []


def test_apply_creates_reschedules_and_preserves_weaker_rows(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    calls = _prepare(module, [_list_ok(), _insert_ok("evt-new-1")])
    _write_watch(
        workspace,
        {
            "updated": "2026-10-01",
            "calendar": "person@example.test",
            "confirmed": [],
            "pending": [{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}],
            "reported": [],
        },
    )
    code, recorded = _record(module, workspace, _record_body(name="Scan Name"), "create.json")
    assert code == 0 and recorded["status"] == "recorded"
    code, applied = _run(module, _argv(workspace, "apply"))
    assert code == 0
    assert applied["ok"] is True
    assert [call["operation"] for call in calls] == ["list_events", "insert_event"]
    assert calls[0]["time_min"] == "2026-11-20T00:00:00-05:00"
    assert calls[0]["time_max"] == "2026-11-21T00:00:00-05:00"
    assert "-04:00" not in calls[0]["time_min"]
    assert calls[1]["summary"] == "Affirm (AFRM) Earnings — Q2 FY2027 [BMO]"
    assert calls[1]["description"].splitlines() == [
        "https://example.test/ir",
        "Scanned 2026-11-14",
        "Confidence: high",
    ]
    assert MARKER not in calls[1]["description"]
    assert calls[1]["start"] == {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"}
    assert calls[1]["end"] == {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"}
    saved = _read_watch(workspace)
    assert saved["pending"] == []
    assert saved["confirmed"][0]["name"] == "Affirm"
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-new-1"
    assert saved["calendar"] == "person@example.test"
    assert "person@example.test" not in json.dumps(applied, ensure_ascii=False)

    reschedule = _workspace(tmp_path / "reschedule")
    calls = _prepare(module, [_list_ok(), _insert_ok("evt-new-1"), _delete_ok("evt-old-1")])
    _write_watch(
        reschedule,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed(
                    "AFRM",
                    name="Affirm",
                    date="2026-11-18",
                    timing="BMO",
                    quarter="Q2 2026",
                    calendar_event_id="evt-old-1",
                    confidence="high",
                )
            ],
            "pending": [],
            "reported": [],
        },
    )
    _record(
        module,
        reschedule,
        _record_body(earnings_date="2026-11-21", timing="AMC", quarter_label="Q3 2026"),
        "replace.json",
    )
    code, replaced = _run(module, _argv(reschedule, "apply"))
    assert code == 0
    assert replaced["ok"] is True
    assert [call["operation"] for call in calls] == ["list_events", "insert_event", "delete_event"]
    assert calls[2]["event_id"] == "evt-old-1"
    assert "Q2 2026" in calls[1]["summary"]
    assert calls[1]["start"]["dateTime"] == "2026-11-21T16:00:00"
    saved = _read_watch(reschedule)
    assert saved["confirmed"][0]["date"] == "2026-11-21"
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-new-1"

    weaker = _workspace(tmp_path / "weaker")
    calls = _prepare(module, [])
    _write_watch(
        weaker,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed(
                    "AFRM",
                    name="Affirm",
                    date="2026-11-18",
                    timing="BMO",
                    confidence="high",
                    calendar_event_id="evt-old-1",
                )
            ],
            "pending": [],
            "reported": [],
        },
    )
    _record(
        module,
        weaker,
        _record_body(earnings_date="2026-12-01", timing="AMC", confidence="medium"),
        "weak.json",
    )
    code, skipped = _run(module, _argv(weaker, "apply"))
    assert code == 0
    assert calls == []
    assert skipped["applied"] == []
    assert "weaker" in skipped["skipped"][0]["reason"]
    assert _read_watch(weaker)["confirmed"][0]["calendar_event_id"] == "evt-old-1"
    assert _read_watch(weaker)["confirmed"][0]["date"] == "2026-11-18"

    quiet = _workspace(tmp_path / "quiet")
    calls = _prepare(module, [])
    _write_watch(
        quiet,
        {
            "updated": "2026-10-01",
            "confirmed": [],
            "pending": [
                {"ticker": "AMZN", "name": "Amazon", "note": "下一期"},
                {"ticker": "AAPL", "name": "Apple", "note": "下一期"},
            ],
            "reported": [],
        },
    )
    _record(
        module,
        quiet,
        _record_body(
            ticker="AMZN",
            name="Amazon",
            group="A",
            status="not_announced",
            earnings_date=None,
            timing=None,
            confidence=None,
        ),
        "none.json",
    )
    _record(
        module,
        quiet,
        _record_body(
            ticker="AAPL",
            name="Apple",
            group="B",
            status="already_reported",
            earnings_date="2026-10-30",
            timing="AMC",
            confidence="high",
        ),
        "reported.json",
    )
    code, untouched = _run(module, _argv(quiet, "apply"))
    assert code == 0
    assert calls == []
    assert untouched["skipped"] == []
    assert untouched["applied"] == []
    assert {item["ticker"] for item in _read_watch(quiet)["pending"]} == {"AMZN", "AAPL"}


def test_apply_returns_research_errors_unchanged(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    calls = _prepare(module, [{"ok": False, "error": "boom-token"}])
    _write_watch(
        workspace,
        {
            "updated": "2026-10-01",
            "confirmed": [],
            "pending": [{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}],
            "reported": [],
        },
    )
    _record(module, workspace, _record_body(), "boom.json")
    code, body = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert body["errors"] == ["boom-token"]
    assert [call["operation"] for call in calls] == ["list_events"]
    assert _read_watch(workspace)["pending"][0]["ticker"] == "AFRM"

    missing = _workspace(tmp_path / "missing")
    calls = _prepare(module, [])
    _record(module, missing, _record_body(), "only.json")
    code, body = _run(module, _argv(missing, "apply"))
    assert code == 1
    assert body["error"] == "watchlist_missing"
    assert calls == []


def test_summary_formats(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    code, missing = _run(module, _argv(workspace, "summary"))
    assert code == 0
    assert missing["ok"] is True
    assert missing["text"] == "日历未写入：apply 没有运行"
    assert missing["report_run_id"] == "richard-earnings-2026-11-14"

    partial = _workspace(tmp_path / "partial")
    _record(module, partial, _record_body(), "one.json")
    _record(
        module,
        partial,
        _record_body(ticker="AAPL", name="Apple", group="B", status="not_announced", earnings_date=None, timing=None, confidence=None),
        "two.json",
    )
    apply_doc = {
        "union": 10,
        "confirmed": 4,
        "pending": 6,
        "scanned_unique": 99,
        "applied": [
            {
                "action": "create",
                "name": "Affirm",
                "date": "2026-11-20",
                "timing": "BMO",
                "source": "https://example.test/ir",
            }
        ],
        "skipped": [{"ticker": "MSFT", "reason": "weaker"}],
        "errors": [],
    }
    apply_path = partial / "state" / "richard-earnings" / "scans" / SCAN_DAY / "apply.json"
    apply_path.parent.mkdir(parents=True, exist_ok=True)
    apply_path.write_text(json.dumps(apply_doc), encoding="utf-8")
    _code, text = _run(module, _argv(partial, "summary"))
    lines = text["text"].splitlines()
    assert lines[0] == "日历未写入：并集 10，不等于 105"
    assert lines[1] == "日历：新增 1 / 改期 0 / 跳过 1，watchlist confirmed 4 + pending 6 = 10"
    assert "跳过：MSFT weaker" in text["text"]
    assert "新增或改期：Affirm 2026-11-20 BMO https://example.test/ir" in text["text"]

    counted = _workspace(tmp_path / "counted")
    _record(module, counted, _record_body(), "a.json")
    _record(
        module,
        counted,
        _record_body(ticker="AAPL", name="Apple", group="B", status="not_announced", earnings_date=None, timing=None, confidence=None),
        "b.json",
    )
    counted_apply = {
        "union": 105,
        "confirmed": 48,
        "pending": 57,
        "scanned_unique": 99,
        "applied": [
            {
                "action": "replace",
                "name": "Affirm",
                "date": "2026-11-20",
                "timing": "unknown",
                "source": "https://example.test/ir",
            }
        ],
        "skipped": [],
        "errors": ["boom-token"],
    }
    counted_path = counted / "state" / "richard-earnings" / "scans" / SCAN_DAY / "apply.json"
    counted_path.parent.mkdir(parents=True, exist_ok=True)
    counted_path.write_text(json.dumps(counted_apply), encoding="utf-8")
    _code, counted_body = _run(module, _argv(counted, "summary"))
    counted_lines = counted_body["text"].splitlines()
    assert counted_lines[0] == "已核查 2/105"
    assert "改期 1" in counted_lines[1]
    assert "时间未定" in counted_body["text"]
    assert "errors：" in counted_body["text"]
    assert "boom-token" in counted_body["text"]

    quiet = _workspace(tmp_path / "quiet-summary")
    quiet_apply = {
        "union": 105,
        "confirmed": 40,
        "pending": 65,
        "applied": [],
        "skipped": [],
        "errors": [],
    }
    quiet_path = quiet / "state" / "richard-earnings" / "scans" / SCAN_DAY / "apply.json"
    quiet_path.parent.mkdir(parents=True, exist_ok=True)
    quiet_path.write_text(json.dumps(quiet_apply), encoding="utf-8")
    code, quiet_body = _run(module, _argv(quiet, "summary"))
    assert code == 0
    assert quiet_body["text"] == "40 家已确认 / 65 家未披露"


def test_roll_advances_fiscal_year_labels(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    _write_watch(
        workspace,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed("DASH", name="DoorDash", quarter="Q2 FY2027", date="2026-11-01"),
                _confirmed("UBER", name="Uber", quarter="FQ4 FY2026", date="2026-11-02"),
                _confirmed("META", name="Meta Platforms", quarter="Q4 FY2027", date="2026-11-03"),
            ],
            "pending": [],
            "reported": [],
        },
    )
    code, body = _run(module, _argv(workspace, "roll"))
    assert code == 0
    notes = {item["ticker"]: item["note"] for item in body["moved"]}
    assert notes["DASH"] == "下一期 Q3 FY2027"
    assert notes["UBER"] == "下一期 FQ1 FY2027"
    assert notes["META"] == "下一期 Q1 FY2028"


def test_progress_reports_invalid_lines_and_allows_rerecord(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    day_dir = workspace / "state" / "richard-earnings" / "scans" / SCAN_DAY
    day_dir.mkdir(parents=True)
    missing = {"ticker": "AAPL", "name": "Apple", "status": "announced", "group": "B"}
    past = _record_body(
        ticker="MSFT",
        name="Microsoft",
        group="A",
        earnings_date="2026-11-13",
    )
    valid = _record_body(ticker="AMZN", name="Amazon")
    lines = [
        json.dumps(missing, ensure_ascii=False),
        json.dumps(past, ensure_ascii=False),
        json.dumps(valid, ensure_ascii=False),
    ]
    (day_dir / "A.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, body = _run(module, _argv(workspace, "progress"))
    assert code == 0
    assert body["unique"] == 1
    assert body["counts"]["A"] == 1
    assert body["invalid_count"] == 2
    reported = {(item["ticker"], item["error"]) for item in body["invalid_lines"]}
    assert ("AAPL", "missing_fields") in reported
    assert ("MSFT", "invalid_announced") in reported
    remaining = {item["ticker"] for item in body["remaining"]}
    assert "AAPL" in remaining
    assert "MSFT" in remaining
    assert "AMZN" not in remaining
    code, recorded = _record(
        module,
        workspace,
        _record_body(ticker="AAPL", name="Apple", group="B"),
        "again.json",
    )
    assert code == 0
    assert recorded["status"] == "recorded"


def _listed_event(event_id: str, ticker: str, start: dict[str, str], end: dict[str, str]) -> dict[str, Any]:
    return {
        "id": event_id,
        "summary": f"Affirm ({ticker}) Earnings — Q2 2026 [slot]",
        "description": "old",
        "start": start,
        "end": end,
    }


def test_apply_same_day_slot_change_creates_and_deletes(tmp_path: Path) -> None:
    module = _load()
    unknown = _workspace(tmp_path)
    # Old id already sits on the target slot, and another event keeps the old slot.
    # Reuse requires both "not the old id" and a matching slot, so neither is kept.
    old_on_target = _listed_event(
        "evt-old-1",
        "AFRM",
        {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"},
        {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"},
    )
    decoy_all_day = _listed_event(
        "evt-decoy-1",
        "AFRM",
        {"date": "2026-11-20"},
        {"date": "2026-11-21"},
    )
    calls = _prepare(
        module,
        [_list_ok([old_on_target, decoy_all_day]), _insert_ok("evt-new-1"), _delete_ok("evt-old-1")],
    )
    _write_watch(
        unknown,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed(
                    "AFRM",
                    name="Affirm",
                    date="2026-11-20",
                    timing="unknown",
                    calendar_event_id="evt-old-1",
                )
            ],
            "pending": [],
            "reported": [],
        },
    )
    _record(module, unknown, _record_body(earnings_date="2026-11-20", timing="BMO"), "bmo.json")
    code, body = _run(module, _argv(unknown, "apply"))
    assert code == 0
    assert body["ok"] is True
    assert [call["operation"] for call in calls] == ["list_events", "insert_event", "delete_event"]
    assert calls[1]["start"] == {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"}
    assert calls[2]["event_id"] == "evt-old-1"
    assert len(body["applied"]) == 1
    assert _read_watch(unknown)["confirmed"][0]["calendar_event_id"] == "evt-new-1"
    assert _read_watch(unknown)["confirmed"][0]["timing"] == "BMO"

    amc = _workspace(tmp_path / "amc")
    old_on_amc = _listed_event(
        "evt-old-1",
        "AFRM",
        {"dateTime": "2026-11-20T16:00:00", "timeZone": "America/New_York"},
        {"dateTime": "2026-11-20T16:30:00", "timeZone": "America/New_York"},
    )
    decoy_bmo = _listed_event(
        "evt-decoy-1",
        "AFRM",
        {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"},
        {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"},
    )
    calls = _prepare(
        module,
        [_list_ok([old_on_amc, decoy_bmo]), _insert_ok("evt-new-2"), _delete_ok("evt-old-1")],
    )
    _write_watch(
        amc,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed(
                    "AFRM",
                    name="Affirm",
                    date="2026-11-20",
                    timing="BMO",
                    calendar_event_id="evt-old-1",
                )
            ],
            "pending": [],
            "reported": [],
        },
    )
    _record(module, amc, _record_body(earnings_date="2026-11-20", timing="AMC"), "amc.json")
    code, body = _run(module, _argv(amc, "apply"))
    assert code == 0
    assert [call["operation"] for call in calls] == ["list_events", "insert_event", "delete_event"]
    assert calls[1]["start"] == {"dateTime": "2026-11-20T16:00:00", "timeZone": "America/New_York"}
    assert calls[1]["end"] == {"dateTime": "2026-11-20T16:30:00", "timeZone": "America/New_York"}
    assert calls[2]["event_id"] == "evt-old-1"
    assert _read_watch(amc)["confirmed"][0]["calendar_event_id"] == "evt-new-2"
    assert _read_watch(amc)["confirmed"][0]["timing"] == "AMC"


def test_apply_keeps_new_event_when_delete_fails(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    calls = _prepare(
        module,
        [
            _list_ok(),
            _insert_ok("evt-new-1"),
            {"ok": False, "status": "upstream_error", "error": "delete-boom"},
        ],
    )
    _write_watch(
        workspace,
        {
            "updated": "2026-10-01",
            "confirmed": [
                _confirmed(
                    "AFRM",
                    name="Affirm",
                    date="2026-11-18",
                    timing="BMO",
                    calendar_event_id="evt-old-1",
                )
            ],
            "pending": [],
            "reported": [],
        },
    )
    _record(
        module,
        workspace,
        _record_body(earnings_date="2026-11-21", timing="AMC"),
        "move.json",
    )
    code, failed = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert failed["ok"] is False
    assert failed["applied"] == []
    assert failed["errors"] == ["delete-boom"]
    assert [call["operation"] for call in calls] == ["list_events", "insert_event", "delete_event"]
    saved = _read_watch(workspace)
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-new-1"
    assert saved["confirmed"][0]["date"] == "2026-11-21"
    assert saved["pending_delete_event_ids"] == ["evt-old-1"]

    calls = _prepare(module, [_delete_ok("evt-old-1")])
    code, retried = _run(module, _argv(workspace, "apply"))
    assert code == 0
    assert retried["ok"] is True
    assert retried["applied"] == []
    assert calls == [{"route": "gcal", "operation": "delete_event", "event_id": "evt-old-1"}]
    saved = _read_watch(workspace)
    assert saved["pending_delete_event_ids"] == []
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-new-1"


def test_apply_and_summary_surface_scan_failures(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    calls = _prepare(module, [])
    _write_watch(
        workspace,
        {
            "updated": "2026-10-01",
            "confirmed": [],
            "pending": [{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}],
            "reported": [],
        },
    )
    _record(
        module,
        workspace,
        _record_body(
            name="Affirm",
            status="scan_failed",
            earnings_date=None,
            timing=None,
            confidence=None,
            notes="search timeout",
        ),
        "failed.json",
    )
    code, applied = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert applied["ok"] is False
    assert applied["applied"] == []
    assert calls == []
    assert applied["errors"] == [{"ticker": "AFRM", "name": "Affirm", "error": "search timeout"}]
    code, text = _run(module, _argv(workspace, "summary"))
    assert code == 0
    assert "扫描失败：Affirm search timeout" in text["text"]
    assert "家已确认 /" not in text["text"]

    quiet = _workspace(tmp_path / "quiet-failed")
    _prepare(module)
    _record(
        module,
        quiet,
        _record_body(
            name="Affirm",
            status="scan_failed",
            earnings_date=None,
            timing=None,
            confidence=None,
            notes="search timeout",
        ),
        "failed.json",
    )
    apply_doc = {
        "union": 105,
        "confirmed": 40,
        "pending": 65,
        "applied": [],
        "skipped": [],
        "errors": [],
    }
    apply_path = quiet / "state" / "richard-earnings" / "scans" / SCAN_DAY / "apply.json"
    apply_path.write_text(json.dumps(apply_doc), encoding="utf-8")
    code, quiet_body = _run(module, _argv(quiet, "summary"))
    assert code == 0
    assert quiet_body["text"] != "40 家已确认 / 65 家未披露"
    assert "扫描失败：Affirm search timeout" in quiet_body["text"]
    assert quiet_body["text"].splitlines()[0] == "已核查 1/105"


def test_summary_lists_unfinished_after_the_gate(tmp_path: Path) -> None:
    module = _load()
    early = _workspace(tmp_path)
    _prepare(module)
    apply_doc = {
        "union": 105,
        "confirmed": 40,
        "pending": 65,
        "applied": [],
        "skipped": [],
        "errors": [],
    }
    early_apply = early / "state" / "richard-earnings" / "scans" / SCAN_DAY / "apply.json"
    early_apply.parent.mkdir(parents=True)
    early_apply.write_text(json.dumps(apply_doc), encoding="utf-8")
    started = early_apply.parent / "started.json"
    started.write_text(
        json.dumps({"started_at": (FIXED_NOW - timedelta(minutes=74)).isoformat(), "date": SCAN_DAY}),
        encoding="utf-8",
    )
    code, closed = _run(module, _argv(early, "summary"))
    assert code == 0
    assert closed["text"] == "40 家已确认 / 65 家未披露"

    opened = _workspace(tmp_path / "opened")
    opened_apply = opened / "state" / "richard-earnings" / "scans" / SCAN_DAY / "apply.json"
    opened_apply.parent.mkdir(parents=True)
    opened_apply.write_text(json.dumps(apply_doc), encoding="utf-8")
    (opened_apply.parent / "started.json").write_text(
        json.dumps({"started_at": (FIXED_NOW - timedelta(minutes=75)).isoformat(), "date": SCAN_DAY}),
        encoding="utf-8",
    )
    code, body = _run(module, _argv(opened, "summary"))
    assert code == 0
    assert body["text"] != "40 家已确认 / 65 家未披露"
    assert body["text"].splitlines()[0] == "已核查 0/105"
    assert "未完成：Affirm (AFRM)" in body["text"]
    assert "Apple (AAPL)" in body["text"]


def _watch(pending: list[dict[str, Any]] | None = None, confirmed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "updated": "2026-10-01",
        "confirmed": confirmed or [],
        "pending": pending or [],
        "reported": [],
    }


def _scan_file(workspace: Path, group: str = "A") -> Path:
    return workspace / "state" / "richard-earnings" / "scans" / SCAN_DAY / f"{group}.jsonl"


def _failed_row(**overrides: Any) -> dict[str, Any]:
    body = _record_body(
        name="Affirm",
        status="scan_failed",
        earnings_date=None,
        timing=None,
        confidence=None,
        notes="search timeout",
    )
    body.update(overrides)
    return body


def _quiet_row(**overrides: Any) -> dict[str, Any]:
    body = _record_body(
        name="Affirm",
        status="not_announced",
        earnings_date=None,
        timing=None,
        confidence=None,
    )
    body.update(overrides)
    return body


def test_pending_delete_id_is_not_reused_or_deleted_while_current(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)

    def at_bmo(event_id: str) -> dict[str, Any]:
        return _listed_event(
            event_id,
            "AFRM",
            {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"},
            {"dateTime": "2026-11-20T08:30:00", "timeZone": "America/New_York"},
        )

    def at_amc(event_id: str) -> dict[str, Any]:
        return _listed_event(
            event_id,
            "AFRM",
            {"dateTime": "2026-11-20T16:00:00", "timeZone": "America/New_York"},
            {"dateTime": "2026-11-20T16:30:00", "timeZone": "America/New_York"},
        )

    delete_boom = {"ok": False, "status": "upstream_error", "error": "delete-boom"}
    calls = _prepare(module, [_list_ok([at_bmo("evt-a")]), _insert_ok("evt-b"), delete_boom])
    _write_watch(
        workspace,
        _watch(confirmed=[_confirmed("AFRM", name="Affirm", date="2026-11-20", timing="BMO", calendar_event_id="evt-a")]),
    )
    _record(module, workspace, _record_body(name="Affirm", earnings_date="2026-11-20", timing="AMC"), "amc.json")
    code, failed = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert failed["ok"] is False
    assert failed["applied"] == []
    assert failed["errors"] == ["delete-boom"]
    saved = _read_watch(workspace)
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-b"
    assert saved["pending_delete_event_ids"] == ["evt-a"]
    assert [call["operation"] for call in calls] == ["list_events", "insert_event", "delete_event"]

    scanned = _scan_file(workspace)
    scanned.write_text(
        scanned.read_text(encoding="utf-8") + json.dumps(_record_body(name="Affirm", timing="BMO"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    calls = _prepare(
        module,
        [delete_boom, _list_ok([at_bmo("evt-a"), at_amc("evt-b")]), _insert_ok("evt-c"), _delete_ok("evt-b")],
    )
    code, changed = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert changed["ok"] is False
    assert changed["errors"] == ["delete-boom"]
    assert [call["operation"] for call in calls] == [
        "delete_event",
        "list_events",
        "insert_event",
        "delete_event",
    ]
    # Retry deletes evt-a only. evt-b was the ledger id and is removed after evt-c exists.
    assert [call["event_id"] for call in calls if call["operation"] == "delete_event"] == ["evt-a", "evt-b"]
    assert changed["applied"][0]["event_id"] == "evt-c"
    saved = _read_watch(workspace)
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-c"
    assert saved["confirmed"][0]["timing"] == "BMO"
    assert saved["pending_delete_event_ids"] == ["evt-a"]

    saved["pending"] = [{"ticker": "AMZN", "name": "Amazon", "note": "下一期", "calendar_event_id": "evt-amzn"}]
    saved["pending_delete_event_ids"] = ["evt-c", "evt-amzn", "evt-a"]
    _write_watch(workspace, saved)
    calls = _prepare(module, [_delete_ok("evt-a")])
    code, retried = _run(module, _argv(workspace, "apply"))
    assert code == 0
    assert retried["ok"] is True
    assert calls == [{"route": "gcal", "operation": "delete_event", "event_id": "evt-a"}]
    saved = _read_watch(workspace)
    assert saved["pending_delete_event_ids"] == []
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-c"
    assert saved["pending"][0]["calendar_event_id"] == "evt-amzn"


def test_slot_matches_compares_the_full_et_window() -> None:
    module = _load()

    def timed(start: str, end: str, zone: str | None = None) -> dict[str, Any]:
        start_value = {"dateTime": start}
        end_value = {"dateTime": end}
        if zone is not None:
            start_value["timeZone"] = zone
            end_value["timeZone"] = zone
        return {"start": start_value, "end": end_value}

    zone = "America/New_York"
    assert module._slot_matches(timed("2026-11-20T08:00:00", "2026-11-20T08:30:00", zone), "2026-11-20", "BMO")
    assert not module._slot_matches(timed("2026-11-20T08:00:00", "2026-11-21T08:30:00", zone), "2026-11-20", "BMO")
    assert not module._slot_matches(timed("2026-11-19T08:00:00", "2026-11-20T08:30:00", zone), "2026-11-20", "BMO")
    assert module._slot_matches(timed("2026-11-20T13:00:00Z", "2026-11-20T13:30:00Z"), "2026-11-20", "BMO")
    assert module._slot_matches(timed("2026-11-20T13:00:00+00:00", "2026-11-20T13:30:00+00:00"), "2026-11-20", "BMO")
    assert module._slot_matches(timed("2026-11-20T21:00:00Z", "2026-11-20T21:30:00Z"), "2026-11-20", "AMC")
    assert not module._slot_matches(timed("2026-11-20T16:00:00", "2026-11-20T17:00:00", zone), "2026-11-20", "AMC")
    assert not module._slot_matches(timed("2026-11-20T08:00:01", "2026-11-20T08:30:00", zone), "2026-11-20", "BMO")
    assert module._slot_matches(timed("2026-03-07T13:00:00Z", "2026-03-07T13:30:00Z"), "2026-03-07", "BMO")
    assert module._slot_matches(timed("2026-03-09T12:00:00Z", "2026-03-09T12:30:00Z"), "2026-03-09", "BMO")
    assert not module._slot_matches(timed("2026-03-09T08:00:00Z", "2026-03-09T08:30:00Z"), "2026-03-09", "BMO")
    assert module._slot_matches(timed("2026-10-31T08:00:00-04:00", "2026-10-31T08:30:00-04:00"), "2026-10-31", "BMO")
    assert module._slot_matches(timed("2026-11-02T08:00:00-05:00", "2026-11-02T08:30:00-05:00"), "2026-11-02", "BMO")
    assert not module._slot_matches(timed("2026-11-02T08:00:00-04:00", "2026-11-02T08:30:00-04:00"), "2026-11-02", "BMO")
    assert module._slot_matches(
        {"start": {"date": "2026-11-20"}, "end": {"date": "2026-11-21"}},
        "2026-11-20",
        "unknown",
    )
    assert not module._slot_matches(
        {"start": {"date": "2026-11-20"}, "end": {"date": "2026-11-20"}},
        "2026-11-20",
        "unknown",
    )
    assert not module._slot_matches(
        {"start": {"date": "2026-11-20"}, "end": {"date": "2026-11-22"}},
        "2026-11-20",
        "unknown",
    )
    assert not module._slot_matches(timed("2026-11-20T08:00:00", "2026-11-20T08:30:00", zone), "2026-11-20", "unknown")


def test_apply_reuses_full_window_and_rejects_cross_day_end(tmp_path: Path) -> None:
    module = _load()
    utc = _workspace(tmp_path)
    matching = _listed_event(
        "evt-match",
        "AFRM",
        {"dateTime": "2026-11-20T13:00:00Z"},
        {"dateTime": "2026-11-20T13:30:00Z"},
    )
    calls = _prepare(module, [_list_ok([matching])])
    _write_watch(utc, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    _record(module, utc, _record_body(name="Affirm"), "bmo.json")
    code, body = _run(module, _argv(utc, "apply"))
    assert code == 0
    assert body["ok"] is True
    assert [call["operation"] for call in calls] == ["list_events"]
    assert _read_watch(utc)["confirmed"][0]["calendar_event_id"] == "evt-match"

    cross = _workspace(tmp_path / "cross")
    overnight = _listed_event(
        "evt-cross",
        "AFRM",
        {"dateTime": "2026-11-20T08:00:00", "timeZone": "America/New_York"},
        {"dateTime": "2026-11-21T08:30:00", "timeZone": "America/New_York"},
    )
    calls = _prepare(module, [_list_ok([overnight]), _insert_ok("evt-new")])
    _write_watch(cross, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    _record(module, cross, _record_body(name="Affirm"), "bmo.json")
    code, body = _run(module, _argv(cross, "apply"))
    assert code == 0
    assert [call["operation"] for call in calls] == ["list_events", "insert_event"]
    assert _read_watch(cross)["confirmed"][0]["calendar_event_id"] == "evt-new"

    allday = _workspace(tmp_path / "allday")
    day_event = _listed_event(
        "evt-day",
        "AFRM",
        {"date": "2026-11-20"},
        {"date": "2026-11-21"},
    )
    calls = _prepare(module, [_list_ok([day_event])])
    _write_watch(allday, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    _record(module, allday, _record_body(name="Affirm", timing="unknown"), "day.json")
    code, body = _run(module, _argv(allday, "apply"))
    assert code == 0
    assert [call["operation"] for call in calls] == ["list_events"]
    assert _read_watch(allday)["confirmed"][0]["calendar_event_id"] == "evt-day"


def test_apply_ok_after_valid_record_covers_invalid_line(tmp_path: Path) -> None:
    module = _load()
    covered = _workspace(tmp_path)
    calls = _prepare(module, [])
    _write_watch(covered, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    day_dir = _scan_file(covered).parent
    day_dir.mkdir(parents=True)
    missing = {"ticker": "AFRM", "name": "Affirm", "status": "announced", "group": "A"}
    _scan_file(covered).write_text(json.dumps(missing, ensure_ascii=False) + "\n", encoding="utf-8")
    code, recorded = _record(module, covered, _quiet_row(), "again.json")
    assert code == 0
    assert recorded["status"] == "recorded"
    code, progress = _run(module, _argv(covered, "progress"))
    assert code == 0
    assert progress["invalid_count"] == 1
    assert progress["invalid_lines"][0]["ticker"] == "AFRM"
    assert progress["invalid_lines"][0]["error"] == "missing_fields"
    code, applied = _run(module, _argv(covered, "apply"))
    assert code == 0
    assert applied["ok"] is True
    assert applied["errors"] == []
    assert calls == []

    mixed = _workspace(tmp_path / "mixed")
    calls = _prepare(module, [])
    _write_watch(mixed, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    lines = [
        "{",
        json.dumps({"ticker": "AAPL", "name": "Apple", "status": "announced", "group": "B"}, ensure_ascii=False),
        json.dumps(_quiet_row(), ensure_ascii=False),
    ]
    path = _scan_file(mixed)
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    code, applied = _run(module, _argv(mixed, "apply"))
    assert code == 1
    assert applied["ok"] is False
    assert any(isinstance(item, str) and item.startswith("A.jsonl:1 ") for item in applied["errors"])
    assert {"file": "A.jsonl", "line": 2, "ticker": "AAPL", "error": "missing_fields"} in applied["errors"]
    assert all(not (isinstance(item, dict) and item.get("ticker") == "AFRM") for item in applied["errors"])
    assert calls == []

    uncovered = _workspace(tmp_path / "uncovered")
    _prepare(module, [])
    _write_watch(uncovered, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    raw = [
        json.dumps(missing, ensure_ascii=False),
        json.dumps(_failed_row(), ensure_ascii=False),
    ]
    failed_path = _scan_file(uncovered)
    failed_path.parent.mkdir(parents=True)
    failed_path.write_text("\n".join(raw) + "\n", encoding="utf-8")
    code, applied = _run(module, _argv(uncovered, "apply"))
    assert code == 1
    assert applied["ok"] is False
    assert {"file": "A.jsonl", "line": 1, "ticker": "AFRM", "error": "missing_fields"} in applied["errors"]
    assert {"ticker": "AFRM", "name": "Affirm", "error": "search timeout"} in applied["errors"]


def test_non_scalar_fields_do_not_crash_record_or_apply(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module, [])
    _write_watch(workspace, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    code, listed = _record(module, workspace, _record_body(confidence=["high"]), "list.json")
    assert code == 1
    assert listed["error"] == "invalid_field"
    assert listed["field"] == "confidence"
    code, obj = _record(module, workspace, _record_body(confidence={"level": "high"}), "obj.json")
    assert code == 1
    assert obj["error"] == "invalid_field"
    assert obj["field"] == "confidence"
    code, timing = _record(module, workspace, _record_body(timing={"slot": "BMO"}), "timing.json")
    assert code == 1
    assert timing["error"] == "invalid_field"
    assert timing["field"] == "timing"
    code, status = _record(module, workspace, _record_body(status=["announced"]), "status.json")
    assert code == 1
    assert status["error"] == "invalid_status"
    assert "unhashable" not in json.dumps([listed, obj, timing, status])
    assert not _scan_file(workspace).exists()

    _scan_file(workspace).parent.mkdir(parents=True)
    _scan_file(workspace).write_text(
        json.dumps(_record_body(confidence=["high"]), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    code, progress = _run(module, _argv(workspace, "progress"))
    assert code == 0
    assert progress["invalid_count"] == 1
    assert progress["invalid_lines"][0]["error"] == "invalid_field"
    code, applied = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert applied["error"] == "no scan records"
    assert applied["errors"][0]["error"] == "invalid_field"
    assert "TypeError" not in json.dumps(applied)
    code, summary = _run(module, _argv(workspace, "summary"))
    assert code == 0
    assert "text" in summary
    assert "TypeError" not in summary["text"]


def test_apply_retries_pending_deletes_without_scan_records(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    missing = {
        "ok": False,
        "status": "event_not_found",
        "error_code": "event_not_found",
        "error": "missing",
    }
    calls = _prepare(module, [missing])
    module.now_et = create_autospec(
        _ORIGINALS["now_et"],
        return_value=datetime(2026, 11, 15, 7, 0, tzinfo=ET),
    )
    _write_watch(
        workspace,
        _watch(
            confirmed=[_confirmed("AFRM", name="Affirm", calendar_event_id="evt-live")],
        ),
    )
    saved = _read_watch(workspace)
    saved["pending_delete_event_ids"] = ["evt-a"]
    _write_watch(workspace, saved)
    code, body = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert body["ok"] is False
    assert body["error"] == "no scan records"
    assert body["errors"] == []
    assert body["scan_date"] == "2026-11-15"
    assert calls == [{"route": "gcal", "operation": "delete_event", "event_id": "evt-a"}]
    saved = _read_watch(workspace)
    assert saved["pending_delete_event_ids"] == []
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-live"


def test_scan_failed_stays_queued_until_a_valid_record(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    _prepare(module)
    _write_watch(workspace, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    code, failed = _record(module, workspace, _failed_row(), "failed.json")
    assert code == 0
    assert failed["status"] == "recorded"
    code, progress = _run(module, _argv(workspace, "progress"))
    assert code == 0
    assert progress["unique"] == 0
    assert "AFRM" in {item["ticker"] for item in progress["remaining"]}
    code, recorded = _record(module, workspace, _quiet_row(), "ok.json")
    assert code == 0
    assert recorded["status"] == "recorded"
    code, progress = _run(module, _argv(workspace, "progress"))
    assert code == 0
    assert progress["unique"] == 1
    assert "AFRM" not in {item["ticker"] for item in progress["remaining"]}
    code, blocked = _record(module, workspace, _failed_row(notes="later outage"), "later.json")
    assert code == 0
    assert blocked["status"] == "already_recorded"
    calls = _prepare(module, [])
    code, applied = _run(module, _argv(workspace, "apply"))
    assert code == 0
    assert applied["ok"] is True
    assert applied["errors"] == []
    assert calls == []


def test_scan_failed_outside_watchlist_is_skipped(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    calls = _prepare(module, [])
    _write_watch(workspace, _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}]))
    _record(module, workspace, _failed_row(), "afrm.json")
    _record(
        module,
        workspace,
        _failed_row(ticker="AAPL", name="Apple", group="B", notes="offline"),
        "aapl.json",
    )
    code, applied = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert applied["ok"] is False
    assert applied["skipped"] == [{"ticker": "AAPL", "reason": "not in watchlist"}]
    assert applied["errors"] == [{"ticker": "AFRM", "name": "Affirm", "error": "search timeout"}]
    assert calls == []


def test_refused_delete_is_reported_once_and_not_retried(tmp_path: Path) -> None:
    module = _load()
    workspace = _workspace(tmp_path)
    refused = {"ok": False, "status": "refused_not_tracker_event", "error": "not ours"}
    calls = _prepare(module, [_list_ok(), _insert_ok("evt-new-1"), refused])
    _write_watch(
        workspace,
        _watch(
            confirmed=[
                _confirmed(
                    "AFRM",
                    name="Affirm",
                    date="2026-11-18",
                    timing="BMO",
                    calendar_event_id="evt-old-1",
                )
            ]
        ),
    )
    _record(module, workspace, _record_body(name="Affirm", earnings_date="2026-11-21", timing="AMC"), "move.json")
    code, failed = _run(module, _argv(workspace, "apply"))
    assert code == 1
    assert failed["ok"] is False
    assert failed["applied"] == []
    assert failed["errors"] == ["not ours"]
    assert [call["operation"] for call in calls] == ["list_events", "insert_event", "delete_event"]
    assert calls[2]["event_id"] == "evt-old-1"
    saved = _read_watch(workspace)
    assert saved["confirmed"][0]["calendar_event_id"] == "evt-new-1"
    assert saved.get("pending_delete_event_ids", []) == []
    calls = _prepare(module, [])
    code, again = _run(module, _argv(workspace, "apply"))
    assert code == 0
    assert again["ok"] is True
    assert calls == []
    assert _read_watch(workspace)["confirmed"][0]["calendar_event_id"] == "evt-new-1"

    queued = _workspace(tmp_path / "queued")
    refused_code = {"ok": False, "error_code": "refused_not_tracker_event", "error": "not ours"}
    calls = _prepare(module, [refused_code])
    payload = _watch(pending=[{"ticker": "AFRM", "name": "Affirm", "note": "下一期"}])
    payload["pending_delete_event_ids"] = ["evt-foreign"]
    _write_watch(queued, payload)
    _record(module, queued, _quiet_row(), "quiet.json")
    code, first = _run(module, _argv(queued, "apply"))
    assert code == 1
    assert first["errors"] == ["not ours"]
    assert calls == [{"route": "gcal", "operation": "delete_event", "event_id": "evt-foreign"}]
    assert _read_watch(queued)["pending_delete_event_ids"] == []
    calls = _prepare(module, [])
    code, second = _run(module, _argv(queued, "apply"))
    assert code == 0
    assert second["ok"] is True
    assert calls == []
    assert _read_watch(queued).get("pending_delete_event_ids", []) == []
