"""Discord wiki auto-ingest: fetch, cursor, download, and quiet summary."""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import sys
import urllib.error
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
SKILL_NAME = "dalton-wiki-auto-ingest"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "wiki_ingest.py"
PROFILE = "wiki-auto-ingest"
DESCRIPTION = "无人值守的 Discord wiki（知识库）资料入库：按频道与发送者策略读取新消息，写入公司 wiki。"
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
FIXED_NOW = datetime(2026, 11, 13, 0, 30, tzinfo=HONG_KONG)
CHANNEL = "100000000000000001"
CHANNEL_B = "100000000000000009"
SENDER = "200000000000000002"
OTHER = "300000000000000003"
BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")
BANNED_RECIPIENTS = (
    "932169512197955636",
    "oc_397207208fb52a0b09969794ef3cd421",
    "ou_",
)
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
_ORIGINALS: dict[str, Any] = {}
SIGNED = "SIGNEDTOKENVALUE"


def _load():
    name = "dalton_wiki_auto_ingest_under_test"
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
    _ORIGINALS["now_hk"] = module.now_hk
    _ORIGINALS["_urlopen"] = module._urlopen
    return module


def _restore(module) -> None:
    module.call_research = _ORIGINALS["call_research"]
    module.now_hk = _ORIGINALS["now_hk"]
    module._urlopen = _ORIGINALS["_urlopen"]


def _run(module, argv: list[str]) -> tuple[int, dict[str, Any]]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, json.loads(buffer.getvalue())


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _bind_clock(module) -> None:
    module.now_hk = create_autospec(_ORIGINALS["now_hk"], return_value=FIXED_NOW)


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


class _Body:
    def __init__(
        self,
        data: bytes,
        *,
        headers: dict[str, str] | None = None,
        final: str | None = None,
    ) -> None:
        self._data = data
        self._off = 0
        self.headers = headers or {}
        self.final = final
        self.read_count = 0

    def geturl(self) -> str:
        return self.final or ""

    def read(self, n: int = -1) -> bytes:
        self.read_count += 1
        if self._off >= len(self._data):
            return b""
        if n is None or n < 0:
            n = len(self._data) - self._off
        chunk = self._data[self._off : self._off + n]
        self._off += len(chunk)
        return chunk

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _bind_urlopen(module, responder) -> list[tuple[Any, float]]:
    calls: list[tuple[Any, float]] = []

    def _fake(req: Any, timeout: float) -> _Body:
        calls.append((req, timeout))
        return responder(req, timeout)

    module._urlopen = create_autospec(_ORIGINALS["_urlopen"], side_effect=_fake)
    return calls


def _blank_result() -> dict[str, Any]:
    return {
        "scanned": 0,
        "ingested": 0,
        "skipped": 0,
        "pending": 0,
        "errors": [],
        "outputs": [],
    }


def _write_state(workspace: Path, channel: str, **overrides: Any) -> Path:
    payload: dict[str, Any] = {
        "channel_id": channel,
        "last_processed_message_id": "10",
        "pending_messages": [],
        "batch_ids": [],
        "run_marks": {},
        "fetch_errors": [],
        "last_run": None,
        "last_result": _blank_result(),
    }
    payload.update(overrides)
    path = workspace / "state" / "wiki-auto-ingest" / f"{channel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def _read_state(workspace: Path, channel: str) -> dict[str, Any]:
    path = workspace / "state" / "wiki-auto-ingest" / f"{channel}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _msg(
    message_id: str,
    author_id: str,
    *,
    content: str = "这是一段足够长的研究资料正文用来入库判断",
    bot: bool = False,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "author_id": author_id,
        "author_is_bot": bot,
        "timestamp": "2026-11-13T01:00:00+08:00",
        "content": content,
        "attachments": attachments or [],
        "embeds": [],
    }


def _page(messages: list[dict[str, Any]], has_more: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "ok",
        "data": {
            "messages": messages,
            "has_more": has_more,
            "message_count": len(messages),
        },
    }


def _fetch_argv(workspace: Path, channel: str, *extra: str) -> list[str]:
    return [
        "--json",
        "fetch",
        "--workspace-dir",
        str(workspace),
        "--channel-id",
        channel,
        *extra,
    ]


def _mark_argv(workspace: Path, channel: str, message_id: str, status: str, *extra: str) -> list[str]:
    return [
        "--json",
        "mark",
        "--workspace-dir",
        str(workspace),
        "--channel-id",
        channel,
        "--message-id",
        message_id,
        "--status",
        status,
        *extra,
    ]


def _write_snapshot(
    workspace: Path,
    channel: str,
    message_id: str,
    attachments: list[dict[str, Any]],
) -> None:
    path = (
        workspace
        / "state"
        / "wiki-auto-ingest"
        / channel
        / "messages"
        / f"{message_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _msg(message_id, SENDER, content="附件", attachments=attachments)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _http_gone(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url,
        code,
        f"HTTP {code}",
        email.message.EmailMessage(),
        io.BytesIO(b"secret-body"),
    )


