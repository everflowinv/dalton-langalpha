"""OC-1 Tencent Meeting weekly minutes: fetch, pending flag, and doc receipt."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest
import yaml

from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-tmeet-weekly-minutes"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "tmeet_minutes.py"
PROFILE = "tmeet-weekly-minutes"
MEETING_DATE = "2026-11-13"
SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXED_NOW = datetime(2026, 11, 13, 15, 0, tzinfo=SHANGHAI)
BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")
BANNED_RECIPIENTS = (
    "932169512197955636",
    "oc_397207208fb52a0b09969794ef3cd421",
    "ou_",
)
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
_ORIGINALS: dict[str, Any] = {}


def _load():
    name = "dalton_tmeet_weekly_under_test"
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
    _ORIGINALS["now_shanghai"] = module.now_shanghai
    return module


def _restore(module) -> None:
    module.call_research = _ORIGINALS["call_research"]
    module.now_shanghai = _ORIGINALS["now_shanghai"]


def _run(module, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, json.loads(buffer.getvalue())


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _bind_clock(module) -> None:
    module.now_shanghai = create_autospec(_ORIGINALS["now_shanghai"], return_value=FIXED_NOW)


def _bind_research(module, responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(responses)

    def _fake(**arguments: Any) -> dict[str, Any]:
        calls.append(arguments)
        if not queue:
            raise AssertionError("unexpected research call")
        return queue.pop(0)

    module.call_research = create_autospec(_ORIGINALS["call_research"], side_effect=_fake)
    return calls


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "status": "ok", "data": data}


def _search_records() -> dict[str, Any]:
    return _ok(
        {
            "records": [
                {
                    "record_file_id": "100",
                    "file_type": "transcript",
                    "subject": "其他会议",
                    "meeting_start_time": "2026-11-13T12:00:00+08:00",
                },
                {
                    "record_file_id": "200",
                    "file_type": "transcript",
                    "subject": "预定的会议",
                    "meeting_start_time": "2026-11-07T09:00:00+08:00",
                },
                {
                    "record_file_id": "300",
                    "file_type": "video",
                    "subject": "周会预定的会议",
                    "meeting_start_time": "2026-11-13T09:00:00+08:00",
                },
                {
                    "record_file_id": "400",
                    "file_type": "transcript",
                    "subject": "周会预定的会议",
                    "meeting_start_time": "2026-11-13T09:00:00+08:00",
                },
            ],
            "record_count": 4,
            "truncated": False,
            "pages_scanned": 1,
        }
    )


def _minutes(text: str) -> dict[str, Any]:
    return _ok({"action": "minutes", "record_file_id": "400", "minute": text, "todos": []})


def _todos() -> dict[str, Any]:
    return _ok(
        {
            "action": "todos",
            "record_file_id": "400",
            "todos": [{"content": "核对数字", "executors": ["甲"]}],
        }
    )


def _transcript_page(pid: str, text: str, *, more: bool, next_pid: str | None) -> dict[str, Any]:
    return _ok(
        {
            "action": "transcript",
            "record_file_id": "400",
            "more": more,
            "next_pid": next_pid,
            "paragraphs": [{"pid": pid, "sentences": [{"words": [{"text": text}]}]}],
        }
    )


def _fetch_argv(workspace: Path) -> list[str]:
    return ["--json", "fetch", "--workspace-dir", str(workspace)]


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
    assert description == "无人值守的腾讯会议周会纪要：只读拉取录制与转写，写成纪要并提交纯文本。"
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
    for sentence in (
        "严禁使用防御性否定表达",
        "先直接给出判断，再写事实、依据、适用条件和具体待核项。",
        "保存或投递成稿前逐句检查，发现上述措辞就整句改写",
        "逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样",
        "沿用原有子任务权限与限制。",
    ):
        assert sentence in rules
    assert rules.strip().endswith("沿用原有子任务权限与限制。")
    assert "gpt-6.1-sol-oauth" in body


def test_skill_tree_has_no_recipient_and_config_has_three_keys() -> None:
    seen = False
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        seen = True
        text = path.read_text(encoding="utf-8")
        for banned in BANNED_RECIPIENTS:
            assert banned not in text, path.name
    assert seen
    config = json.loads((SKILL_ROOT / "config.json").read_text(encoding="utf-8"))
    assert set(config) == {"meeting_subject_keyword", "lookback_days", "lang"}
    assert config["meeting_subject_keyword"] == "预定的会议"
    assert config["lookback_days"] == 7
    assert config["lang"] == "zh"


def test_fetch_selects_latest_transcript_and_pages(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    calls = _bind_research(
        module,
        [
            _search_records(),
            _minutes("纪要一句。"),
            _todos(),
            _transcript_page("0", "你好", more=True, next_pid="1"),
            _transcript_page("1", "结束", more=False, next_pid=None),
        ],
    )
    workspace = _workspace(tmp_path)
    code, body = _run(module, _fetch_argv(workspace))
    assert code == 0 and body["ok"] is True and body["found"] is True
    assert body["record_file_id"] == "400"
    assert body["file_type"] == "transcript"
    assert body["meeting_date"] == MEETING_DATE
    assert body["report_run_id"] == f"tmeet-weekly-minutes-{MEETING_DATE}"
    assert body["run_dir"] == f"state/tmeet-weekly-minutes/runs/{MEETING_DATE}"
    assert body["run_dir"].startswith("state/")
    assert "/Users/" not in json.dumps(body, ensure_ascii=False)
    assert body["minutes_empty"] is False
    assert body["transcript_empty"] is False
    assert body["pending_needed"] is False
    assert body["transcript_pages"] == 2
    assert body["todos_count"] == 1
    assert not (workspace / "state/tmeet-weekly-minutes/pending.flag").exists()
    assert calls[0] == {
        "route": "tmeet",
        "operation": "record_search",
        "start": "2026-11-06T15:00:00+08:00",
        "end": "2026-11-13T15:00:00+08:00",
        "query": "预定的会议",
        "limit": 100,
    }
    assert calls[1]["action"] == "minutes" and calls[1]["record_file_id"] == "400"
    assert calls[1]["lang"] == "zh"
    assert calls[2]["action"] == "todos" and calls[2]["record_file_id"] == "400"
    assert calls[3]["action"] == "transcript" and calls[3]["pid"] == "0" and calls[3]["limit"] == 100
    assert calls[4]["pid"] == "1" and calls[4]["record_file_id"] == "400"
    assert all(item["record_file_id"] != "300" for item in calls[1:])
    run = workspace / body["run_dir"]
    stored = json.loads((run / "transcript.json").read_text(encoding="utf-8"))
    assert stored["more"] is False
    assert [item["pid"] for item in stored["paragraphs"]] == ["0", "1"]
    assert (run / "minutes.json").is_file()
    assert (run / "todos.json").is_file()
    assert (run / "search.json").is_file()
    assert (run / "selected.json").is_file()
    assert (run / "manifest.json").is_file()
    selected = json.loads((run / "selected.json").read_text(encoding="utf-8"))
    assert selected["record_file_id"] == "400"


def test_fetch_both_empty_sets_pending_needed_without_flag(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    _bind_research(
        module,
        [
            _search_records(),
            _minutes("  "),
            _todos(),
            _transcript_page("0", "  ", more=False, next_pid=None),
        ],
    )
    workspace = _workspace(tmp_path)
    code, body = _run(module, _fetch_argv(workspace))
    assert code == 0 and body["ok"] is True and body["found"] is True
    assert body["minutes_empty"] is True
    assert body["transcript_empty"] is True
    assert body["pending_needed"] is True
    assert not (workspace / "state/tmeet-weekly-minutes/pending.flag").exists()


def test_fetch_stops_when_transcript_next_pid_missing(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    _bind_research(
        module,
        [
            _search_records(),
            _minutes("纪要一句。"),
            _todos(),
            _transcript_page("0", "你好", more=True, next_pid=None),
        ],
    )
    workspace = _workspace(tmp_path)
    code, body = _run(module, _fetch_argv(workspace))
    assert code == 1 and body["ok"] is False
    assert "下一页" in body["error"]
    run = workspace / "state/tmeet-weekly-minutes/runs" / MEETING_DATE
    assert not (run / "transcript.json").exists()
    assert not (run / "manifest.json").exists()
    assert not (workspace / "state/tmeet-weekly-minutes/pending.flag").exists()


def test_pending_write_check_delete(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    flag = workspace / "state/tmeet-weekly-minutes/pending.flag"
    code, missing = _run(
        module,
        ["--json", "pending", "--workspace-dir", str(workspace), "--op", "check"],
    )
    assert code == 0 and missing["exists"] is False
    code, written = _run(
        module,
        [
            "--json",
            "pending",
            "--workspace-dir",
            str(workspace),
            "--op",
            "write",
            "--meeting-date",
            MEETING_DATE,
            "--record-file-id",
            "900001",
        ],
    )
    assert code == 0 and written["ok"] is True and written["exists"] is True
    assert written["meeting_date"] == MEETING_DATE
    assert written["record_file_id"] == "900001"
    assert written["path"] == "state/tmeet-weekly-minutes/pending.flag"
    assert flag.is_file() and not flag.is_symlink()
    stored = json.loads(flag.read_text(encoding="utf-8"))
    assert set(stored) == {"meeting_date", "record_file_id", "written_at"}
    code, found = _run(
        module,
        ["--json", "pending", "--workspace-dir", str(workspace), "--op", "check"],
    )
    assert code == 0 and found["exists"] is True and found["record_file_id"] == "900001"
    code, deleted = _run(
        module,
        ["--json", "pending", "--workspace-dir", str(workspace), "--op", "delete"],
    )
    assert code == 0 and deleted["deleted"] is True and deleted["exists"] is False
    assert not flag.exists()


def test_existing_doc_is_not_recreated(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    workspace = _workspace(tmp_path)
    relative = f"state/tmeet-weekly-minutes/runs/{MEETING_DATE}"
    raw = json.dumps(
        {
            "url": "https://feishu.cn/docx/Abcd1234efgh",
            "node_token": "WikiToken1",
            "document_id": "DocxToken1",
        },
        ensure_ascii=False,
    ).encode()
    dest = workspace / relative / "doc.json"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(raw)
    code, status = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
        ],
    )
    assert code == 0 and status["create"] is False
    assert status["url"] == "https://feishu.cn/docx/Abcd1234efgh"
    other = workspace / relative / "feishu-return.json"
    other.write_bytes(json.dumps({"document_id": "Zyxw9876vuts", "node_token": "WikiToken1"}).encode())
    code, skipped = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
            "--file",
            f"{relative}/feishu-return.json",
        ],
    )
    assert code == 0 and skipped["ok"] is True
    assert skipped["skipped"] is True and skipped["create"] is False
    assert skipped["url"] == "https://feishu.cn/docx/Abcd1234efgh"
    assert dest.read_bytes() == raw
    assert (
        module.feishu_doc_url({"url": "https://example.invalid/x", "node_token": "WikiToken1"})
        == "https://feishu.cn/wiki/WikiToken1"
    )


def test_unreadable_doc_record_is_not_overwritten(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    workspace = _workspace(tmp_path)
    relative = f"state/tmeet-weekly-minutes/runs/{MEETING_DATE}"
    dest = workspace / relative / "doc.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{", encoding="utf-8")
    code, status = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
        ],
    )
    assert code == 0 and status["create"] is False
    assert status["reason"] == "doc_record_unreadable"
    (workspace / relative / "feishu-return.json").write_bytes(
        json.dumps({"url": "https://feishu.cn/docx/Abcd1234efgh"}).encode()
    )
    code, saved = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
            "--file",
            f"{relative}/feishu-return.json",
        ],
    )
    assert code == 0 and saved["reason"] == "doc_record_unreadable"
    assert saved["create"] is False
    assert dest.read_text(encoding="utf-8") == "{"


def test_two_doc_failures_stop_create(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    workspace = _workspace(tmp_path)
    relative = f"state/tmeet-weekly-minutes/runs/{MEETING_DATE}"
    first = workspace / relative / "return-1.json"
    second = workspace / relative / "return-2.json"
    first.parent.mkdir(parents=True)
    first.write_bytes(b'{"ok": false}')
    second.write_bytes(b'{"ok": false, "error": "again"}')
    code, once = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
            "--file",
            f"{relative}/return-1.json",
        ],
    )
    assert code == 0 and once["ok"] is True
    assert once["create"] is True and once["attempts"] == 1
    assert not (workspace / relative / "doc.json").exists()
    code, twice = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
            "--file",
            f"{relative}/return-2.json",
        ],
    )
    assert code == 0 and twice["create"] is False
    assert twice["reason"] == "create_failed_twice"
    assert twice["attempts"] == 2
    assert not (workspace / relative / "doc.json").exists()
    code, status = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
        ],
    )
    assert code == 0 and status["create"] is False
    assert status["reason"] == "create_failed_twice"


def test_paths_do_not_escape(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    workspace = _workspace(tmp_path)
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        module.workspace_file(workspace, "../secret.json")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        module.workspace_file(workspace, "/etc/passwd")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "state")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        module.workspace_file(workspace, "state/tmeet-weekly-minutes/pending.flag")
    code, escaped = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
            "--file",
            "../secret.json",
        ],
    )
    assert code == 1 and escaped["ok"] is False
    assert not (tmp_path / "secret.json").exists()
    assert not (outside / "secret.json").exists()
    assert "/Users/" not in json.dumps(escaped, ensure_ascii=False)

    workspace2 = tmp_path / "ws2"
    workspace2.mkdir()
    (workspace2 / "real.json").write_text("{}", encoding="utf-8")
    os.symlink(workspace2 / "real.json", workspace2 / "link.json")
    with pytest.raises(ValueError, match="refusing symlink"):
        module.workspace_file(workspace2, "link.json")
    link_ws = tmp_path / "link-ws"
    os.symlink(workspace2, link_ws)
    code, linked = _run(
        module,
        ["--json", "pending", "--workspace-dir", str(link_ws), "--op", "check"],
    )
    assert code == 1 and linked["ok"] is False
    assert "path must stay inside the workspace" in linked["error"]
    assert "/Users/" not in linked["error"]


def test_failure_copy_and_group_summary_stay_in_skill() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    listed = text.split("## 失败文案", 1)[1].split("## 7. 兜底", 1)[0]
    for sentence in (
        "腾讯会议周会纪要失败：需要在宿主机重新登录 tmeet",
        "腾讯会议周会纪要失败：回看窗口内没有匹配的周会录制",
        "腾讯会议周会纪要失败：智能纪要和转写仍未生成",
        "腾讯会议周会纪要失败：宿主机 tmeet 输出不再是约定结构",
        "腾讯会议周会纪要失败：飞书文档未建成",
        "腾讯会议周会纪要失败：纪要文档已创建但正文没写进去",
        "腾讯会议周会纪要失败：转写分页超过 50 页，已停止",
        "腾讯会议周会纪要失败：转写 pid 不连续或有重复",
        "腾讯会议周会纪要失败：待重拉的会议标记已损坏",
        "腾讯会议周会纪要失败：拉取或保存没有完成",
    ):
        assert listed.count(sentence) == 1
    assert "failure_text` 只用对应原句" in listed
    assert 'material="yes"' in listed
    assert "tmeet-weekly-minutes-<会议日期>" in listed
    for phrase in (
        "feishu_read",
        "max_chars=50000",
        "total_chars",
        "10%",
        "参会人",
        "speaker.user_name",
        "一屏以内",
        "美元/USD",
        "fetch --from-pending",
        "pending_unreadable",
        "tmeet-weekly-minutes-<上海当天日期>",
    ):
        assert phrase in text
    assert "开头和文末" not in text
    assert "加大 `max_chars` 再读" not in text
    positions = (SKILL_ROOT / "references" / "positions-performance.md").read_text(encoding="utf-8")
    assert "pid 从 0 起连续、无重复" in positions
    assert "超过 50 页" in positions
    assert "已完整获取" in positions
    assert "已核对相关原文" in positions


def test_fetch_from_pending_keeps_the_flagged_meeting(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    calls: list[dict[str, Any]] = []

    def _fake(**arguments: Any) -> dict[str, Any]:
        calls.append(dict(arguments))
        if arguments.get("operation") == "record_search":
            return _ok(
                {
                    "records": [
                        {
                            "record_file_id": "800",
                            "file_type": "transcript",
                            "subject": "预定的会议",
                            "meeting_start_time": "2026-11-13T10:00:00+08:00",
                        }
                    ],
                    "record_count": 1,
                    "truncated": False,
                    "pages_scanned": 1,
                }
            )
        assert arguments.get("record_file_id") == "200"
        action = arguments.get("action")
        if action == "minutes":
            return _ok({"action": "minutes", "record_file_id": "200", "minute": "原会议纪要。", "todos": []})
        if action == "todos":
            return _ok({"action": "todos", "record_file_id": "200", "todos": []})
        if action == "transcript":
            return _transcript_page("0", "原会议", more=False, next_pid=None)
        raise AssertionError(action)

    module.call_research = create_autospec(_ORIGINALS["call_research"], side_effect=_fake)
    workspace = _workspace(tmp_path)
    code, written = _run(
        module,
        [
            "--json",
            "pending",
            "--workspace-dir",
            str(workspace),
            "--op",
            "write",
            "--meeting-date",
            "2026-11-07",
            "--record-file-id",
            "200",
        ],
    )
    assert code == 0 and written["meeting_date"] == "2026-11-07"
    module.write_json(
        workspace,
        "state/tmeet-weekly-minutes/runs/2026-11-07/selected.json",
        {"record_file_id": "800", "subject": "更新的预定的会议"},
    )
    code, body = _run(
        module,
        ["--json", "fetch", "--workspace-dir", str(workspace), "--from-pending"],
    )
    assert code == 0 and body["ok"] is True and body["found"] is True
    assert body["meeting_date"] == "2026-11-07"
    assert body["report_run_id"] == "tmeet-weekly-minutes-2026-11-07"
    assert body["record_file_id"] == "200"
    assert all(item.get("operation") != "record_search" for item in calls)
    assert {item.get("record_file_id") for item in calls} == {"200"}
    run = workspace / "state/tmeet-weekly-minutes/runs/2026-11-07"
    assert not (run / "search.json").exists()
    selected = json.loads((run / "selected.json").read_text(encoding="utf-8"))
    assert selected["record_file_id"] == "200"


def test_fetch_without_recording_uses_shanghai_today(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    _bind_research(
        module,
        [
            _ok(
                {
                    "records": [
                        {
                            "record_file_id": "100",
                            "file_type": "transcript",
                            "subject": "其他会议",
                            "meeting_start_time": "2026-11-13T12:00:00+08:00",
                        }
                    ],
                    "record_count": 1,
                    "truncated": False,
                    "pages_scanned": 1,
                }
            )
        ],
    )
    workspace = _workspace(tmp_path)
    code, body = _run(module, _fetch_argv(workspace))
    assert code == 0 and body["ok"] is True and body["found"] is False
    assert body["meeting_date"] is None
    assert body["report_run_id"] == "tmeet-weekly-minutes-2026-11-13"


def test_content_insert_failed_is_stored_and_not_recreated(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    workspace = _workspace(tmp_path)
    relative = f"state/tmeet-weekly-minutes/runs/{MEETING_DATE}"
    payload = {
        "ok": False,
        "error": "content_insert_failed",
        "document_id": "DocxToken1",
        "url": "https://feishu.cn/docx/DocxToken1",
    }
    raw = json.dumps(payload, ensure_ascii=False).encode()
    source = workspace / relative / "feishu-return.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    code, saved = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
            "--file",
            f"{relative}/feishu-return.json",
        ],
    )
    assert code == 0 and saved["ok"] is True
    assert saved["create"] is False
    assert saved["content_complete"] is False
    assert saved["reason"] == "content_incomplete"
    assert saved["error"] == "content_insert_failed"
    assert saved["url"] == "https://feishu.cn/docx/DocxToken1"
    assert (workspace / relative / "doc.json").read_bytes() == raw
    assert not (workspace / relative / "doc.failed.json").exists()
    code, status = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--meeting-date",
            MEETING_DATE,
        ],
    )
    assert code == 0 and status["create"] is False
    assert status["content_complete"] is False
    assert status["reason"] == "content_incomplete"
    assert status["error"] == "content_insert_failed"
    assert status["url"] == "https://feishu.cn/docx/DocxToken1"


def _broken_transcript(paragraphs: list[dict[str, Any]]) -> dict[str, Any]:
    return _ok(
        {
            "action": "transcript",
            "record_file_id": "400",
            "more": False,
            "next_pid": None,
            "paragraphs": paragraphs,
        }
    )


def test_fetch_rejects_duplicate_or_gapped_pids(tmp_path: Path) -> None:
    pages = [
        [
            {"pid": "0", "sentences": [{"words": [{"text": "甲"}]}]},
            {"pid": "0", "sentences": [{"words": [{"text": "甲"}]}]},
        ],
        [
            {"pid": "0", "sentences": [{"words": [{"text": "甲"}]}]},
            {"pid": "2", "sentences": [{"words": [{"text": "乙"}]}]},
        ],
    ]
    for index, paragraphs in enumerate(pages):
        module = _load()
        _restore(module)
        _bind_clock(module)
        _bind_research(
            module,
            [
                _search_records(),
                _minutes("纪要一句。"),
                _todos(),
                _broken_transcript(paragraphs),
            ],
        )
        case_root = tmp_path / f"case{index}"
        case_root.mkdir()
        workspace = _workspace(case_root)
        code, body = _run(module, _fetch_argv(workspace))
        assert code == 1 and body["ok"] is False
        assert body["status"] == "transcript_pid_invalid"
        assert body["error"] == "转写 pid 不连续或有重复"
        assert body["status"] != "cli_contract_changed"
        run = workspace / "state/tmeet-weekly-minutes/runs" / MEETING_DATE
        assert not (run / "transcript.json").exists()
        assert not (run / "manifest.json").exists()
        assert not (run / "minutes.json").exists()


def test_fetch_stops_when_transcript_exceeds_page_cap(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    assert module.TRANSCRIPT_PAGE_CAP == 50
    _bind_research(
        module,
        [
            _search_records(),
            _minutes("纪要一句。"),
            _todos(),
            _transcript_page("0", "甲", more=True, next_pid="1"),
        ],
    )
    module.TRANSCRIPT_PAGE_CAP = 1
    workspace = _workspace(tmp_path)
    try:
        code, body = _run(module, _fetch_argv(workspace))
    finally:
        module.TRANSCRIPT_PAGE_CAP = 50
    assert code == 1 and body["ok"] is False
    assert body["status"] == "transcript_too_long"
    assert body["status"] != "cli_contract_changed"
    assert body["error"] == "转写分页超过 50 页，已停止"
    run = workspace / "state/tmeet-weekly-minutes/runs" / MEETING_DATE
    assert not (run / "transcript.json").exists()
    assert not (run / "manifest.json").exists()
    assert module.TRANSCRIPT_PAGE_CAP == 50


def test_unreadable_pending_flag_is_not_a_missing_flag(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)

    def _refuse(**arguments: Any) -> dict[str, Any]:
        raise AssertionError(arguments)

    module.call_research = create_autospec(_ORIGINALS["call_research"], side_effect=_refuse)
    cases = (
        ("{", "tmeet-weekly-minutes-2026-11-13"),
        ("[1]", "tmeet-weekly-minutes-2026-11-13"),
        (
            json.dumps({"meeting_date": "2026-02-30", "record_file_id": "200"}),
            "tmeet-weekly-minutes-2026-11-13",
        ),
        (
            json.dumps({"meeting_date": "2026-11-07", "record_file_id": "abc"}),
            "tmeet-weekly-minutes-2026-11-07",
        ),
    )
    for index, (text, run_id) in enumerate(cases):
        workspace = tmp_path / f"flag-{index}"
        workspace.mkdir()
        flag = workspace / "state/tmeet-weekly-minutes/pending.flag"
        flag.parent.mkdir(parents=True)
        flag.write_text(text, encoding="utf-8")
        code, checked = _run(
            module,
            ["--json", "pending", "--workspace-dir", str(workspace), "--op", "check"],
        )
        assert code == 0 and checked["ok"] is True
        assert checked["exists"] is True
        assert checked["readable"] is False
        code, fetched = _run(
            module,
            ["--json", "fetch", "--workspace-dir", str(workspace), "--from-pending"],
        )
        assert code == 1 and fetched["ok"] is False
        assert fetched["status"] == "pending_unreadable"
        assert fetched["report_run_id"] == run_id
        assert flag.is_file()
    with pytest.raises(ValueError, match="meeting_date"):
        module._require_date("2026-02-30")
    assert module._require_date("2026-02-28") == "2026-02-28"
