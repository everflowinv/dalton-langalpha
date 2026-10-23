"""Unit tests for the bridge-executed `x-timeline` (xreach) community route and health-probe date windows."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
import source_identity  # noqa: E402
from src.dalton.data_map import health as health_mod  # noqa: E402

XREACH_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "xreach" / "tweets_nvidia.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(XREACH_FIXTURE.read_text(encoding="utf-8"))


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rb.ResearchDataBridge:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    # xreach argv assertions must not see the host's proxy configuration.
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
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    monkeypatch.setenv("DALTON_XREACH_BIN", str(dummy_xreach))
    cfg = rb.BridgeConfig()
    return rb.ResearchDataBridge(cfg)


def _xreach_exec_double(stdout_payload: Any) -> tuple[Any, list[list[str]]]:
    """`create_autospec(asyncio.create_subprocess_exec)` double returning fixed stdout; records argv."""
    exec_mock = create_autospec(asyncio.create_subprocess_exec)
    calls: list[list[str]] = []

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        calls.append(list(cmd))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        out = stdout_payload if isinstance(stdout_payload, bytes) else json.dumps(stdout_payload).encode("utf-8")
        proc.communicate.return_value = (out, b"")
        return proc

    exec_mock.side_effect = _fake_exec
    return exec_mock, calls


async def _call(
    bridge: rb.ResearchDataBridge, req_id: int, args: dict[str, Any]
) -> dict[str, Any]:
    resp = await bridge.handle_tools_call(req_id, {"name": "community", "arguments": args})
    return json.loads(resp["result"]["content"][0]["text"])


class TestXTimelineParamValidation:
    @pytest.mark.asyncio
    async def test_handle_forms_normalize_to_same_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        for handle in (
            "@nvidia",
            "nvidia",
            "https://x.com/nvidia",
            "https://twitter.com/nvidia",
            "https://x.com/nvidia/with/extra/path",
        ):
            body = await _call(bridge, 1, {"route": "x-timeline", "handle": handle})
            assert body["ok"] is True, f"failed for handle={handle}: {body}"

        assert exec_mock.await_count == 5
        assert all(c[1:3] == ["tweets", "@nvidia"] for c in calls)
        assert all(c[3:5] == ["-n", "20"] for c in calls)
        assert all(c[-1] == "--json" for c in calls)

    @pytest.mark.asyncio
    async def test_invalid_handles_rejected_without_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, _calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        for bad in (
            None,
            "",
            "  ",
            "nvidia; rm -rf /",
            "nvidia$(curl evil.com)",
            "x" * 16,
            "has space",
            "https://evil.com/nvidia",
            "https://x.com/",
            "https://xueqiu.com/nvidia",
        ):
            body = await _call(bridge, 2, {"route": "x-timeline", "handle": bad})
            assert body["ok"] is False, f"expected rejection for handle={bad!r}"
            assert body["status"] == "invalid_arguments"

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_forbidden_subcommands_and_params_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, _calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        bad_calls = [
            {"route": "x-timeline", "operation": op, "handle": "nvidia"}
            for op in ("auth", "followers", "following", "doctor", "whoami", "logout")
        ] + [
            {"route": "x-timeline", "handle": "nvidia", **extra}
            for extra in (
                {"auth_token": "tok"},
                {"ct0": "ct0"},
                {"cookie_source": "browser"},
                {"max_pages": 3},
                {"proxy": "http://evil.com:8080"},
                {"delay": "0.1"},
            )
        ]
        for idx, args in enumerate(bad_calls):
            body = await _call(bridge, 100 + idx, args)
            assert body["ok"] is False, f"expected rejection for {args}"
            assert body["status"] == "invalid_arguments"

        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_count_bounds_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        for bad_count in (0, 101, "20", 2.5, True):
            body = await _call(bridge, 3, {"route": "x-timeline", "handle": "nvidia", "count": bad_count})
            assert body["ok"] is False, f"expected rejection for count={bad_count!r}"
            assert body["status"] == "invalid_arguments"
        assert exec_mock.await_count == 0

        for good_count in (1, 100):
            body = await _call(bridge, 4, {"route": "x-timeline", "handle": "nvidia", "count": good_count})
            assert body["ok"] is True
        assert [c[4] for c in calls] == ["1", "100"]

    @pytest.mark.asyncio
    async def test_search_query_rules_and_window_splicing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        bad_queries = [
            {"route": "x-timeline", "operation": "search"},  # query required
            {"route": "x-timeline", "operation": "search", "query": ""},
            {"route": "x-timeline", "operation": "search", "query": "NVDA\n--auth-token x"},
            {"route": "x-timeline", "operation": "search", "query": "-nvidia"},
            {"route": "x-timeline", "operation": "search", "query": "NVDA", "since": "2026/10/01"},
            {"route": "x-timeline", "operation": "search", "query": "NVDA", "since": "2026-13-40"},
            {
                "route": "x-timeline",
                "operation": "search",
                "query": "NVDA",
                "since": "2026-10-23",
                "until": "2026-10-01",
            },
            {"route": "x-timeline", "operation": "search", "query": "NVDA", "handle": "nvidia"},
        ]
        for idx, args in enumerate(bad_queries):
            body = await _call(bridge, 200 + idx, args)
            assert body["ok"] is False, f"expected rejection for {args}"
            assert body["status"] == "invalid_arguments"
        assert exec_mock.await_count == 0

        body = await _call(
            bridge,
            210,
            {
                "route": "x-timeline",
                "operation": "search",
                "query": "NVDA Blackwell",
                "since": "2026-10-01",
                "until": "2026-10-23",
            },
        )
        assert body["ok"] is True
        assert calls[0][1:3] == ["search", "NVDA Blackwell since:2026-10-01 until:2026-10-23"]

        body_since_only = await _call(
            bridge, 211, {"route": "x-timeline", "operation": "search", "query": "NVDA", "since": "2026-10-01"}
        )
        assert body_since_only["ok"] is True
        assert calls[1][2] == "NVDA since:2026-10-01"

    @pytest.mark.asyncio
    async def test_tweet_thread_targets_and_url_whitelist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        bad_calls = [
            {"route": "x-timeline", "operation": "tweet"},  # no target
            {"route": "x-timeline", "operation": "tweet", "url": "https://evil.com/status/1"},
            {
                "route": "x-timeline",
                "operation": "tweet",
                "url": "https://xueqiu.com/status/1",
            },
            {
                "route": "x-timeline",
                "operation": "tweet",
                "url": "https://x.com/nvidia/status/2112920354230296993",
                "tweet_id": "2112920354230296993",
            },
            {"route": "x-timeline", "operation": "thread", "tweet_id": "123;id"},
            {"route": "x-timeline", "operation": "tweet", "tweet_id": 12345},
            # Cross-operation parameter leakage
            {"route": "x-timeline", "operation": "tweets", "handle": "nvidia", "query": "NVDA"},
            {"route": "x-timeline", "operation": "tweets", "handle": "nvidia", "url": "https://x.com/a/status/1"},
            {
                "route": "x-timeline",
                "operation": "tweet",
                "tweet_id": "2112920354230296993",
                "count": 5,
                "since": "2026-10-01",
                "include_replies": True,
            },
        ]
        for idx, args in enumerate(bad_calls):
            body = await _call(bridge, 300 + idx, args)
            assert body["ok"] is False, f"expected rejection for {args}"
            assert body["status"] == "invalid_arguments"
        assert exec_mock.await_count == 0

        body = await _call(
            bridge,
            310,
            {"route": "x-timeline", "operation": "tweet", "url": "https://x.com/nvidia/status/2112920354230296993"},
        )
        assert body["ok"] is True
        assert calls[0][1:3] == ["tweet", "https://x.com/nvidia/status/2112920354230296993"]
        assert calls[0][-1] == "--json"

        body_tw = await _call(
            bridge,
            311,
            {"route": "x-timeline", "operation": "tweet", "url": "https://twitter.com/nvidia/status/2112920354230296993"},
        )
        assert body_tw["ok"] is True
        assert calls[1][2] == "https://twitter.com/nvidia/status/2112920354230296993"

        body_th = await _call(
            bridge, 312, {"route": "x-timeline", "operation": "thread", "tweet_id": "2112920354230296993"}
        )
        assert body_th["ok"] is True
        assert calls[2][1:3] == ["thread", "2112920354230296993"]

    @pytest.mark.asyncio
    async def test_include_replies_and_replies_only_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        body = await _call(
            bridge, 5, {"route": "x-timeline", "handle": "nvidia", "include_replies": True, "count": 7}
        )
        assert body["ok"] is True
        assert calls[0][:6] == [str(bridge.cfg.xreach_bin), "tweets", "@nvidia", "-n", "7", "--replies"]
        assert calls[0][-1] == "--json"

        body_bool = await _call(bridge, 6, {"route": "x-timeline", "handle": "nvidia", "include_replies": "yes"})
        assert body_bool["ok"] is False
        assert body_bool["status"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_search_rejects_include_replies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        for idx, flag in enumerate((True, False)):
            body = await _call(
                bridge,
                400 + idx,
                {
                    "route": "x-timeline",
                    "operation": "search",
                    "query": "NVDA",
                    "include_replies": flag,
                },
            )
            assert body["ok"] is False
            assert body["status"] == "invalid_arguments"
        assert exec_mock.await_count == 0

        body_ok = await _call(bridge, 410, {"route": "x-timeline", "operation": "search", "query": "NVDA"})
        assert body_ok["ok"] is True
        assert "--replies" not in calls[0]  # xreach search has no --replies option

    @pytest.mark.asyncio
    async def test_params_start_end_rejected_and_timeout_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        # `params` is not accepted on x-timeline -> rejected with unknown_parameters
        body_params = await _call(
            bridge,
            500,
            {"route": "x-timeline", "handle": "nvidia", "params": {"offset": 0}},
        )
        assert body_params["ok"] is False
        assert body_params["status"] == "invalid_arguments"
        assert body_params["error_code"] == "unknown_parameters"
        assert exec_mock.await_count == 0

        # `start`/`end`/`limit` on x-timeline map to `since`/`until`/`count` (and `timeout`/`mode` are ignored)
        body = await _call(
            bridge,
            510,
            {
                "route": "x-timeline",
                "handle": "nvidia",
                "start": "2026-10-21",
                "end": "2026-10-21",
                "limit": 3,
                "timeout": 180,
                "mode": "fetch",
            },
        )
        assert body["ok"] is True, body
        assert body["data"]["returned"] == 3
        assert calls[0][3:5] == ["-n", "3"]
        assert body["normalized_params"]["since"] == "2026-10-21"
        assert body["normalized_params"]["until"] == "2026-10-21"
        assert body["normalized_params"]["count"] == 3


class TestXTimelineNormalization:
    @pytest.mark.asyncio
    async def test_fixture_normalized_truncated_and_attributed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fixture = _load_fixture()  # 15 items even though xreach was asked for -n 3 upstream
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(fixture)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        body = await _call(bridge, 7, {"route": "x-timeline", "handle": "nvidia", "count": 3})
        assert body["ok"] is True
        assert body["status"] == "ok"
        assert calls[0][3:5] == ["-n", "3"]  # bridge still passes -n count to xreach

        data = body["data"]
        assert len(data["items"]) == 3  # bridge truncates xreach's over-return to count
        assert data["returned"] == 3
        assert data["has_more"] is True

        first = data["items"][0]
        raw_first = fixture["items"][0]
        assert first["id"] == raw_first["id"]
        assert first["url"] == f"https://x.com/nvidia/status/{raw_first['id']}"
        assert first["author_handle"] == "nvidia"
        assert first["author_name"] == "NVIDIA"
        assert first["created_at"] == "2026-10-21T14:54:03+00:00"
        assert first["text"] == raw_first["text"]
        assert first["lang"] == "en"
        assert first["metrics"] == {"likes": 434, "reposts": 59, "replies": 68, "views": 58212}
        assert first["is_reply"] is False
        assert first["is_repost"] is False
        assert first["quoted_id"] is None
        assert first["source_attribution"] == f"**X**, @nvidia (2026-10-21; status {raw_first['id']})"
        assert "_date" not in first

        second = data["items"][1]
        assert second["is_repost"] is True  # RT @NVIDIAGTC item maps isRetweet -> is_repost

        srcs = body["_sources"]
        assert len(srcs) == 3
        assert srcs[0]["document_id"] == f"x-timeline:{raw_first['id']}"
        assert srcs[0]["route"] == "x-timeline"
        assert srcs[0]["platform"] == "x"
        assert srcs[0]["author"] == "@nvidia"
        assert srcs[0]["as_of"] == "2026-10-21"
        assert srcs[0]["url"] == f"https://x.com/nvidia/status/{raw_first['id']}"
        assert srcs[0]["source_type"] == "community_post"

    @pytest.mark.asyncio
    async def test_tweets_date_window_filters_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, _calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        body_in = await _call(
            bridge,
            8,
            {"route": "x-timeline", "handle": "nvidia", "since": "2026-10-21", "until": "2026-10-21", "count": 20},
        )
        assert body_in["ok"] is True
        assert body_in["data"]["returned"] == 15  # every fixture item is from 2026-10-21

        body_out = await _call(
            bridge, 9, {"route": "x-timeline", "handle": "nvidia", "until": "2026-10-20", "count": 20}
        )
        assert body_out["ok"] is True
        assert body_out["status"] == "empty"
        assert body_out["empty_reason"] == "no_matching_items"

    def test_source_identity_dedupes_by_status_id(self) -> None:
        item = _load_fixture()["items"][0]
        payload = {
            "ok": True,
            "status": "ok",
            "data": {"items": [item, dict(item)]},
        }
        declared = source_identity.declare("community", {"route": "x-timeline"}, payload)
        assert len(declared) == 1
        assert declared[0]["document_id"] == f"x-timeline:{item['id']}"

    @pytest.mark.asyncio
    async def test_single_tweet_output_shape_supported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        single = dict(_load_fixture()["items"][0])
        exec_mock, _calls = _xreach_exec_double(single)  # bare tweet object, no items wrapper
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        body = await _call(
            bridge, 10, {"route": "x-timeline", "operation": "tweet", "tweet_id": single["id"]}
        )
        assert body["ok"] is True
        assert body["data"]["returned"] == 1
        assert body["data"]["items"][0]["id"] == single["id"]


    @pytest.mark.asyncio
    async def test_proxy_flag_mirrors_bridge_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)  # no proxy configured
        exec_mock, calls = _xreach_exec_double(_load_fixture())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
        body = await _call(bridge, 20, {"route": "x-timeline", "handle": "nvidia"})
        assert body["ok"] is True
        assert "--proxy" not in calls[0]

        # https_proxy present -> explicit --proxy
        monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
        body_p = await _call(bridge, 21, {"route": "x-timeline", "handle": "nvidia"})
        assert body_p["ok"] is True
        assert calls[1][-2:] == ["--proxy", "http://127.0.0.1:7890"]

        # Credentials in the proxy URL -> never reaches argv, runs without --proxy
        monkeypatch.setenv("https_proxy", "http://user:secret@127.0.0.1:7890")
        body_c = await _call(bridge, 22, {"route": "x-timeline", "handle": "nvidia"})
        assert body_c["ok"] is True
        assert "--proxy" not in calls[2]
        assert not any("secret" in arg or "user:" in arg for arg in calls[2])

        # http_proxy is the secondary source when https_proxy is absent
        monkeypatch.delenv("https_proxy")
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.2:8080")
        body_h = await _call(bridge, 23, {"route": "x-timeline", "handle": "nvidia"})
        assert body_h["ok"] is True
        assert calls[3][-2:] == ["--proxy", "http://10.0.0.2:8080"]

    @pytest.mark.asyncio
    async def test_has_more_true_when_truncated_by_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        fixture_no_more = dict(_load_fixture())
        fixture_no_more["hasMore"] = False  # isolation: truncation alone must set has_more
        exec_mock, _calls = _xreach_exec_double(fixture_no_more)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        body_cut = await _call(bridge, 30, {"route": "x-timeline", "handle": "nvidia", "count": 5})
        assert len(body_cut["data"]["items"]) == 5  # 15 fixture items truncated
        assert body_cut["data"]["has_more"] is True

        body_full = await _call(bridge, 31, {"route": "x-timeline", "handle": "nvidia", "count": 20})
        assert len(body_full["data"]["items"]) == 15  # nothing truncated
        assert body_full["data"]["has_more"] is False  # upstream hasMore preserved

    @pytest.mark.asyncio
    async def test_exit_zero_with_429_error_json_returns_ok_false_and_is_error_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, _calls = _xreach_exec_double(
            {"ok": False, "error": "HTTP 429 Too Many Requests: rate limit exceeded"}
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        resp = await bridge.handle_tools_call(
            40,
            {
                "name": "community",
                "arguments": {"route": "x-timeline", "handle": "nvidia", "count": 5},
            },
        )
        assert resp["result"]["isError"] is True
        body = json.loads(resp["result"]["content"][0]["text"])
        assert body["ok"] is False
        assert "empty_reason" not in body
        assert body["error_category"] == "权限或额度"
        assert "429 Too Many Requests" in body["error"]
        assert body["hint"]

    @pytest.mark.asyncio
    async def test_exit_zero_with_status_rate_limited_only_returns_ok_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        # X exits 0 with ONLY {"status": "rate_limited"} (no ok, no error) -> must stay ok=False, isError=True
        exec_mock, _calls = _xreach_exec_double({"status": "rate_limited"})
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)
        resp_status = await bridge.handle_tools_call(
            41,
            {
                "name": "community",
                "arguments": {"route": "x-timeline", "handle": "nvidia", "count": 5},
            },
        )
        assert resp_status["result"]["isError"] is True
        body_status = json.loads(resp_status["result"]["content"][0]["text"])
        assert body_status["ok"] is False
        assert "empty_reason" not in body_status
        assert body_status["error_category"] == "权限或额度"
        assert "rate_limited" in body_status["error"]
        assert body_status["hint"]


class TestXreachVersionProbe:
    @pytest.mark.asyncio
    async def test_missing_binary_reported_without_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        bridge.cfg.xreach_bin = tmp_path / "missing-xreach"
        exec_mock, _calls = _xreach_exec_double(b"")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        out = await bridge.probe_xreach_version()
        assert out["ok"] is False
        assert out["status"] == "missing"
        assert exec_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_version_ok_and_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bridge = _make_bridge(tmp_path, monkeypatch)
        exec_mock, calls = _xreach_exec_double(b"xreach 1.4.2\n")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_mock)

        out1 = await bridge.probe_xreach_version()
        assert out1 == {"ok": True, "status": "ok", "version": "xreach 1.4.2"}
        assert calls[0][1:] == ["--version"]  # offline version check only

        out2 = await bridge.probe_xreach_version()
        assert out2 == out1
        assert exec_mock.await_count == 1  # cached, not re-spawned


class TestHealthProbeWindows:
    @staticmethod
    def _patch_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            health_mod,
            "resolve_researchdata_endpoint_and_token",
            create_autospec(
                health_mod.resolve_researchdata_endpoint_and_token,
                return_value=("http://127.0.0.1:8960/mcp", "tok", {"agent-reach"}),
            ),
        )

    @staticmethod
    def _autospec_probes() -> dict[str, Any]:
        return {
            "probe_web": create_autospec(
                health_mod.default_web_search_probe, return_value={"latest_date": None}
            ),
            "probe_x": create_autospec(
                health_mod.default_x_search_probe, return_value={"latest_date": None}
            ),
            "probe_market": create_autospec(
                health_mod.default_market_quote_probe, return_value={"latest_date": None}
            ),
            "probe_conv": create_autospec(
                health_mod.default_conversation_probe, return_value={"latest_date": None}
            ),
        }

    def test_probe_specs_windows_and_local_list(self) -> None:
        today = health_mod._probe_today()
        assert health_mod.WINDOWED_PROBE_ROUTE_IDS == frozenset(
            {"alphaengine", "guidepoint", "transcript", "sales-notes"}
        )
        for rid in health_mod.WINDOWED_PROBE_ROUTE_IDS:
            _tool, args = health_mod.build_route_probe_spec(rid, "research")
            assert "start" in args, f"{rid} missing start"
            assert "end" in args, f"{rid} missing end"
            expected_days = 120 if rid == "transcript" else 30
            assert args["start"] == (today - timedelta(days=expected_days)).isoformat()
            assert args["end"] == today.isoformat()

        _t, gp = health_mod.build_route_probe_spec("guidepoint", "research")
        assert gp["operation"] == "search_library"
        assert gp["limit"] == 3
        _t, gp_365 = health_mod.build_route_probe_spec("guidepoint", "research", window_days=365)
        assert gp_365["start"] == (today - timedelta(days=365)).isoformat()

        # Local libraries: upstream `list` is ORDER BY date DESC — newest rows first, no window.
        # sales-notes `list` does not return within the probe budget, so it keeps a windowed search.
        for rid in health_mod.LOCAL_LIBRARY_SUBROUTES:
            tool, args = health_mod.build_route_probe_spec(rid, "research")
            if rid == "sales-notes":
                assert args["operation"] == "search" and args["start"] and args["end"]
            else:
                assert (tool, args) == ("research", {"route": rid, "operation": "list", "limit": 3})

        _t, sec = health_mod.build_route_probe_spec("sec", "finance")
        assert "start" not in sec

        tool, xtl = health_mod.build_route_probe_spec("x-timeline", "community")
        assert tool == "community"
        assert xtl == {"route": "x-timeline", "operation": "tweets", "handle": "nvidia", "count": 3}

    def test_extract_latest_date_ignores_echoed_window_and_takes_max_item_date(self) -> None:
        payload = {
            "start": "2026-09-23",
            "end": "2026-10-23",
            "results": [{"publish_time": "2026-10-19"}, {"date": "2026-10-01"}, {"date": "2026-09-30"}],
        }
        assert health_mod.extract_latest_date(payload) == "2026-10-19"
        assert health_mod.extract_latest_date({"start": "2026-09-23", "end": "2026-10-23"}) is None

    @pytest.mark.asyncio
    async def test_window_expansion_when_30d_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections.abc import Mapping

        self._patch_endpoint(monkeypatch)
        research_calls: list[dict[str, Any]] = []

        async def _side_effect(
            tool_name: str, arguments: Mapping[str, Any], *, timeout_s: float = 45.0
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {"ok": True, "routes": [{"id": "guidepoint", "category": "research"}]}
            research_calls.append(dict(arguments))
            if len(research_calls) == 1:
                return {"ok": True, "status": "ok", "results": []}
            return {
                "ok": True,
                "status": "ok",
                "results": [{"id": "library_ts:gp-9", "date": "2026-04-15"}],
            }

        results, _catalog, _handoff = await health_mod._execute_probes(
            clamped_timeout=10.0,
            clamped_concurrency=2,
            call_bridge=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_side_effect),
            probe_sec_filings=None,
            **self._autospec_probes(),
        )
        assert len(research_calls) == 2
        today = health_mod._probe_today()
        assert research_calls[0]["start"] == (today - timedelta(days=30)).isoformat()
        assert research_calls[1]["start"] == (today - timedelta(days=365)).isoformat()
        gp_row = next(r for r in results if r["source_id"] == "guidepoint")
        assert gp_row["status"] == "available"
        assert gp_row["latest_date"] == "2026-04-15"
        assert gp_row["detail"]["window_days"] == 365

    @pytest.mark.asyncio
    async def test_no_expansion_when_30d_has_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections.abc import Mapping

        self._patch_endpoint(monkeypatch)
        research_calls: list[dict[str, Any]] = []

        async def _side_effect(
            tool_name: str, arguments: Mapping[str, Any], *, timeout_s: float = 45.0
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {"ok": True, "routes": [{"id": "guidepoint", "category": "research"}]}
            research_calls.append(dict(arguments))
            return {
                "ok": True,
                "status": "ok",
                "results": [{"id": "library_ts:gp-9", "date": "2026-10-19"}],
            }

        results, _catalog, _handoff = await health_mod._execute_probes(
            clamped_timeout=10.0,
            clamped_concurrency=2,
            call_bridge=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_side_effect),
            probe_sec_filings=None,
            **self._autospec_probes(),
        )
        assert len(research_calls) == 1
        gp_row = next(r for r in results if r["source_id"] == "guidepoint")
        assert gp_row["latest_date"] == "2026-10-19"
        assert "window_days" not in gp_row["detail"]  # only recorded when the widened window is adopted

    @pytest.mark.asyncio
    async def test_timeout_never_expands_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from collections.abc import Mapping

        self._patch_endpoint(monkeypatch)
        research_calls: list[dict[str, Any]] = []

        async def _side_effect(
            tool_name: str, arguments: Mapping[str, Any], *, timeout_s: float = 45.0
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {"ok": True, "routes": [{"id": "guidepoint", "category": "research"}]}
            research_calls.append(dict(arguments))
            raise asyncio.TimeoutError("slow upstream")

        results, _catalog, _handoff = await health_mod._execute_probes(
            clamped_timeout=10.0,
            clamped_concurrency=2,
            call_bridge=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_side_effect),
            probe_sec_filings=None,
            **self._autospec_probes(),
        )
        assert len(research_calls) == 1  # a timeout marks the route unavailable, never widens
        gp_row = next(r for r in results if r["source_id"] == "guidepoint")
        assert gp_row["status"] == "unavailable"
        assert gp_row["error_type"] == "TimeoutError"
        assert "window_days" not in gp_row["detail"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("order", [["guidepoint", "sec"], ["sec", "guidepoint"]])
    async def test_each_route_keeps_its_own_window_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch, order: list[str]
    ) -> None:
        """Loop variables (`windowed`, …) must bind per route, not to the last iteration."""
        from collections.abc import Mapping

        self._patch_endpoint(monkeypatch)
        calls: list[dict[str, Any]] = []

        async def _side_effect(
            tool_name: str, arguments: Mapping[str, Any], *, timeout_s: float = 45.0
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {
                    "ok": True,
                    "routes": [
                        (
                            {"id": "guidepoint", "category": "research"}
                            if r == "guidepoint"
                            else {"id": "sec", "category": "finance"}
                        )
                        for r in order
                    ],
                }
            args = dict(arguments)
            args["_timeout_s"] = timeout_s
            calls.append(args)
            if args.get("route") == "guidepoint":
                gp_seen = [c for c in calls if c.get("route") == "guidepoint"]
                if len(gp_seen) == 1:
                    return {"ok": True, "status": "ok", "results": []}  # empty 30d window
                return {
                    "ok": True,
                    "status": "ok",
                    "results": [{"id": "library_ts:gp-9", "date": "2026-04-15"}],
                }
            return {"ok": True, "status": "ok", "rows": [{"accession_no": "1"}]}  # sec: ok, no dates

        results, _catalog, _handoff = await health_mod._execute_probes(
            clamped_timeout=10.0,
            clamped_concurrency=2,
            call_bridge=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_side_effect),
            probe_sec_filings=None,
            **self._autospec_probes(),
        )

        gp_calls = [c for c in calls if c.get("route") == "guidepoint"]
        sec_calls = [c for c in calls if c.get("route") == "sec"]
        today = health_mod._probe_today()
        assert len(gp_calls) == 2  # windowed: empty 30d -> widened to 365
        assert gp_calls[1]["start"] == (today - timedelta(days=365)).isoformat()
        assert len(sec_calls) == 1  # non-windowed: never widened even with no dates
        assert all(c["_timeout_s"] == 10.0 for c in calls)  # every attempt gets the full budget

        gp_row = next(r for r in results if r["source_id"] == "guidepoint")
        assert gp_row["status"] == "available"
        assert gp_row["latest_date"] == "2026-04-15"
        assert gp_row["detail"]["window_days"] == 365
        sec_row = next(r for r in results if r["source_id"] == "sec")
        assert sec_row["status"] == "available"
        assert sec_row["latest_date"] is None
        assert "window_days" not in sec_row["detail"]

    @pytest.mark.asyncio
    async def test_all_route_aggregates_local_sublibraries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from collections.abc import Mapping

        self._patch_endpoint(monkeypatch)
        sub_dates = {
            "company-wiki": "2026-10-20",
            "sales-notes": "2026-10-22",
            "tmtb": "2026-10-18",
            "funda-ai": "2026-10-15",
        }
        research_calls: list[dict[str, Any]] = []

        async def _side_effect(
            tool_name: str, arguments: Mapping[str, Any], *, timeout_s: float = 45.0
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {
                    "ok": True,
                    "routes": [
                        {"id": "all", "category": "research"},
                        *({"id": sub, "category": "research"} for sub in sub_dates),
                    ],
                }
            args = dict(arguments)
            research_calls.append(args)
            rid = str(args.get("route"))
            return {
                "ok": True,
                "status": "ok",
                "results": [{"document_id": f"{rid}:1", "date": sub_dates[rid]}],
            }

        results, _catalog, _handoff = await health_mod._execute_probes(
            clamped_timeout=10.0,
            clamped_concurrency=2,
            call_bridge=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_side_effect),
            probe_sec_filings=None,
            **self._autospec_probes(),
        )

        # `all` never calls the bridge itself; freshness comes from the sublibrary rows.
        assert all(c.get("route") != "all" for c in research_calls)
        assert all(
            c.get("operation") == ("search" if c.get("route") == "sales-notes" else "list")
            for c in research_calls
        )
        all_row = next(r for r in results if r["source_id"] == "all")
        assert all_row["status"] == "available"
        assert all_row["latest_date"] == "2026-10-22"
        assert all_row["detail"]["subroute_latest"] == sub_dates
        assert all_row["detail"]["missing_sources"] == ["alphaengine", "guidepoint"]
        sn_row = next(r for r in results if r["source_id"] == "sales-notes")
        assert sn_row["latest_date"] == "2026-10-22"
        assert "window_days" not in sn_row["detail"]

    @pytest.mark.asyncio
    async def test_x_timeline_probe_latest_date_from_newest_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from collections.abc import Mapping

        self._patch_endpoint(monkeypatch)

        async def _side_effect(
            tool_name: str, arguments: Mapping[str, Any], *, timeout_s: float = 45.0
        ) -> dict[str, Any]:
            if tool_name == "catalog":
                return {"ok": True, "routes": [{"id": "x-timeline", "category": "community"}]}
            assert dict(arguments) == {
                "route": "x-timeline",
                "operation": "tweets",
                "handle": "nvidia",
                "count": 3,
            }
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "items": [
                        {"id": "1", "created_at": "2026-10-21T14:54:03+00:00"},
                        {"id": "2", "created_at": "2026-10-20T09:00:00+00:00"},
                    ]
                },
            }

        results, _catalog, _handoff = await health_mod._execute_probes(
            clamped_timeout=10.0,
            clamped_concurrency=2,
            call_bridge=create_autospec(health_mod.default_bridge_rpc_call, side_effect=_side_effect),
            probe_sec_filings=None,
            **self._autospec_probes(),
        )
        xtl_row = next(r for r in results if r["source_id"] == "x-timeline")
        assert xtl_row["status"] == "available"
        assert xtl_row["latest_date"] == "2026-10-21"
        assert "window_days" not in xtl_row["detail"]