def _download_argv(workspace: Path, channel: str, message_id: str) -> list[str]:
    return [
        "--json",
        "download",
        "--workspace-dir",
        str(workspace),
        "--channel-id",
        channel,
        "--message-id",
        message_id,
    ]


def _pending(message_id: str, error: str, outputs: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": message_id,
        "status": "pending",
        "source": "ingest",
        "error": error,
        "outputs": outputs or [],
        "author_id": SENDER,
        "author_is_bot": False,
        "timestamp": "2026-11-12T00:00:00+08:00",
        "content": "早先留下的研究资料正文，归属还要补",
        "attachments": [],
        "embeds": [],
    }


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
    for sentence in (
        "严禁使用防御性否定表达",
        "先直接给出判断，再写事实、依据、适用条件和具体待核项。",
        "保存或投递成稿前逐句检查，发现上述措辞就整句改写",
        "逐字引文、原始转写、代码、命令、原始报错及固定协议字段保持原样",
        "沿用原有子任务权限与限制。",
    ):
        assert sentence in rules
    assert rules.strip().endswith("沿用原有子任务权限与限制。")
    assert "不对 Discord 消息加表情" in body
    assert "NO_REPLY" in body
    assert "feishu_read" in body
    assert "failure_text" in body
    assert "最多 100 页" in body
    assert "continue_after" in body
    assert "本轮未读完，下次继续" in body
    assert "文档超过单次读取上限，需要人工处理" in body
    assert "oversize_docs" in body
    assert "confirm=True" in body
    assert "duplicate_suspected" in body
    assert "needs_alias" in body
    assert "suspected_sell_side" in body
    assert "20 页" not in body
    assert (
        "其余 `ok` 为 false，或没有写入回执（没有 `document_id` 或 `doc_key`）的状态"
        "（含 `rate_limited`、`rate_limit_unavailable`、`bridge_unreachable`、`secret_detected`）"
        "一律 `mark --status pending`，`--error` 写返回的 `status` 与 `error`。"
    ) in body


def test_skill_tree_has_no_recipient() -> None:
    seen = False
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        seen = True
        text = path.read_text(encoding="utf-8")
        for banned in BANNED_RECIPIENTS:
            assert banned not in text, path.name
    assert seen


