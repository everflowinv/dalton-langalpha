"""Unit tests for the read-only research route ``tmeet``."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"

if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

# A test may install the subprocess stub twice; autospec the real function, never the stub.
_REAL_CREATE_SUBPROCESS_EXEC = asyncio.create_subprocess_exec


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tmeet_exists: bool = True) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    for proxy_var in (
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
        "ALL_PROXY",
        "DALTON_BRIDGE_SUBPROCESS_PROXY",
    ):
        monkeypatch.delenv(proxy_var, raising=False)
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    dummy_xreach = tmp_path / "xreach"
    dummy_xreach.write_text("#!/bin/sh\n", encoding="utf-8")
    tmeet_bin = tmp_path / "nodebin" / "tmeet"
    tmeet_bin.parent.mkdir(parents=True, exist_ok=True)
    if tmeet_exists:
        tmeet_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_XREACH_BIN", str(dummy_xreach))
    monkeypatch.setenv("DALTON_TMEET_BIN", str(tmeet_bin if tmeet_exists else tmp_path / "missing-tmeet"))
    monkeypatch.setenv("SEED_ASR_ACCESS_KEY", "asr-secret-value")
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _install_exec(monkeypatch: pytest.MonkeyPatch, payloads: list[tuple[bytes, bytes, int | BaseException]]) -> tuple[Any, list[tuple[list[str], dict[str, Any]]]]:
    exec_mock = create_autospec(_REAL_CREATE_SUBPROCESS_EXEC)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    queue = list(payloads)

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append((list(cmd), kwargs))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.pid = None
        item = queue.pop(0)
        if isinstance(item[2], BaseException):
            proc.returncode = 0

            async def _boom(input: bytes | None = None) -> tuple[bytes, bytes]:
                raise item[2]

            proc.communicate.side_effect = _boom
            return proc
        stdout, stderr, returncode = item
        proc.returncode = returncode

        async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
            return stdout, stderr

        proc.communicate.side_effect = _communicate
        return proc

    exec_mock.side_effect = _fake_exec
    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
    return exec_mock, calls


def _json_out(payload: dict[str, Any]) -> tuple[bytes, bytes, int]:
    return json.dumps(payload).encode("utf-8"), b"", 0


async def _call(bridge: rb.ResearchDataBridge, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    resp = await bridge.handle_tools_call(1, {"name": "research", "arguments": {"route": "tmeet", **args}})
    body = json.loads(resp["result"]["content"][0]["text"])
    return resp, body


def test_shanghai_wall_clock_and_refresh_days() -> None:
    assert rb._tmeet_shanghai_iso("2026-11-13", end=False) == "2026-11-13T00:00:00+08:00"
    assert rb._tmeet_shanghai_iso("2026-12-01", end=True) == "2026-12-01T23:59:59+08:00"
    assert rb._tmeet_shanghai_iso("2026-11-13T15:00:00", end=False) == "2026-11-13T15:00:00+08:00"
    assert rb._tmeet_shanghai_iso("2026-11-13T07:00:00Z", end=False) == "2026-11-13T15:00:00+08:00"
    today = date(2026, 11, 13)
    assert rb._tmeet_refresh_days("expires in 12 days, on 2026-11-01", today=today) == 12
    assert rb._tmeet_refresh_days("valid through 2026-11-20", today=today) == 7
    assert rb._tmeet_refresh_days("logged in", today=today) is None


class TestTmeetRoute:
    @pytest.mark.asyncio
    async def test_auth_status_hides_secrets_and_reports_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        text = (
            "Logged in. refresh token expires in 12 days.\n"
            "OpenId: oid-EXAMPLE-999\n"
            "username: user-ada-example\n"
            "token=sekret-value\n"
        )
        _install_exec(monkeypatch, [(text.encode("utf-8"), b"token=sekret-value\n", 0)])
        resp, body = await _call(bridge, {"operation": "auth_status"})
        dumped = json.dumps(resp, ensure_ascii=False)
        assert resp["result"]["isError"] is False
        assert body["ok"] is True
        assert set(body["data"]) == {"logged_in", "refresh_token_days_left"}
        assert body["data"]["logged_in"] is True
        assert body["data"]["refresh_token_days_left"] == 12
        assert body["_sources"][0]["document_id"] == "tmeet:auth"
        assert body["_sources"][0]["publisher"] == "腾讯会议"
        assert body["_sources"][0]["identity"] == "internal_note"
        assert body["_sources"][0]["source_type"] == "meeting_minutes"
        assert body["_sources"][0]["private"] is True
        for secret in ("oid-EXAMPLE-999", "user-ada-example", "sekret-value", "token=sekret-value"):
            assert secret not in dumped
        assert "stdout" not in body
        assert "stderr" not in body

    @pytest.mark.asyncio
    async def test_auth_not_logged_in_returns_hint_without_raw_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        text = "Not logged in\nOpenId: oid-EXAMPLE-999\nusername: user-ada-example\ntoken=sekret-value\n"
        _install_exec(monkeypatch, [(text.encode("utf-8"), b"", 0)])
        _resp, body = await _call(bridge, {"operation": "auth_status"})
        dumped = json.dumps(body, ensure_ascii=False)
        assert body["ok"] is False
        assert body["status"] == "not_logged_in"
        assert body["error_code"] == "not_logged_in"
        assert "需要在宿主机重新登录 tmeet" in body["hint"]
        for secret in ("oid-EXAMPLE-999", "user-ada-example", "sekret-value", "Not logged in"):
            assert secret not in dumped
        assert "stdout" not in body
        assert "stderr" not in body

    @pytest.mark.asyncio
    async def test_auth_contract_failure_omits_openid_and_username(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets = ("oid-EXAMPLE-999", "user-ada-example", "sekret-value", "OpenId", "UserName")
        cases = (
            "Unknown Command\nOpenId: oid-EXAMPLE-999\nUserName: user-ada-example\n",
            "status unclear\nOpenId: oid-EXAMPLE-999\nUserName: user-ada-example\n",
        )
        for stdout in cases:
            bridge = _make_bridge(tmp_path, monkeypatch)
            _install_exec(
                monkeypatch,
                [(stdout.encode("utf-8"), b"token=sekret-value\nUserName: user-ada-example\n", 0)],
            )
            resp, body = await _call(bridge, {"operation": "auth_status"})
            dumped = json.dumps(resp, ensure_ascii=False)
            assert body["ok"] is False
            assert body["status"] == "cli_contract_changed"
            assert body["error_code"] == "cli_contract_changed"
            assert body["hint"]
            assert "stdout" not in body
            assert "stderr" not in body
            for secret in secrets:
                assert secret not in dumped

    @pytest.mark.asyncio
    async def test_auth_days_null_when_no_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        _install_exec(monkeypatch, [(b"logged in\n", b"", 0)])
        _resp, body = await _call(bridge, {"operation": "auth_status"})
        assert body["ok"] is True
        assert body["data"]["refresh_token_days_left"] is None

    @pytest.mark.asyncio
    async def test_search_argv_pagination_filter_and_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        page_one = {
            "data": {
                "has_more": True,
                "next_page_token": "p.1",
                "records": [
                    {
                        "record_file_id": "100",
                        "file_type": "video",
                        "subject": "其他会议",
                        "meeting_start_time": "2026-11-07T09:00:00+08:00",
                        "avatar_url": "https://example.invalid/a.png",
                        "creator_name": "hidden-person",
                    },
                    {
                        "record_file_id": "200",
                        "file_type": "transcript",
                        "subject": "周会预定的会议",
                        "meeting_start_time": "2026-11-07T09:00:00+08:00",
                        "meeting_record_id": "m-200",
                        "duration_seconds": 60,
                        "has_smart_minutes": True,
                        "has_transcript_content": False,
                    },
                ],
            }
        }
        page_two = {
            "data": {
                "has_more": False,
                "records": [
                    {
                        "record_file_id": "400",
                        "file_type": "transcript",
                        "subject": "未登录问题但主题含关键字预定的会议",
                        "meeting_start_time": "2026-11-13T09:00:00+08:00",
                    }
                ],
            }
        }
        _exec_mock, calls = _install_exec(monkeypatch, [_json_out(page_one), _json_out(page_two)])
        resp, body = await _call(
            bridge,
            {
                "operation": "record_search",
                "start": "2026-11-09",
                "end": "2026-12-01T08:00:00Z",
                "query": "预定的会议",
                "limit": 20,
            },
        )
        assert resp["result"]["isError"] is False
        assert body["ok"] is True
        assert "has_more" not in body
        assert [item["record_file_id"] for item in body["data"]["records"]] == ["200", "400"]
        assert body["data"]["record_count"] == 2
        assert body["data"]["truncated"] is False
        assert body["data"]["pages_scanned"] == 2
        dumped = json.dumps(body, ensure_ascii=False)
        assert "avatar_url" not in dumped
        assert "hidden-person" not in dumped
        assert "example.invalid" not in dumped
        assert body["normalized_params"]["start"] == "2026-11-09T00:00:00+08:00"
        assert body["normalized_params"]["end"] == "2026-12-01T16:00:00+08:00"
        assert [item["document_id"] for item in body["_sources"]] == ["tmeet:200", "tmeet:400"]
        assert _exec_mock.await_count == 2
        first, first_kwargs = calls[0]
        second, second_kwargs = calls[1]
        assert first == [
            str(bridge.cfg.tmeet_bin),
            "record",
            "search",
            "--start",
            "2026-11-09T00:00:00+08:00",
            "--end",
            "2026-12-01T16:00:00+08:00",
            "--page-size",
            "30",
            "--format",
            "json",
        ]
        assert "--query" not in first
        assert "预定的会议" not in first
        assert second[-2:] == ["--page-token", "p.1"]
        for kwargs in (first_kwargs, second_kwargs):
            assert kwargs["env"]["PATH"].split(os.pathsep)[0] == str(bridge.cfg.tmeet_bin.parent)
            assert "SEED_ASR_ACCESS_KEY" not in kwargs["env"]
            assert "asr-secret-value" not in kwargs["env"].values()
            assert kwargs["start_new_session"] is True
            assert kwargs["stdin"] is asyncio.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_search_page_cap_and_limit_truncation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        pages = [
            _json_out(
                {
                    "data": {
                        "has_more": True,
                        "next_page_token": f"p{index}",
                        "records": [
                            {
                                "record_file_id": str(1000 + index),
                                "subject": "预定的会议",
                                "file_type": "transcript",
                                "meeting_start_time": "2026-11-13T09:00:00+08:00",
                            }
                        ],
                    }
                }
            )
            for index in range(10)
        ]
        _exec_mock, _calls = _install_exec(monkeypatch, pages)
        _resp, body = await _call(
            bridge,
            {"operation": "record_search", "start": "2026-11-01", "end": "2026-11-13"},
        )
        assert _exec_mock.await_count == 10
        assert body["ok"] is True
        assert body["data"]["pages_scanned"] == 10
        assert body["data"]["truncated"] is True
        assert "10 页" in body["hint"]

        bridge_limit = _make_bridge(tmp_path, monkeypatch)
        _exec_limit, _limit_calls = _install_exec(
            monkeypatch,
            [
                _json_out(
                    {
                        "data": {
                            "has_more": False,
                            "records": [
                                {
                                    "record_file_id": "1",
                                    "subject": "预定的会议",
                                    "file_type": "transcript",
                                    "meeting_start_time": "2026-11-13T09:00:00+08:00",
                                },
                                {
                                    "record_file_id": "2",
                                    "subject": "预定的会议",
                                    "file_type": "video",
                                    "meeting_start_time": "2026-11-13T09:00:00+08:00",
                                },
                            ],
                        }
                    }
                )
            ],
        )
        _resp_limit, limited = await _call(
            bridge_limit,
            {
                "operation": "record_search",
                "start": "2026-11-13T00:00:00+08:00",
                "end": "2026-11-13T23:59:59+08:00",
                "limit": 1,
            },
        )
        assert _exec_limit.await_count == 1
        assert limited["data"]["record_count"] == 1
        assert limited["data"]["records"][0]["record_file_id"] == "1"
        assert limited["data"]["truncated"] is True
        assert "limit" in limited["hint"]

    @pytest.mark.asyncio
    async def test_invalid_page_token_is_contract_error_without_partial_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        _exec_mock, _calls = _install_exec(
            monkeypatch,
            [
                _json_out(
                    {
                        "data": {
                            "has_more": True,
                            "next_page_token": "bad token",
                            "records": [
                                {
                                    "record_file_id": "900001",
                                    "subject": "预定的会议",
                                    "file_type": "transcript",
                                }
                            ],
                        }
                    }
                )
            ],
        )
        resp, body = await _call(
            bridge,
            {"operation": "record_search", "start": "2026-11-01", "end": "2026-11-13"},
        )
        assert _exec_mock.await_count == 1
        assert resp["result"]["isError"] is True
        assert body["ok"] is False
        assert body["status"] == "cli_contract_changed"
        assert body["data"] is None
        assert "900001" not in json.dumps(body, ensure_ascii=False)

    @pytest.mark.asyncio
    async def test_empty_search_is_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        _install_exec(monkeypatch, [_json_out({"data": {"has_more": False, "records": []}})])
        resp, body = await _call(
            bridge,
            {"operation": "record_search", "start": "2026-11-01", "end": "2026-11-02"},
        )
        assert resp["result"]["isError"] is False
        assert body["ok"] is True
        assert body["status"] == "ok"
        assert body["data"]["records"] == []
        assert "empty_reason" not in body

    @pytest.mark.asyncio
    async def test_content_argv_and_normalization(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        minutes = {
            "message": "success",
            "data": {
                "meeting_minute": {
                    "minute": "纪要正文",
                    "todos": [
                        {
                            "content": "核对数字",
                            "executor_list": [{"nick": "甲", "open_id": "cli_drop_me"}],
                        }
                    ],
                }
            }
        }
        todos = {
            "message": "success",
            "data": {
                "todos": [
                    {"content": "回访", "executor_list": [{"nick": "乙", "open_id": "cli_drop_me"}]}
                ]
            }
        }
        transcript = {
            "message": "success",
            "data": {
                "more": True,
                "minutes": {
                    "paragraphs": [
                        {
                            "pid": "0",
                            "speaker": {
                                "user_name": "甲",
                                "user_id": "u-1",
                                "avatar_url": "https://example.invalid/a.png",
                            },
                            "sentences": [{"words": [{"text": "你好"}]}],
                        }
                    ]
                },
            }
        }
        flat_transcript = {
            "message": "success",
            "data": {
                "more": False,
                "paragraphs": [
                    {"pid": "3", "speaker": {"user_name": "乙", "avatar_url": "https://example.invalid/b.png"}}
                ],
            }
        }
        _exec_mock, calls = _install_exec(
            monkeypatch,
            [_json_out(minutes), _json_out(todos), _json_out(transcript), _json_out(flat_transcript)],
        )
        _resp, minutes_body = await _call(
            bridge,
            {"operation": "record_content", "action": "minutes", "record_file_id": "900001"},
        )
        _resp, todos_body = await _call(
            bridge,
            {"operation": "record_content", "action": "todos", "record_file_id": 900001, "lang": "zh"},
        )
        _resp, transcript_body = await _call(
            bridge,
            {
                "operation": "record_content",
                "action": "transcript",
                "record_file_id": "900001",
                "pid": "0",
                "limit": 100,
            },
        )
        _resp, flat_body = await _call(
            bridge,
            {"operation": "record_content", "action": "transcript", "record_file_id": "900001"},
        )
        bin_path = str(bridge.cfg.tmeet_bin)
        assert calls[0][0] == [
            bin_path,
            "record",
            "content",
            "--action",
            "minutes",
            "--record-file-id",
            "900001",
            "--lang",
            "zh",
        ]
        assert calls[1][0] == [
            bin_path,
            "record",
            "content",
            "--action",
            "todos",
            "--record-file-id",
            "900001",
            "--lang",
            "zh",
        ]
        assert calls[2][0] == [
            bin_path,
            "record",
            "content",
            "--action",
            "transcript",
            "--record-file-id",
            "900001",
            "--pid",
            "0",
            "--limit",
            "100",
        ]
        assert calls[3][0] == [
            bin_path,
            "record",
            "content",
            "--action",
            "transcript",
            "--record-file-id",
            "900001",
        ]
        for argv, _kwargs in calls:
            assert "--format" not in argv
        assert minutes_body["data"]["minute"] == "纪要正文"
        assert minutes_body["data"]["todos"] == [{"content": "核对数字", "executors": ["甲"]}]
        assert todos_body["data"]["todos"] == [{"content": "回访", "executors": ["乙"]}]
        assert "minute" not in todos_body["data"]
        assert transcript_body["data"]["more"] is True
        assert transcript_body["data"]["next_pid"] == "1"
        speaker = transcript_body["data"]["paragraphs"][0]["speaker"]
        assert speaker["user_name"] == "甲"
        assert speaker["user_id"] == "u-1"
        assert "avatar_url" not in speaker
        assert flat_body["data"]["more"] is False
        assert flat_body["data"]["next_pid"] is None
        assert flat_body["data"]["paragraphs"][0]["speaker"]["user_name"] == "乙"
        dumped = json.dumps(
            [minutes_body, todos_body, transcript_body, flat_body],
            ensure_ascii=False,
        )
        assert "cli_drop_me" not in dumped
        assert "avatar_url" not in dumped
        assert "example.invalid" not in dumped
        assert _exec_mock.await_count == 4
        assert transcript_body["_sources"][0]["document_id"] == "tmeet:900001"
        assert transcript_body["_sources"][0]["as_of_basis"] == "retrieved_at"

    @pytest.mark.asyncio
    async def test_transcript_omits_limit_when_caller_omits_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        _exec_mock, calls = _install_exec(
            monkeypatch,
            [_json_out({"message": "success", "data": {"more": False, "minutes": {"paragraphs": []}}})],
        )
        _resp, body = await _call(
            bridge,
            {"operation": "record_content", "action": "transcript", "record_file_id": "900001"},
        )
        assert body["ok"] is True
        assert calls[0][0] == [
            str(bridge.cfg.tmeet_bin),
            "record",
            "content",
            "--action",
            "transcript",
            "--record-file-id",
            "900001",
        ]
        assert _exec_mock.await_count == 1

    @pytest.mark.asyncio
    async def test_content_rejects_missing_fields_and_accepts_empty_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        marker = "shape-marker-oid"
        bad = [
            ("minutes", {}),
            ("minutes", {"message": "success", "data": [], "note": marker}),
            ("minutes", {"message": "not-success", "data": {"meeting_minute": {"minute": "x"}}, "note": marker}),
            ("transcript", {"message": "success", "note": marker}),
        ]
        good = [
            ("minutes", {"message": "success", "data": {}, "note": marker}, {"minute": "", "todos": []}),
            ("minutes", {"message": "success", "data": None}, {"minute": "", "todos": []}),
            ("minutes", {"message": "success", "data": {"meeting_minute": None}}, {"minute": "", "todos": []}),
            ("minutes", {"message": "success", "data": {"meeting_minute": {"minute": ""}}}, {"minute": ""}),
            ("minutes", {"message": "success", "data": {"minutes": {"minute": ""}}}, {"minute": ""}),
            ("todos", {"message": "success", "data": {}}, {"todos": []}),
            ("todos", {"message": "success", "data": {"todos": None}}, {"todos": []}),
            ("todos", {"message": "success", "data": {"todos": []}}, {"todos": []}),
            (
                "transcript",
                {"message": "success", "data": {}},
                {"paragraphs": [], "more": False},
            ),
            (
                "transcript",
                {"message": "success", "data": {"minutes": None, "paragraphs": None, "more": None}},
                {"paragraphs": [], "more": False},
            ),
            (
                "transcript",
                {"message": "success", "data": {"more": False, "minutes": {"paragraphs": []}}},
                {"paragraphs": [], "more": False},
            ),
            (
                "transcript",
                {"message": "success", "data": {"paragraphs": []}},
                {"paragraphs": [], "more": False},
            ),
        ]
        stderr = b"UserName: user-ada-example\nOpenId: oid-EXAMPLE-999\n"
        queue: list[tuple[bytes, bytes, int]] = [
            (json.dumps(payload).encode("utf-8"), stderr, 0) for _action, payload in bad
        ]
        queue.extend(_json_out(payload) for _action, payload, _expected in good)
        _install_exec(monkeypatch, queue)
        for action, payload in bad:
            resp, body = await _call(
                bridge,
                {"operation": "record_content", "action": action, "record_file_id": "900001"},
            )
            dumped = json.dumps(resp, ensure_ascii=False)
            assert body["ok"] is False, payload
            assert body["status"] == "cli_contract_changed"
            assert body["error_code"] == "cli_contract_changed"
            assert "stdout" not in body
            assert "stderr" not in body
            assert marker not in dumped
            assert "UserName" not in dumped
            assert "OpenId" not in dumped
        for action, payload, expected in good:
            resp, body = await _call(
                bridge,
                {"operation": "record_content", "action": action, "record_file_id": "900001"},
            )
            dumped = json.dumps(resp, ensure_ascii=False)
            assert body["ok"] is True, payload
            assert body["status"] == "ok"
            for key, value in expected.items():
                assert body["data"][key] == value
            assert marker not in dumped

    @pytest.mark.asyncio
    async def test_rejects_illegal_action_file_id_and_unknown_params_without_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, _calls = _install_exec(monkeypatch, [])
        cases = [
            ({"operation": "record_content", "action": "delete", "record_file_id": "900001"}, "minutes、todos、transcript"),
            ({"operation": "record_content", "action": "paragraphs", "record_file_id": "900001"}, "minutes、todos、transcript"),
            ({"operation": "record_content", "action": "minutes", "record_file_id": "12a"}, "1 到 32"),
            ({"operation": "record_content", "action": "minutes", "record_file_id": "../900001"}, "1 到 32"),
            ({"operation": "auth_status", "mode": "write"}, "mode"),
            ({"operation": "record_content", "action": "minutes", "record_file_id": "900001", "limit": 10}, "limit"),
            ({"operation": "record_search", "start": "2026-11-13", "end": "2026-11-01"}, "不晚于"),
            ({"operation": "record_search", "start": "2026-11-13", "end": "2026-11-13", "limit": True}, "limit"),
        ]
        for args, needle in cases:
            _resp, body = await _call(bridge, args)
            assert body["ok"] is False, args
            assert body["status"] == "invalid_arguments"
            assert needle in body["hint"]
            if args.get("action") == "paragraphs":
                assert body["hint"] == "record_content 的 action 只能是 minutes、todos、transcript。"
        _resp, created = await _call(bridge, {"operation": "meeting_create"})
        assert created["ok"] is False
        assert created["error_code"] == "invalid_operation"
        _resp, unknown = await _call(bridge, {"operation": "auth_status", "foo": "1"})
        assert unknown["error_code"] == "unknown_parameters"
        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_missing_binary_non_json_and_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = _make_bridge(tmp_path, monkeypatch, tmeet_exists=False)
        exec_mock, _calls = _install_exec(monkeypatch, [])
        _resp, body = await _call(missing, {"operation": "auth_status"})
        assert body["ok"] is False
        assert body["status"] == "source_missing"
        assert "DALTON_TMEET_BIN" in body["hint"]
        assert exec_mock.await_count == 0

        present = _make_bridge(tmp_path, monkeypatch)
        _install_exec(
            monkeypatch,
            [(b"not json at all oid-EXAMPLE-999", b"token=sekret-value\n", 0)],
        )
        _resp, contract = await _call(
            present,
            {"operation": "record_search", "start": "2026-11-01", "end": "2026-11-13"},
        )
        dumped = json.dumps(contract, ensure_ascii=False)
        assert contract["ok"] is False
        assert contract["status"] == "cli_contract_changed"
        assert contract["error_code"] == "cli_contract_changed"
        assert "v1.1.0" in contract["hint"]
        assert "oid-EXAMPLE-999" not in dumped
        assert "sekret-value" not in dumped

        timeout_bridge = _make_bridge(tmp_path, monkeypatch)
        _install_exec(monkeypatch, [(b"", b"", asyncio.TimeoutError())])
        _resp, timed = await _call(timeout_bridge, {"operation": "auth_status"})
        assert timed["ok"] is False
        assert timed["status"] == "timeout"
        assert "超时" in timed["hint"]

    @pytest.mark.asyncio
    async def test_catalog_injects_tmeet_without_calling_the_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        commands: list[list[str]] = []

        def _mcp_response(req_id: Any) -> dict[str, Any]:
            payload = {"ok": True, "status": "ok", "routes": [{"id": "alphaengine", "category": "research", "operations": ["search_library"]}]}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
            }

        exec_mock = create_autospec(_REAL_CREATE_SUBPROCESS_EXEC)

        async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            commands.append([str(part) for part in cmd])
            proc = create_autospec(asyncio.subprocess.Process, instance=True)
            proc.returncode = 0
            proc.pid = None

            async def _communicate(input: bytes | None = None) -> tuple[bytes, bytes]:
                lines = (input or b"").decode("utf-8").splitlines()
                reqs = [json.loads(line) for line in lines if line.strip()]
                proc.returncode = 0
                return (json.dumps(_mcp_response(reqs[0]["id"])) + "\n").encode("utf-8"), b""

            proc.communicate.side_effect = _communicate
            return proc

        exec_mock.side_effect = _fake_exec
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
        listed = await bridge.handle_tools_call(2, {"name": "catalog", "arguments": {}})
        listed_body = json.loads(listed["result"]["content"][0]["text"])
        assert any(item.get("id") == "tmeet" for item in listed_body["routes"])
        single = await bridge.handle_tools_call(3, {"name": "catalog", "arguments": {"route": "tmeet"}})
        single_body = json.loads(single["result"]["content"][0]["text"])
        assert single_body["route_count"] == 1
        entry = single_body["routes"][0]
        assert entry["id"] == "tmeet"
        assert entry["required_params"] == rb.ROUTE_CONTRACT_SPECS["tmeet"]["required_params"]
        assert entry["accepted_params"] == rb.ROUTE_CONTRACT_SPECS["tmeet"]["accepted_params"]
        assert all("record" not in part and part != "auth" and "--version" not in part for cmd in commands for part in cmd)
        assert str(bridge.cfg.tmeet_bin) not in [part for cmd in commands for part in cmd]


def test_health_probe_uses_auth_status() -> None:
    from src.dalton.data_map.health import build_route_probe_spec

    tool_name, args = build_route_probe_spec("tmeet", "research")
    assert tool_name == "research"
    assert args == {"route": "tmeet", "operation": "auth_status"}
    assert args["operation"] != "search"


def test_refresh_days_reads_refresh_token_line_not_access_token() -> None:
    text = (
        "Logged in\n"
        "  OpenId:  cli_example\n"
        "  UserName:  Example\n"
        "  AccessToken:  valid (expires at 2026-11-13 07:36:01, remaining 4h 36m)\n"
        "  RefreshToken: valid (expires at 2026-12-13 01:36:01, remaining 29d 22h 36m)\n"
    )
    assert rb._tmeet_refresh_days(text, today=date(2026, 11, 13)) == 29
    no_compact = text.replace(", remaining 29d 22h 36m", "")
    assert rb._tmeet_refresh_days(no_compact, today=date(2026, 11, 13)) == 30
