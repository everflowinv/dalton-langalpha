"""OC-1 ToB industry weekly: collect facts, then submit plain text."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "plugins" / "dalton" / "skills"
SKILL_NAME = "dalton-tob-industry-weekly"
PROFILE = "tob-industry-weekly"
BANNED = (
    "discord",
    "feishu_send_card",
    ".openclaw",
    "openclaw_workspace",
    "xreach",
    "chat_id",
)
_RECIPIENT_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
CARD = """# 💼 China & Global ToB Tracker — 2026-11-13
## 本周总览
本周财报淡季，无池内公司发业绩
## 业绩
本周无 A股/港股池内公司发业绩
本周无美股软件池内公司发业绩
## 管理层
本周无新增管理层重点发言
## 卖方
本周无显著预测修正信号
## 下周
下周无池内重点公司发业绩
"""
REPORT = CARD + "\n## 信源\n公司披露\n## 口径\n文本口径\n## 免责\n内部研究参考\n"
CONFIRM = ["--content-kind", "text", "--delivery-confirmed", "--status", "sent"]
FRIDAY = "2026-11-13"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _cli():
    return _load(
        "tob_industry_weekly_agentturn",
        SKILLS / SKILL_NAME / "scripts" / "tob_industry_weekly.py",
    )


def _cninfo():
    return _load(
        "cninfo_a_scan_agentturn",
        SKILLS / SKILL_NAME / "scripts" / "cninfo_a_scan.py",
    )


def _run(module, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    text = buffer.getvalue()
    return code, json.loads(text) if text.strip() else {}


def _run_captured(module, argv: list[str]) -> tuple[int, dict, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = module.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    text = stdout.getvalue()
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": text}
    return code, payload, stderr.getvalue()


def _bash_commands(text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
        tokens: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                stripped = stripped[:-1].strip()
            tokens.extend(stripped.split())
        current: list[str] = []
        for token in tokens:
            if token == "python3" and current:
                commands.append(current)
                current = [token]
            else:
                current.append(token)
        if current:
            commands.append(current)
    return commands


def _documented(subcommand: str, replacements: dict[str, str]) -> list[tuple[str, list[str]]]:
    text = (SKILLS / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    found: list[tuple[str, list[str]]] = []
    for command in _bash_commands(text):
        if subcommand not in command:
            continue
        script_at = next(index for index, token in enumerate(command) if token.endswith(".py"))
        argv: list[str] = []
        for token in command[script_at + 1 :]:
            token = token.strip("'\"")
            for old, new in replacements.items():
                token = token.replace(old, new)
            argv.append(token)
        found.append((command[script_at], argv))
    if not found:
        raise AssertionError(f"{SKILL_NAME} has no {subcommand} command")
    return found


def _note(workspace: Path, *extra: str) -> list[str]:
    return [
        "--json", "add-note", "--workspace-dir", str(workspace),
        "--ticker", "MSFT", "--company", "微软", "--market", "US",
        "--event-type", "earnings", "--direction", "beat",
        "--source-type", "company_disclosure",
        *extra,
    ]


def test_skill_frontmatter_and_registry() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        DALTON_SKILL_DEFINITIONS,
    )

    text = (SKILLS / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    assert "触发词" not in meta["description"]
    assert len(meta["description"]) <= 120
    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].requires == ()
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].command is None
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].description == meta["description"].strip()
    assert PROFILE in REPORT_PROFILES
    assert "--since <source-start>" in text
    assert "周一到周四写入的记录也要进正文" in text
    assert "started_at" in text
    assert "历史状态由主助手预先导入" in text
    assert "failure_targets" in text


def test_skill_tree_omits_delivery_targets() -> None:
    root = SKILLS / SKILL_NAME
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert _RECIPIENT_OC_RE.search(text) is None, path
        for token in BANNED:
            assert token not in text, f"{path} contains {token}"


def test_documented_commands_run_offline(tmp_path: Path) -> None:
    cli = _cli()
    workspace = tmp_path / "tob"
    workspace.mkdir()
    replacements = {
        "<workspace>": str(workspace),
        "<as-of>": "2026-11-15",
        "<friday>": FRIDAY,
        "<source-start>": "2026-11-06",
    }
    self_tests = _documented("self-test", replacements)
    assert self_tests[0][0].endswith("tob_industry_weekly.py")
    assert self_tests[1][0].endswith("cninfo_a_scan.py")
    self_argv = self_tests[0][1]
    assert self_argv[:4] == ["--json", "self-test", "--workspace-dir", str(workspace)]
    assert "--network" in self_argv
    code, payload, err = _run_captured(cli, [arg for arg in self_argv if arg != "--network"])
    assert code == 0 and payload["ok"] is True, f"stderr={err!r} payload={payload!r}"
    code, payload, err = _run_captured(_cninfo(), self_tests[1][1])
    assert code == 0 and payload["ok"] is True, f"stderr={err!r} payload={payload!r}"

    anchor_argv = _documented("anchor", replacements)[0][1]
    assert anchor_argv[:4] == ["--json", "anchor", "--workspace-dir", str(workspace)]
    code, payload = _run(cli, anchor_argv)
    assert code == 0 and payload["friday"] == FRIDAY
    assert payload["report_run_id"] == f"{PROFILE}-{FRIDAY}"
    code, thursday = _run(
        cli,
        ["--json", "anchor", "--workspace-dir", str(workspace), "--as-of", "2026-11-12"],
    )
    assert code == 0 and thursday["friday"] == "2026-11-06"
    assert thursday["report_run_id"] == f"{PROFILE}-2026-11-06"

    card = workspace / "temp" / PROFILE / f"{FRIDAY}-card.md"
    card.parent.mkdir(parents=True)
    card.write_text(CARD, encoding="utf-8")
    (card.parent / f"{FRIDAY}.md").write_text(REPORT, encoding="utf-8")
    validates = _documented("validate", replacements)
    assert validates[0][1][:4] == ["--json", "validate", "--workspace-dir", str(workspace)]
    assert validates[0][1][validates[0][1].index("--profile") + 1] == "card"
    assert validates[1][1][validates[1][1].index("--profile") + 1] == "report"
    code, payload = _run(cli, validates[0][1])
    assert code == 0 and payload["ok"] is True, payload
    code, payload = _run(cli, validates[1][1])
    assert code == 0 and payload["ok"] is True, payload

    started_argv = _documented("start-collection", replacements)[0][1]
    assert started_argv[:4] == ["--json", "start-collection", "--workspace-dir", str(workspace)]
    code, started = _run(cli, started_argv)
    assert code == 0 and started["ok"] is True and started["kept"] is False, started
    recorded_argv = _documented("record-collection", replacements)[0][1]
    code, recorded = _run(cli, recorded_argv)
    assert code == 0 and recorded["ok"] is True and recorded["mode"] == "collect"
    assert recorded["started_at"] == started["started_at"]
    assert recorded["finished_at"]
    run_file = workspace / "state" / PROFILE / "collection-runs" / f"{FRIDAY}.json"
    assert run_file.is_file()
    assert "recorded_at" not in json.loads(run_file.read_text(encoding="utf-8"))
    assert not (workspace / "state" / PROFILE / "snapshots").exists()


def test_archive_and_snapshot_wait_for_delivery(tmp_path: Path) -> None:
    cli = _cli()
    workspace = tmp_path / "tob"
    workspace.mkdir()
    source = workspace / "temp" / PROFILE / f"{FRIDAY}.md"
    source.parent.mkdir(parents=True)
    source.write_text(REPORT, encoding="utf-8")
    code, refused = _run(
        cli,
        [
            "--json", "archive", "--workspace-dir", str(workspace),
            "--file", f"temp/{PROFILE}/{FRIDAY}.md", "--as-of", FRIDAY,
        ],
    )
    assert code == 1 and refused["error"] == "未确认发送" and refused["written"] is None
    code, refused = _run(
        cli,
        ["--json", "snapshot", "--workspace-dir", str(workspace), "--as-of", FRIDAY, "--write"],
    )
    assert code == 1 and refused["error"] == "未确认发送" and refused["written"] is None
    assert not (workspace / "research-outputs").exists()
    assert not (workspace / "state" / PROFILE / "snapshots").exists()

    replacements = {
        "<workspace>": str(workspace),
        "<friday>": FRIDAY,
        "<as-of>": FRIDAY,
    }
    code, archived = _run(cli, _documented("archive", replacements)[0][1])
    assert code == 0
    assert archived["written"] == f"research-outputs/tob-industry-tracker/{FRIDAY}.md"
    assert not (workspace / "state" / PROFILE / "snapshots" / f"{FRIDAY}.json").exists()
    code, snap = _run(cli, _documented("snapshot", replacements)[0][1])
    assert code == 0
    assert snap["written"] == f"state/{PROFILE}/snapshots/{FRIDAY}.json"
    assert (workspace / snap["written"]).is_file()


def test_paths_do_not_escape_or_follow_symlinks(tmp_path: Path) -> None:
    cli = _cli()
    workspace = tmp_path / "tob"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    note = _note(
        workspace,
        "--evidence-grade", "A",
        "--claim", "营收同比增加",
        "--source-date", FRIDAY,
        "--week-of", FRIDAY,
    )
    os.symlink(outside, workspace / "state")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        cli._card_io().workspace_file(workspace, f"state/{PROFILE}/notes.jsonl")
    code, payload = _run(cli, note)
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(outside.iterdir()) == []
    (workspace / "state").unlink()

    folder = workspace / "state" / PROFILE
    folder.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("sentinel", encoding="utf-8")
    os.symlink(sentinel, folder / "notes.jsonl")
    code, payload = _run(cli, note)
    assert code == 1 and "refusing symlink" in payload["error"]
    assert sentinel.read_text(encoding="utf-8") == "sentinel"

    code, escaped = _run(
        cli,
        [
            "--json", "archive", "--workspace-dir", str(workspace),
            "--file", "../secret.md", "--as-of", FRIDAY, *CONFIRM,
        ],
    )
    assert code == 1 and "path must stay inside the workspace" in escaped["error"]
    assert not (tmp_path / "secret.md").exists()

    linked = tmp_path / "linked-workspace"
    os.symlink(workspace, linked)
    code, payload = _run(cli, ["--json", "anchor", "--workspace-dir", str(linked)])
    assert code == 1 and "symlink" in payload["error"]


def test_network_probe_uses_autospec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli()
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "import socket" not in source
    assert "urllib" not in source
    probe = create_autospec(cli._network_probe, return_value={"detail": "offline"})
    monkeypatch.setattr(cli, "_network_probe", probe)
    workspace = tmp_path / "tob"
    workspace.mkdir()
    code, payload = _run(
        cli, ["--json", "self-test", "--workspace-dir", str(workspace)]
    )
    assert code == 0 and payload["ok"] is True, payload
    assert probe.call_count == 0
    code, payload = _run(
        cli, ["--json", "self-test", "--workspace-dir", str(workspace), "--network"]
    )
    assert code == 0 and payload["ok"] is True, payload
    probe.assert_called_once_with()


def test_screen_uses_utc8_and_closed_interval(tmp_path: Path) -> None:
    cninfo = _cninfo()
    assert cninfo.cn_date(1786671000000) == "2026-08-14"
    checks = {item["check"]: item for item in cninfo.offline_checks()}
    assert checks["cn_date_utc8"]["ok"] is True
    assert checks["sedate_closed_interval"]["ok"] is True
    assert checks["universe_a_shares"]["ok"] is True

    workspace = tmp_path / "tob"
    filings = workspace / "state" / PROFILE / "a-filings.json"
    filings.parent.mkdir(parents=True)
    companies = {
        row["ticker"].split(".")[0]: [{"ok": True, "rows": []}]
        for row in cninfo.a_share_rows()
    }
    companies["600588"] = [
        {
            "ok": True,
            "rows": [
                {
                    "ticker": "600588",
                    "title": "2026年半年度报告",
                    "announcementTime": 1786671000000,
                    "adjunctUrl": "finalpage/demo.pdf",
                },
                {
                    "ticker": "600588",
                    "title": "2026年半年度报告",
                    "date": "2026-08-15",
                },
                {
                    "ticker": "600588",
                    "title": "2026年半年度报告摘要",
                    "date": "2026-08-14",
                },
            ],
        }
    ]
    filings.write_text(json.dumps({"companies": companies}), encoding="utf-8")
    argv = _documented(
        "screen",
        {
            "<workspace>": str(workspace),
            "<source-start>": "2026-08-14",
            "<friday>": "2026-08-14",
        },
    )[0][1]
    assert argv[:4] == ["--json", "screen", "--workspace-dir", str(workspace)]
    code, payload = _run(cninfo, argv)
    assert code == 0 and payload["ok"] is True, payload
    assert payload["incomplete"] is False
    assert payload["scanned"] == 56
    assert payload["errors"] == [] and payload["missing"] == []
    assert payload["closed_interval"] is True
    hits = payload["companies"]["600588.SH"]["hits"]
    assert payload["hit_companies"] == 1 and len(hits) == 1
    assert hits[0]["date"] == "2026-08-14"
    assert payload["dropped_outside_window"] == 1
    assert (workspace / "state" / PROFILE / "a-scan.json").is_file()


def test_screen_merges_calls_and_rejects_bad_shapes(tmp_path: Path) -> None:
    cninfo = _cninfo()
    pool = [row["ticker"] for row in cninfo.a_share_rows()]
    merged = "600588.SH"
    mixed = next(ticker for ticker in pool if ticker != merged)
    false_only = next(ticker for ticker in pool if ticker not in {merged, mixed})
    empty = next(ticker for ticker in pool if ticker not in {merged, mixed, false_only})
    same = {
        "title": "2026年半年度报告",
        "date": "2026-08-14",
        "url": "http://static.cninfo.com.cn/finalpage/a.pdf",
    }
    companies = {
        ticker.split(".")[0]: [{"ok": True, "rows": []}]
        for ticker in pool
        if ticker not in {mixed, false_only, empty}
    }
    companies["600588"] = [
        {
            "ok": True,
            "rows": [
                same,
                {"title": "2026年半年度报告", "date": "2026-08-15", "url": "http://static.cninfo.com.cn/b.pdf"},
            ],
        },
        {
            "ok": True,
            "data": [
                same,
                {"title": "2026年第三季度报告", "date": "2026-08-14", "url": "http://static.cninfo.com.cn/c.pdf"},
            ],
        },
    ]
    companies[mixed.split(".")[0]] = [
        {"ok": True, "rows": [{"title": "2026年半年度报告", "date": "2026-08-14", "url": "http://x/a"}]},
        {"ok": False, "error": "timeout"},
    ]
    companies[false_only.split(".")[0]] = [{"ok": False}]
    companies[empty.split(".")[0]] = []
    workspace = tmp_path / "tob"
    filings = workspace / "state" / PROFILE / "a-filings.json"
    filings.parent.mkdir(parents=True)
    filings.write_text(json.dumps({"companies": companies}), encoding="utf-8")
    argv = _documented(
        "screen",
        {
            "<workspace>": str(workspace),
            "<source-start>": "2026-08-14",
            "<friday>": "2026-08-14",
        },
    )[0][1]
    code, payload = _run(cninfo, argv)
    assert code == 0 and payload["ok"] is False, payload
    assert payload["incomplete"] is True
    hits = payload["companies"]["600588.SH"]["hits"]
    assert {item["title"] for item in hits} == {"2026年半年度报告", "2026年第三季度报告"}
    assert len(hits) == 2
    assert payload["dropped_outside_window"] == 1
    assert payload["errors"] == sorted([mixed, false_only])
    assert payload["error_details"][mixed] == "timeout"
    assert payload["error_details"][false_only] == "ok 不是 true"
    assert payload["companies"].get(mixed, {}).get("hits", []) == []
    assert payload["missing"] == [empty]
    assert payload["scanned"] == 53

    listed = cninfo.screen_rows([{"ok": False}], "2026-08-14", "2026-08-14")
    assert listed["ok"] is False and "格式" in listed["error"]
    assert listed["errors"] == [] and listed["missing"] == []
    assert listed["scanned"] == 0


def test_plan_windows_and_notes_since(tmp_path: Path) -> None:
    cli = _cli()
    workspace = tmp_path / "tob"
    workspace.mkdir()
    replacements = {
        "<workspace>": str(workspace),
        "<friday>": FRIDAY,
        "<as-of>": FRIDAY,
        "<source-start>": "2026-11-06",
        "<a-date>": "2026-11-12",
        "<hk-date>": "2026-11-12",
        "<us-date>": "2026-11-12",
    }
    state = workspace / "state" / PROFILE
    state.mkdir(parents=True)
    (state / "sessions.json").write_text(
        json.dumps({
            "weeks": {
                FRIDAY: {"markets": {"A": "2026-11-04", "HK": "2026-11-04", "US": "2026-11-04"}},
            },
        }),
        encoding="utf-8",
    )
    code, plan = _run(cli, _documented("plan", replacements)[0][1])
    assert code == 0 and plan["ok"] is True, plan
    assert "window" not in plan
    assert plan["price_window"] is None
    assert plan["price_window_missing"] is True
    assert plan["price_as_of"] is None
    assert plan["source_window"]["start"] == "2026-11-06"
    assert plan["source_window"]["end_at"].endswith("+08:00")
    assert "2026-11-04" in plan["holiday_note"]
    assert "2026-11-06" in plan["holiday_note"]
    assert "本期不输出业绩窗口" in plan["holiday_note"]
    assert plan["missing_count"] == 0

    code, perf = _run(cli, _documented("perf", replacements)[0][1])
    assert code == 0 and perf["ok"] is True, perf
    assert perf["requested_as_of"] == FRIDAY
    assert perf["price_as_of"] is None
    assert perf["as_of"] == FRIDAY
    assert perf["price_window"] is None
    assert "本期不输出业绩窗口" in perf["holiday_note"]

    (state / "sessions.json").write_text(
        json.dumps({
            "markets": {"A": "2026-11-01", "HK": "2026-11-01", "US": "2026-11-01"},
            "weeks": {
                "2026-11-06": {
                    "markets": {"A": "2026-11-06", "HK": "2026-11-06", "US": "2026-11-06"},
                },
            },
        }),
        encoding="utf-8",
    )
    code, partial = _run(
        cli,
        [
            "--json", "record-sessions", "--workspace-dir", str(workspace),
            "--as-of", FRIDAY, "--a-date", "2026-11-12",
        ],
    )
    assert code == 0 and partial["markets"] == {"A": "2026-11-12"}, partial
    stored = json.loads((state / "sessions.json").read_text(encoding="utf-8"))
    assert "markets" not in stored
    assert stored["weeks"]["2026-11-06"]["markets"]["HK"] == "2026-11-06"
    assert stored["weeks"][FRIDAY]["markets"] == {"A": "2026-11-12"}
    code, plan = _run(cli, _documented("plan", replacements)[0][1])
    assert plan["missing_count"] == 2
    assert plan["missing_markets"] == ["HK", "US"]
    assert plan["price_as_of"] is None
    assert plan["price_window"]["end"] == FRIDAY
    assert plan["price_window_missing"] is False

    code, aligned = _run(cli, _documented("record-sessions", replacements)[0][1])
    assert code == 0 and aligned["markets"]["US"] == "2026-11-12", aligned
    code, plan = _run(cli, _documented("plan", replacements)[0][1])
    assert plan["price_window"]["start"] == "2026-11-06"
    assert plan["price_window"]["end"] == "2026-11-12"
    assert plan["price_as_of"] == "2026-11-12"
    assert plan["price_window_missing"] is False
    assert plan["missing_count"] == 0
    assert plan["holiday_note"]
    assert "本期不输出业绩窗口" not in plan["holiday_note"]
    code, perf = _run(cli, _documented("perf", replacements)[0][1])
    assert perf["price_as_of"] == "2026-11-12"
    assert perf["as_of"] == perf["price_as_of"]

    samples = [
        ("2026-11-01", "2026-11-13", "周次入窗"),
        ("2026-11-10", "2026-11-01", "来源入窗"),
        ("2026-11-01", "2026-11-01", "窗口外"),
        ("2026-11-06", "2026-11-06", "上周五入窗"),
        ("2026-11-05", "2026-10-30", "上周五前"),
    ]
    for source_date, week_of, claim in samples:
        code, added = _run(
            cli,
            _note(
                workspace,
                "--evidence-grade", "A",
                "--claim", claim,
                "--source-date", source_date,
                "--week-of", week_of,
            ),
        )
        assert code == 0 and added["ok"] is True, added
    code, listed = _run(cli, _documented("notes", replacements)[0][1])
    assert code == 0 and listed["count"] == 3
    assert {row["claim"] for row in listed["notes"]} == {"周次入窗", "来源入窗", "上周五入窗"}


def test_collection_keeps_earliest_start(tmp_path: Path) -> None:
    cli = _cli()
    workspace = tmp_path / "tob"
    workspace.mkdir()
    replacements = {"<workspace>": str(workspace), "<friday>": FRIDAY}
    finish = _documented("record-collection", replacements)[0][1]
    code, missing = _run(cli, finish)
    assert code == 1 and "start-collection" in missing["hint"]
    start = _documented("start-collection", replacements)[0][1]
    code, first = _run(cli, start)
    assert code == 0 and first["kept"] is False, first
    run_file = workspace / "state" / PROFILE / "collection-runs" / f"{FRIDAY}.json"
    body = json.loads(run_file.read_text(encoding="utf-8"))
    body["started_at"] = "2026-11-13T14:00:00+08:00"
    run_file.write_text(json.dumps(body), encoding="utf-8")
    code, again = _run(cli, start)
    assert code == 0 and again["kept"] is True, again
    assert again["started_at"] == "2026-11-13T14:00:00+08:00"
    code, done = _run(cli, finish)
    assert code == 0 and done["started_at"] == "2026-11-13T14:00:00+08:00", done
    assert done["finished_at"]
    saved = json.loads(run_file.read_text(encoding="utf-8"))
    assert saved["started_at"] == "2026-11-13T14:00:00+08:00"
    assert saved["finished_at"] == done["finished_at"]
    assert "recorded_at" not in saved


def test_notes_reject_weak_evidence_and_card_length(tmp_path: Path) -> None:
    cli = _cli()
    workspace = tmp_path / "tob"
    workspace.mkdir()
    code, rejected = _run(
        cli,
        _note(
            workspace,
            "--evidence-grade", "D",
            "--claim", "转述",
            "--source-date", FRIDAY,
        ),
    )
    assert code == 1 and "D 级" in rejected["error"]
    code, rejected = _run(
        cli,
        _note(
            workspace,
            "--evidence-grade", "B",
            "--claim", "口头数字",
            "--source-date", FRIDAY,
        ),
    )
    assert code == 1 and "cross-check" in rejected["error"]
    code, rejected = _run(
        cli,
        _note(
            workspace,
            "--evidence-grade", "A",
            "--claim", "逐字过长",
            "--source-date", FRIDAY,
            "--verbatim", " ".join(["alpha"] * 21),
        ),
    )
    assert code == 1 and "20" in rejected["error"]
    code, rejected = _run(
        cli,
        [
            "--json", "add-note", "--workspace-dir", str(workspace),
            "--ticker", "MSFT", "--company", "微软", "--market", "US",
            "--event-type", "management", "--evidence-grade", "A",
            "--direction", "neutral", "--source-type", "earnings_call",
            "--claim", "云需求放缓", "--source-date", FRIDAY,
        ],
    )
    assert code == 1 and "source-attribution" in rejected["error"]

    long_rel = f"temp/{PROFILE}/long-card.md"
    long_path = workspace / long_rel
    long_path.parent.mkdir(parents=True)
    long_path.write_text(CARD + ("甲" * 10001), encoding="utf-8")
    code, payload = _run(
        cli,
        [
            "--json", "validate", "--workspace-dir", str(workspace),
            "--file", long_rel, "--profile", "card",
        ],
    )
    assert code == 0 and payload["ok"] is False
    assert any(item["type"] == "card_too_long" for item in payload["problems"])


def test_call_rows_read_the_probe_envelopes() -> None:
    cninfo = _cninfo()
    row = {"title": "2026年半年度报告", "date": "2026-08-14", "url": "http://x/a"}
    for call in (
        {"ok": True, "filings": [row]},
        {"ok": True, "items": [row]},
        {"ok": True, "results": [row]},
        {"ok": True, "data": {"rows": [row]}},
        {"ok": True, "data": {"filings": [row]}},
    ):
        assert cninfo._call_rows(call) == [row], call
        assert cninfo._call_failure(call) is None, call
    assert cninfo._call_failure({"ok": True, "data": {"ok": False, "error": "upstream down"}}) == "upstream down"
    assert cninfo._call_failure({"ok": True, "data": {"isError": True}}) == "data.ok 是 false"