def test_fetch_pages_with_all_non_bot(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    _write_state(workspace, CHANNEL, lead_import={"from": "openclaw"})
    calls = _bind_research(
        module,
        [
            _page(
                [
                    _msg("30", OTHER),
                    _msg("20", SENDER),
                    _msg("12", SENDER, bot=True),
                ],
                True,
            ),
            _page([_msg("40", SENDER)], False),
        ],
    )
    code, body = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert body["ok"] is True
    assert [item["id"] for item in body["messages"]] == ["20", "30", "40"]
    assert all(item["queue"] == "new" for item in body["messages"])
    assert body["new_count"] == 3
    assert body["pending_count"] == 0
    assert body["last_processed_message_id"] == "12"
    assert len(calls) == 2
    for call in calls:
        assert call["route"] == "discord-history"
        assert call["operation"] == "list_messages"
        assert call["channel_id"] == CHANNEL
        assert call["limit"] == 100
    assert calls[0]["after"] == "10"
    assert calls[1]["after"] == "30"
    state = _read_state(workspace, CHANNEL)
    assert state["lead_import"] == {"from": "openclaw"}
    assert state["batch_ids"] == ["12", "20", "30", "40"]
    assert state["run_marks"]["12"]["reason"] == "bot"
    assert all(isinstance(item, str) for item in state["batch_ids"])
    assert body["report_run_id"] == f"wiki-auto-ingest-{CHANNEL}-2026-11-13"


def test_fetch_sender_filter_skips_bots_and_others(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    _write_state(workspace, CHANNEL)
    _bind_research(
        module,
        [
            _page(
                [
                    _msg("11", OTHER),
                    _msg("12", SENDER, bot=True),
                    _msg("13", SENDER),
                ],
                False,
            )
        ],
    )
    code, body = _run(module, _fetch_argv(workspace, CHANNEL, "--sender-id", SENDER))
    assert code == 0
    assert [item["id"] for item in body["messages"]] == ["13"]
    assert body["messages"][0]["queue"] == "new"
    assert body["last_processed_message_id"] == "12"
    state = _read_state(workspace, CHANNEL)
    assert state["run_marks"]["11"]["reason"] == "sender_filter"
    assert state["run_marks"]["12"]["reason"] == "bot"
    assert "13" not in state["run_marks"]
    assert state["sender_policy"] == {"mode": "sender", "sender_id": SENDER}


def test_pending_is_listed_once_and_does_not_pull_the_cursor(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    _write_state(
        workspace,
        CHANNEL,
        pending_messages=[_pending("5", "归属不清", ["wiki:OLD"])],
    )
    _bind_research(
        module,
        [_page([_msg("11", SENDER), _msg("5", SENDER, content="早先留下的研究资料正文，归属还要补")], False)],
    )
    code, body = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert [item["id"] for item in body["messages"]] == ["5", "11"]
    assert body["messages"][0]["queue"] == "pending"
    assert body["messages"][0]["error"] == "归属不清"
    assert body["messages"][0]["outputs"] == ["wiki:OLD"]
    assert body["messages"][1]["queue"] == "new"
    assert body["messages"].count(body["messages"][0]) == 1
    assert body["last_processed_message_id"] == "10"
    assert _read_state(workspace, CHANNEL)["batch_ids"] == ["5", "11"]

    code, marked_new = _run(module, _mark_argv(workspace, CHANNEL, "11", "ingested", "--output", "wiki:NEW"))
    assert code == 0
    assert marked_new["last_processed_message_id"] == "10"
    code, marked_old = _run(module, _mark_argv(workspace, CHANNEL, "5", "skipped", "--reason", "existing"))
    assert code == 0
    assert marked_old["last_processed_message_id"] == "11"

    _write_state(
        workspace,
        CHANNEL_B,
        last_processed_message_id="100",
        pending_messages=[_pending("5", "归属不清")],
    )
    _bind_research(module, [_page([_msg("110", SENDER)], False)])
    code, later = _run(module, _fetch_argv(workspace, CHANNEL_B, "--all-non-bot"))
    assert code == 0
    assert [item["id"] for item in later["messages"]] == ["5", "110"]
    assert later["last_processed_message_id"] == "100"
    assert _read_state(workspace, CHANNEL_B)["batch_ids"] == ["110"]
    code, advanced = _run(
        module, _mark_argv(workspace, CHANNEL_B, "110", "ingested", "--output", "wiki:B")
    )
    assert code == 0
    assert advanced["last_processed_message_id"] == "110"
    kept = _read_state(workspace, CHANNEL_B)
    assert [item["id"] for item in kept["pending_messages"]] == ["5"]
    assert kept["last_processed_message_id"] == "110"


def test_snowflake_ids_compare_as_integers(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    path = _write_state(workspace, CHANNEL, last_processed_message_id="50")
    _bind_research(module, [_page([_msg("100", SENDER), _msg("99", SENDER)], False)])
    code, body = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert body["last_processed_message_id"] == "50"
    state = _read_state(workspace, CHANNEL)
    assert state["batch_ids"] == ["99", "100"]
    assert all(isinstance(item, str) for item in state["batch_ids"])
    raw = path.read_text(encoding="utf-8")
    assert '"99"' in raw
    assert '"100"' in raw

    code, first = _run(module, _mark_argv(workspace, CHANNEL, "100", "ingested", "--output", "wiki:100"))
    assert code == 0
    assert first["last_processed_message_id"] == "50"
    code, second = _run(module, _mark_argv(workspace, CHANNEL, "99", "ingested", "--output", "wiki:99"))
    assert code == 0
    assert second["last_processed_message_id"] == "100"
    saved = path.read_text(encoding="utf-8")
    assert '"last_processed_message_id": "100"' in saved
    loaded = json.loads(saved)
    assert isinstance(loaded["last_processed_message_id"], str)
    assert isinstance(loaded["batch_ids"][0], str)


def test_mark_contiguous_prefix_and_remark(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    _write_state(workspace, CHANNEL)
    _bind_research(
        module,
        [_page([_msg("400", SENDER), _msg("200", SENDER), _msg("300", SENDER)], False)],
    )
    code, _fetched = _run(module, _fetch_argv(workspace, CHANNEL, "--sender-id", SENDER))
    assert code == 0
    code, first = _run(module, _mark_argv(workspace, CHANNEL, "200", "ingested", "--output", "wiki:A"))
    assert code == 0
    assert first["last_processed_message_id"] == "200"
    assert first["last_result"]["ingested"] == 1
    code, again = _run(module, _mark_argv(workspace, CHANNEL, "200", "ingested", "--output", "wiki:A"))
    assert code == 0
    assert again["last_result"]["ingested"] == 1
    assert again["last_processed_message_id"] == "200"
    code, jumped = _run(module, _mark_argv(workspace, CHANNEL, "400", "ingested", "--output", "wiki:C"))
    assert code == 0
    assert jumped["last_processed_message_id"] == "200"
    assert jumped["last_result"]["ingested"] == 2
    code, filled = _run(module, _mark_argv(workspace, CHANNEL, "300", "ingested", "--output", "wiki:B"))
    assert code == 0
    assert filled["last_processed_message_id"] == "400"
    assert filled["last_result"]["ingested"] == 3
    code, waiting = _run(
        module,
        _mark_argv(workspace, CHANNEL, "400", "pending", "--error", "归属不清", "--output", "wiki:PART"),
    )
    assert code == 0
    assert waiting["last_processed_message_id"] == "400"
    assert waiting["last_result"]["pending"] == 1
    assert waiting["last_result"]["ingested"] == 2
    assert "归属不清" in waiting["last_result"]["errors"]
    code, done = _run(module, _mark_argv(workspace, CHANNEL, "400", "ingested", "--output", "wiki:C"))
    assert code == 0
    assert done["last_result"]["pending"] == 0
    assert done["last_result"]["ingested"] == 3
    assert "归属不清" not in done["last_result"]["errors"]
    assert done["last_result"]["outputs"] == ["wiki:A", "wiki:C", "wiki:B"]


def test_download_rejects_escape_oversize_and_offhost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    assert module.MAX_ATTACHMENT_BYTES == 50 * 1024 * 1024

    opened: list[tuple[str, _Body]] = []

    def _filename(url: str) -> str:
        return url.split("?", 1)[0].rsplit("/", 1)[-1]

    def _responder(req: Any, timeout: float) -> _Body:
        name = _filename(req.full_url)
        if name == "huge.pdf":
            body = _Body(b"", headers={"Content-Length": str(50 * 1024 * 1024 + 1)}, final=req.full_url)
        elif name == "bad.pdf":
            body = _Body(b"secret-body", final=f"https://evil.example/bad.pdf?ex={SIGNED}")
        elif name == "ok.pdf":
            body = _Body(b"ok-bytes", final="https://media.discordapp.net/ok.pdf")
        elif name == "a.pdf":
            body = _Body(b"AAA", final=req.full_url)
        elif name == "b.pdf":
            body = _Body(b"BBB", final=req.full_url)
        else:
            body = _Body(b"stream-too-long-body", final=req.full_url)
        opened.append((name, body))
        return body

    calls = _bind_urlopen(module, _responder)

    _write_snapshot(
        workspace,
        CHANNEL,
        "1",
        [
            {"id": "1", "filename": "../escape.txt", "content_type": "text/plain", "size": 1, "url": "https://cdn.discordapp.com/escape.txt"},
            {"id": "2", "filename": "a/b.txt", "content_type": "text/plain", "size": 1, "url": "https://cdn.discordapp.com/a/b.txt"},
        ],
    )
    code, escaped = _run(
        module,
        ["--json", "download", "--workspace-dir", str(workspace), "--channel-id", CHANNEL, "--message-id", "1"],
    )
    assert code == 1
    assert escaped["ok"] is False
    assert escaped["files"] == []
    assert [item["error"] for item in escaped["errors"]] == ["文件名不安全", "文件名不安全"]
    assert calls == []
    assert not (workspace / "escape.txt").exists()

    before = len(calls)
    _write_snapshot(
        workspace,
        CHANNEL,
        "2",
        [
            {"id": "3", "filename": "http.pdf", "content_type": "application/pdf", "size": 1, "url": "http://cdn.discordapp.com/http.pdf"},
            {"id": "4", "filename": "evil.pdf", "content_type": "application/pdf", "size": 1, "url": f"https://evil.example/evil.pdf?ex={SIGNED}"},
        ],
    )
    code, offhost = _run(
        module,
        ["--json", "download", "--workspace-dir", str(workspace), "--channel-id", CHANNEL, "--message-id", "2"],
    )
    assert code == 1
    assert offhost["files"] == []
    assert calls[before:] == []
    dumped_offhost = json.dumps(offhost, ensure_ascii=False)
    assert SIGNED not in dumped_offhost
    assert "evil.example" not in dumped_offhost

    _write_snapshot(
        workspace,
        CHANNEL,
        "3",
        [
            {"id": "5", "filename": "ok.pdf", "content_type": "application/pdf", "size": 3, "url": f"https://cdn.discordapp.com/ok.pdf?ex={SIGNED}"},
            {"id": "6", "filename": "huge.pdf", "content_type": "application/pdf", "size": 1, "url": f"https://cdn.discordapp.com/huge.pdf?ex={SIGNED}"},
            {"id": "7", "filename": "bad.pdf", "content_type": "application/pdf", "size": 1, "url": f"https://cdn.discordapp.com/bad.pdf?ex={SIGNED}"},
        ],
    )
    code, mixed = _run(
        module,
        ["--json", "download", "--workspace-dir", str(workspace), "--channel-id", CHANNEL, "--message-id", "3"],
    )
    assert code == 1
    assert mixed["ok"] is False
    assert mixed["files"] == [f"state/wiki-auto-ingest/{CHANNEL}/files/3/ok.pdf"]
    written = workspace / mixed["files"][0]
    assert written.read_bytes() == b"ok-bytes"
    assert written.is_relative_to(workspace)
    huge = next(item for item in mixed["errors"] if item["id"] == "6")
    bad = next(item for item in mixed["errors"] if item["id"] == "7")
    assert huge["error"] == "附件超过 50MB"
    assert bad["error"] == "附件地址不在 Discord CDN"
    dumped_mixed = json.dumps(mixed, ensure_ascii=False)
    assert SIGNED not in dumped_mixed
    assert "evil.example" not in dumped_mixed
    assert calls
    assert all(timeout == module.DOWNLOAD_TIMEOUT_S for _req, timeout in calls)
    huge_call = next(req for req, _timeout in calls if _filename(req.full_url) == "huge.pdf")
    assert huge_call.get_header("User-agent") == "DaltonWikiIngest/1.0"
    assert next(body for name, body in opened if name == "huge.pdf").read_count == 0
    assert next(body for name, body in opened if name == "bad.pdf").read_count == 0
    saved_names = [
        path.name
        for path in (workspace / "state" / "wiki-auto-ingest" / CHANNEL / "files" / "3").iterdir()
    ]
    assert saved_names == ["ok.pdf"]

    _write_snapshot(
        workspace,
        CHANNEL,
        "4",
        [
            {"id": "8", "filename": "note.pdf", "content_type": "application/pdf", "size": 3, "url": "https://cdn.discordapp.com/a.pdf"},
            {"id": "9", "filename": "note.pdf", "content_type": "application/pdf", "size": 3, "url": "https://media.discordapp.net/b.pdf"},
        ],
    )
    code, named = _run(
        module,
        ["--json", "download", "--workspace-dir", str(workspace), "--channel-id", CHANNEL, "--message-id", "4"],
    )
    assert code == 0
    assert named["ok"] is True
    assert named["files"] == [
        f"state/wiki-auto-ingest/{CHANNEL}/files/4/note.pdf",
        f"state/wiki-auto-ingest/{CHANNEL}/files/4/9-note.pdf",
    ]
    assert (workspace / named["files"][0]).read_bytes() == b"AAA"
    assert (workspace / named["files"][1]).read_bytes() == b"BBB"

    monkeypatch.setattr(module, "MAX_ATTACHMENT_BYTES", 8)
    _write_snapshot(
        workspace,
        CHANNEL,
        "5",
        [
            {"id": "10", "filename": "stream.bin", "content_type": "application/octet-stream", "size": 1, "url": "https://cdn.discordapp.com/stream.bin"},
        ],
    )
    code, streamed = _run(
        module,
        ["--json", "download", "--workspace-dir", str(workspace), "--channel-id", CHANNEL, "--message-id", "5"],
    )
    assert code == 1
    assert streamed["files"] == []
    assert streamed["errors"] == [{"id": "10", "error": "附件超过 50MB"}]
    assert next(body for name, body in opened if name == "stream.bin").read_count == 1
    assert not (workspace / "state" / "wiki-auto-ingest" / CHANNEL / "files" / "5").exists()


def test_summary_quiet_and_report_date(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    _write_state(workspace, CHANNEL)
    _bind_research(module, [_page([_msg("11", SENDER, bot=True), _msg("12", OTHER, bot=True)], False)])
    code, fetched = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert fetched["messages"] == []
    code, quiet = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert code == 0
    assert quiet["quiet"] is True
    assert quiet["text"] == "这一轮没有新的入库，也没有待处理失败。"
    assert quiet["report_run_id"] == f"wiki-auto-ingest-{CHANNEL}-2026-11-13"
    assert _read_state(workspace, CHANNEL)["last_result"]["skipped"] == 2

    _write_state(
        workspace,
        CHANNEL_B,
        last_processed_message_id="100",
        pending_messages=[_pending("5", "归属不清")],
    )
    _bind_research(module, [_page([], False)])
    code, carried = _run(module, _fetch_argv(workspace, CHANNEL_B, "--all-non-bot"))
    assert code == 0
    assert carried["messages"][0]["id"] == "5"
    code, noisy = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL_B],
    )
    assert noisy["quiet"] is False
    assert "5" in noisy["text"]
    assert "归属不清" in noisy["text"]

    _write_state(workspace, CHANNEL, last_processed_message_id="10")
    _bind_research(
        module,
        [{"ok": False, "status": "not_authorized", "hint": "Discord 拒绝了这次读取。"}],
    )
    code, failed = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert failed["read_ok"] is False
    assert failed["last_processed_message_id"] == "10"
    assert "Discord 拒绝了这次读取。" in failed["errors"]
    code, failed_summary = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert failed_summary["quiet"] is False
    assert "Discord 拒绝了这次读取。" in failed_summary["text"]

    _bind_research(module, [_page([_msg("21", SENDER)], False)])
    _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    _run(module, _mark_argv(workspace, CHANNEL, "21", "ingested", "--output", "wiki:OK"))
    code, ingested = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert ingested["quiet"] is False
    assert ingested["last_result"]["ingested"] == 1

    missing = tmp_path / "empty"
    missing.mkdir()
    code, absent = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(missing), "--channel-id", CHANNEL],
    )
    assert code == 0
    assert absent["quiet"] is False
    assert absent["status"] == "missing_cursor"
    assert absent["text"] == "还没有导入游标，这一轮没有读取频道。"
    assert absent["last_result"]["errors"] == ["missing_cursor"]
    assert absent["report_run_id"] == f"wiki-auto-ingest-{CHANNEL}-2026-11-13"
    assert not (missing / "state" / "wiki-auto-ingest" / f"{CHANNEL}.json").exists()


def test_state_write_is_atomic_and_empty_cursor_does_not_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    calls = _bind_research(module, [])
    missing = _write_state(workspace, CHANNEL, last_processed_message_id="")
    before = missing.read_bytes()
    code, empty = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 1
    assert empty["status"] == "missing_cursor"
    assert calls == []
    assert missing.read_bytes() == before

    bare = tmp_path / "bare"
    bare.mkdir()
    code, absent = _run(module, _fetch_argv(bare, CHANNEL, "--sender-id", SENDER))
    assert code == 1
    assert absent["status"] == "missing_cursor"
    assert calls == []
    assert not (bare / "state").exists()

    code, viewed = _run(
        module,
        ["--json", "state", "--workspace-dir", str(bare), "--channel-id", CHANNEL],
    )
    assert code == 0
    assert viewed["ok"] is True
    assert viewed["exists"] is False
    assert viewed["last_processed_message_id"] == ""
    assert not (bare / "state").exists()

    bad = _write_state(workspace, CHANNEL_B, last_processed_message_id="abc")
    bad_before = bad.read_bytes()
    code, bad_body = _run(module, _fetch_argv(workspace, CHANNEL_B, "--all-non-bot"))
    assert code == 1
    assert bad_body["status"] == "bad_cursor"
    assert calls == []
    assert bad.read_bytes() == bad_before

    seeded = _write_state(workspace, CHANNEL, last_processed_message_id="10")
    original = seeded.read_bytes()
    card = module._card_io()

    def _boom(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        raise OSError("replace failed")

    monkeypatch.setattr(card.os, "replace", _boom)
    code, broken = _run(module, _mark_argv(workspace, CHANNEL, "10", "skipped", "--reason", "chat"))
    assert code == 1
    assert broken["ok"] is False
    assert seeded.read_bytes() == original
    assert "replace failed" in broken["error"]


def test_page_cap_records_an_error_and_both_flags_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    assert module.PAGE_CAP == 100
    workspace = _workspace(tmp_path)
    _write_state(workspace, CHANNEL, last_processed_message_id="10", continue_after="9")
    monkeypatch.setattr(module, "PAGE_CAP", 1)
    calls = _bind_research(module, [_page([_msg("11", SENDER)], True)])
    code, body = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert len(calls) == 1
    assert body["truncated"] is True
    assert body["read_ok"] is False
    assert body["continue_after"] == "11"
    assert body["errors"] == ["本轮未读完，下次继续（continue_after 11）"]
    assert [item["id"] for item in body["messages"]] == ["11"]
    assert body["last_processed_message_id"] == "10"
    assert _read_state(workspace, CHANNEL)["continue_after"] == "11"
    code, summary = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert summary["quiet"] is False
    assert summary["continue_after"] == "11"
    assert "本轮未读完，下次继续" in summary["text"]
    assert summary["oversize_docs"] == []

    _bind_research(module, [_page([_msg("11", SENDER)], False)])
    code, finished = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert finished["truncated"] is False
    assert finished["continue_after"] == ""
    assert finished["errors"] == []
    assert _read_state(workspace, CHANNEL)["continue_after"] == ""

    _write_state(workspace, CHANNEL, last_processed_message_id="10", continue_after="11", fetch_errors=[])
    code, marked = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert code == 0
    assert marked["quiet"] is False
    assert marked["continue_after"] == "11"
    assert "本轮未读完，下次继续" in marked["text"]

    buffer = StringIO()
    with redirect_stdout(buffer):
        both = module.main(_fetch_argv(workspace, CHANNEL, "--sender-id", SENDER, "--all-non-bot"))
    assert both == 2
    assert buffer.getvalue() == ""

    buffer = StringIO()
    with redirect_stdout(buffer):
        neither = module.main(
            ["--json", "fetch", "--workspace-dir", str(workspace), "--channel-id", CHANNEL]
        )
    assert neither == 2
    assert buffer.getvalue() == ""


def test_oversize_doc_is_listed_and_kept_across_fetch(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    _write_state(workspace, CHANNEL, last_processed_message_id="10")
    note = "文档超过单次读取上限，需要人工处理"
    code, marked = _run(
        module,
        _mark_argv(
            workspace,
            CHANNEL,
            "11",
            "pending",
            "--error",
            note,
            "--doc-title",
            "行业周报",
        ),
    )
    assert code == 0
    assert marked["ok"] is True
    code, summary = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert summary["quiet"] is False
    assert summary["oversize_docs"] == [{"id": "11", "doc_title": "行业周报"}]
    assert f"消息 11《行业周报》：{note}" in summary["text"]
    assert summary["text"].count(note) == 1

    _bind_research(module, [_page([_msg("12", SENDER)], False)])
    code, fetched = _run(module, _fetch_argv(workspace, CHANNEL, "--all-non-bot"))
    assert code == 0
    assert [item["id"] for item in fetched["messages"]] == ["11", "12"]
    pending = _read_state(workspace, CHANNEL)["pending_messages"]
    assert len(pending) == 1
    assert pending[0]["id"] == "11"
    assert pending[0]["doc_title"] == "行业周报"
    assert pending[0]["oversize_doc"] is True
    assert note in pending[0]["error"]
    code, again = _run(
        module,
        ["--json", "summary", "--workspace-dir", str(workspace), "--channel-id", CHANNEL],
    )
    assert again["oversize_docs"] == [{"id": "11", "doc_title": "行业周报"}]
    assert f"消息 11《行业周报》：{note}" in again["text"]


def test_download_reuses_complete_file_and_refreshes_expired_url(tmp_path: Path) -> None:
    module = _load()
    _restore(module)
    _bind_clock(module)
    workspace = _workspace(tmp_path)
    kept = b"kept-bytes"
    reused_rel = f"state/wiki-auto-ingest/{CHANNEL}/files/30/kept.pdf"
    reused_path = workspace / reused_rel
    reused_path.parent.mkdir(parents=True)
    reused_path.write_bytes(kept)
    signed = f"https://cdn.discordapp.com/kept.pdf?ex={SIGNED}"
    _write_snapshot(
        workspace,
        CHANNEL,
        "30",
        [
            {
                "id": "1",
                "filename": "kept.pdf",
                "content_type": "application/pdf",
                "size": len(kept),
                "url": signed,
            }
        ],
    )

    def _unexpected(req: Any, timeout: float) -> _Body:
        raise AssertionError(req.full_url)

    url_calls = _bind_urlopen(module, _unexpected)
    research_calls = _bind_research(module, [])
    code, reused = _run(module, _download_argv(workspace, CHANNEL, "30"))
    assert code == 0
    assert reused["ok"] is True
    assert reused["files"] == [reused_rel]
    assert reused["errors"] == []
    assert url_calls == []
    assert research_calls == []
    assert reused_path.read_bytes() == kept
    assert SIGNED not in json.dumps(reused, ensure_ascii=False)

    empty_id = "31"
    empty_path = workspace / "state" / "wiki-auto-ingest" / CHANNEL / "files" / empty_id / "empty.pdf"
    empty_path.parent.mkdir(parents=True)
    empty_path.write_bytes(b"")
    _write_snapshot(
        workspace,
        CHANNEL,
        empty_id,
        [
            {
                "id": "2",
                "filename": "empty.pdf",
                "content_type": "application/pdf",
                "size": 0,
                "url": "https://cdn.discordapp.com/empty.pdf",
            }
        ],
    )

    def _empty(req: Any, timeout: float) -> _Body:
        return _Body(b"xx", final=req.full_url)

    url_calls = _bind_urlopen(module, _empty)
    code, emptied = _run(module, _download_argv(workspace, CHANNEL, empty_id))
    assert code == 0
    assert emptied["ok"] is True
    assert len(url_calls) == 1
    assert (workspace / emptied["files"][0]).read_bytes() == b"xx"

    fresh_id = "40"
    fresh_url = "https://cdn.discordapp.com/new.pdf"
    _write_snapshot(
        workspace,
        CHANNEL,
        fresh_id,
        [
            {
                "id": "a1",
                "filename": "note.pdf",
                "content_type": "application/pdf",
                "size": 5,
                "url": f"https://cdn.discordapp.com/old.pdf?ex={SIGNED}",
            }
        ],
    )
    research_calls = _bind_research(
        module,
        [
            _page(
                [
                    _msg(
                        fresh_id,
                        SENDER,
                        attachments=[
                            {
                                "id": "a1",
                                "filename": "note.pdf",
                                "content_type": "application/pdf",
                                "size": 5,
                                "url": fresh_url,
                            }
                        ],
                    )
                ],
                False,
            )
        ],
    )

    def _refresh_ok(req: Any, timeout: float) -> _Body:
        if req.full_url == fresh_url:
            return _Body(b"fresh", final=fresh_url)
        raise _http_gone(req.full_url, 403)

    url_calls = _bind_urlopen(module, _refresh_ok)
    code, refreshed = _run(module, _download_argv(workspace, CHANNEL, fresh_id))
    assert code == 0
    assert refreshed["ok"] is True
    assert refreshed["files"] == [f"state/wiki-auto-ingest/{CHANNEL}/files/{fresh_id}/note.pdf"]
    assert (workspace / refreshed["files"][0]).read_bytes() == b"fresh"
    assert research_calls == [
        {
            "route": "discord-history",
            "operation": "list_messages",
            "channel_id": CHANNEL,
            "after": str(int(fresh_id) - 1),
            "limit": 1,
        }
    ]
    assert len(url_calls) == 2
    saved = json.loads(
        (
            workspace / "state" / "wiki-auto-ingest" / CHANNEL / "messages" / f"{fresh_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert saved["attachments"][0]["url"] == fresh_url
    dumped = json.dumps({"result": refreshed, "snapshot": saved}, ensure_ascii=False)
    assert SIGNED not in dumped
    assert "secret-body" not in dumped

    mismatch_id = "50"
    _write_snapshot(
        workspace,
        CHANNEL,
        mismatch_id,
        [
            {
                "id": "a1",
                "filename": "gone.pdf",
                "content_type": "application/pdf",
                "size": 3,
                "url": f"https://cdn.discordapp.com/gone.pdf?ex={SIGNED}",
            }
        ],
    )
    research_calls = _bind_research(module, [_page([_msg("51", SENDER)], False)])

    def _always_404(req: Any, timeout: float) -> _Body:
        raise _http_gone(req.full_url, 404)

    url_calls = _bind_urlopen(module, _always_404)
    code, mismatched = _run(module, _download_argv(workspace, CHANNEL, mismatch_id))
    assert code == 1
    assert mismatched["files"] == []
    assert mismatched["errors"] == [
        {"id": "a1", "error": "附件链接失效，重新获取消息没有对上同一条"}
    ]
    assert len(research_calls) == 1
    assert research_calls[0]["after"] == str(int(mismatch_id) - 1)
    assert research_calls[0]["limit"] == 1
    assert len(url_calls) == 1
    assert SIGNED not in json.dumps(mismatched, ensure_ascii=False)
    assert "secret-body" not in json.dumps(mismatched, ensure_ascii=False)
    kept_snap = json.loads(
        (
            workspace / "state" / "wiki-auto-ingest" / CHANNEL / "messages" / f"{mismatch_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert "ex=" in kept_snap["attachments"][0]["url"]

    stalled_id = "60"
    stalled_new = "https://cdn.discordapp.com/new-stalled.pdf"
    _write_snapshot(
        workspace,
        CHANNEL,
        stalled_id,
        [
            {
                "id": "a1",
                "filename": "stalled.pdf",
                "content_type": "application/pdf",
                "size": 3,
                "url": f"https://cdn.discordapp.com/stalled.pdf?ex={SIGNED}",
            }
        ],
    )
    research_calls = _bind_research(
        module,
        [
            _page(
                [
                    _msg(
                        stalled_id,
                        SENDER,
                        attachments=[
                            {
                                "id": "a1",
                                "filename": "stalled.pdf",
                                "content_type": "application/pdf",
                                "size": 3,
                                "url": stalled_new,
                            }
                        ],
                    )
                ],
                False,
            )
        ],
    )
    stalled_body: dict[str, _Body] = {}

    def _still_410(req: Any, timeout: float) -> _Body:
        if req.full_url == stalled_new:
            body = _Body(b"secret-body", final=req.full_url)
            body.status = 410
            stalled_body["resp"] = body
            return body
        raise _http_gone(req.full_url, 410)

    url_calls = _bind_urlopen(module, _still_410)
    code, stalled = _run(module, _download_argv(workspace, CHANNEL, stalled_id))
    assert code == 1
    assert stalled["files"] == []
    assert stalled["errors"] == [{"id": "a1", "error": "重新获取后仍是 HTTP 410"}]
    assert len(research_calls) == 1
    assert len(url_calls) == 2
    assert stalled_body["resp"].read_count == 0
    assert not (
        workspace / "state" / "wiki-auto-ingest" / CHANNEL / "files" / stalled_id
    ).exists()
    dumped_stalled = json.dumps(stalled, ensure_ascii=False)
    assert SIGNED not in dumped_stalled
    assert "secret-body" not in dumped_stalled
