"""Full-stack integration sentinel tests for AU1 Public Share Redaction (§6.2.4 & B6/B7/B8).

Designed to be executed outside the sandbox by the lead against real PostgreSQL
(``TEST_DB_NAME=*_test``), real Redis, and (optionally) real MinIO/S3 buckets:

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \\
      uv run pytest -m integration tests/integration/dalton/test_share_redaction_e2e.py -v

Covers:
- S01–S19 strict share redaction through real FastAPI routes (``public_app.router`` including
  ``share_files_router`` and ``public_media_router``) across three distinct replay sources:
  1) warm ``turn_slices`` DB rows, 2) cold checkpoint projection (``project_turn``), and
  3) ``stub_lines`` / ``replay_facts.legacy.verbatim`` fallback.
- All 5 public ``/files*`` routes + public ``/media/*`` returning 403 + ``Cache-Control: no-store``
  + ``Clear-Site-Data: "cache"`` under conflicting ``{allow_files: false, allow_download: true}``.
- S19 unkeyed single-user ``oss`` remote/proxied boundary across owner read/file/stream endpoints,
  dev-fallback transcription ticket ``/result`` + ``/result/raw`` (with and without
  ``LANGALPHA_OWNER_SECRET``), and B8 Caddy proxy-header rejection.
- B6 physically-deleted-thread pagination through real ``GET /api/v1/activity`` and
  ``GET /api/v1/org-library/service`` FastAPI routes with multi-page cursors.
- B7 quantified cold/warm concurrent replay performance, real ``PageBudget`` soft-budget
  truncation (``min_turns=20`` over 35 turns + 250-turn ``history_page(has_more=true)``),
  strict ``tracemalloc`` heap assertions (``<= 96 MiB`` cold / ``<= 8 MiB`` warm),
  foreground ``persist=False == 1`` vs background ``persist=True == 1`` split counts,
  and ``allow_files=True`` pagination replay verification.
- Scheme A multi-bucket ``resolve_active_storage_targets()`` + strict 403 ``--verify``
  (with ``probe_write=True`` across S3 and OSS, rejecting 404 false passes).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import tracemalloc
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pathlib import Path
import pytest

from scripts.dalton.ops import privatize_shared_thread_objects as ops_script
from src.dalton import share_media, share_redaction
from src.dalton.app import transcriptions as asr_router_mod
from src.dalton.services import transcriptions as asr_svc
from src.dalton.share_redaction import DEFAULT_SHARED_TITLE
from src.server.app import activity as activity_app
from src.server.app import org_library as org_library_app
from src.server.app import public as public_app
from src.server.app import share_access
from src.server.app.threads import router as threads_router
from src.server.app.workspace_files import router as workspace_files_router
from src.server.auth import jwt_bearer, owner_requests, ws_auth
from src.server.database.conversation import replay_rows as replay_rows_mod
from src.server.database.conversation import turn_slices as slices_db
from src.server.services import activity as activity_svc
from src.server.services.history import replay as replay_mod
from src.server.services.history import slices as slices_mod
from src.server.services.history.reader import CheckpointHistoryReader, TurnAnchor
from src.server.services.history.replay import PageBudget
from src.server.services.history.replay.lines import encode as encode_lines
from src.server.services.history.replay.lines import line_of as _line_of
from src.server.services.preview_base import PreviewBase
from src.server.utils import api as api_utils


def line_of(event: str, data: dict[str, Any]) -> Any:
    return _line_of({"event": event, "data": data})


pytestmark = pytest.mark.integration


def _all_sentinels() -> tuple[str, ...]:
    return (
        "SECRET_TITLE_01",
        "SECRET_AUTO_LIB_01",
        "SECRET_COMMENT_TITLE_01",
        "grant_lib_summary_01",
        "SECRET_USER_FILE_02",
        "SECRET_STEER_FILE_02",
        "SECRET_LOCAL_IMG_03",
        "SECRET_ANNOT_04",
        "SECRET_WIDGET_SNAP_05",
        "SECRET_WAKE_BODY_06",
        "SECRET_ALERT_BODY_06",
        "SECRET_WAKE_LABEL_06",
        "SECRET_WAKE_SYM_06",
        "SECRET_ORD_06",
        "SECRET_TICKER_06",
        "SECRET_SUB_INPUT_07",
        "SECRET_SUB_OUT_07",
        "SECRET_SUBAGENT_FALLBACK_07",
        "SECRET_COT_ADJ_08",
        "SECRET_COT_NESTED_OUTER_08",
        "SECRET_COT_MID_08",
        "SECRET_COT_LONG_08",
        "SECRET_COT_TAIL_08",
        "SECRET_COT_NEED_08",
        "SECRET_COT_NEED_AFTER_CODE_08",
        "SECRET_COMPACT_08",
        "SECRET_PRETOOL_09",
        "SECRET_COMMENTARY_09",
        "SECRET_OLD_PRETOOL_09",
        "SECRET_FAILED_ARCHIVE_09",
        "SECRET_FAILED_PROJ_09",
        "SECRET_STOPPED_TAIL_09",
        "SECRET_TOOL_10",
        "SECRET_LAYER_10",
        "SECRET_WIDGET_TITLE_11",
        "SECRET_WIDGET_HTML_11",
        "SECRET_WIDGET_DATA_11",
        "SECRET_WIDGET_REF_12",
        "SECRET_ALT_URL_13.png",
        "SECRET_CDN_IMG_13.png",
        "SECRET_MERMAID_IMG_13.png",
        "SECRET_REF_IMG_14.png",
        "SECRET_IMG_TITLE_14",
        "SECRET_CSS_URL_15.png",
        "SECRET_FO_15.png",
        "SECRET_LINK_TITLE_16",
        "SECRET_REF_TITLE_16",
        "SECRET_UNUSED_DEF_16",
        "SECRET_HTML_A_TITLE_16",
        "SECRET_VERBATIM_17",
        "SECRET_FENCE_INFO_18.py",
        "SECRET_CODE_URL_18",
        "SECRET_PROV_INLINE_19",
        "SECRET_PROV_SPILL_19",
        "SECRET_RAW_TITLE_19",
        "SECRET_WS_FILE_19",
        "SECRET_LIVE_STREAM_19",
        "SECRET_ASR_TRANSCRIPT_19",
        "SECRET_ASR_RAW_19",
    )


def _build_final_answer_markdown(
    *,
    thread_id: str,
    grant_16: str,
    cap_token_16: str,
    mon_grant_16: str,
    pub_code_16: str,
    media_token_19: str,
    preview: PreviewBase,
) -> str:
    art_id = str(uuid.uuid4())
    art_url = preview.artifact_url(art_id, grant_16) + "v/1/report.html"
    cap_url = preview.url(8080, cap_token_16)
    api_grant_url = f"https://app.local/api/v1/artifacts/g/{grant_16}/v/1/report.html"
    mon_grant_url = f"https://app.local/api/v1/dalton/monitors/g/{mon_grant_16}/board.html"
    owner_media_url = f"https://app.local/api/v1/threads/{thread_id}/media/{media_token_19}"
    pub_short_url = f"https://app.local/a/{pub_code_16}"
    long_prefix = "背景结论 " * 600
    return (
        f"{long_prefix}"
        "<thinking>SECRET_COT_MID_08</thinking>"
        "<thinking_mode>SECRET_COT_LONG_08</thinking_mode>"
        "NEED MORE THINKING\nInspect `SECRET_COT_NEED_08.py` and SECRET_COT_NEED_AFTER_CODE_08.\n\n"
        "正式结论汇总，详见 reports/q3.pdf、reports/财报.pdf、reports/README 与 `out/memo.md`。\n\n"
        f"查看 [报告]({art_url} \"SECRET_LINK_TITLE_16\")、"
        f"[API]({api_grant_url})、[Cap]({cap_url})、[Mon]({mon_grant_url})、"
        f"[媒体]({owner_media_url}) 与 [公开短链]({pub_short_url}).\n\n"
        "![https://cdn.custom-domain.example/charts/SECRET_ALT_URL_13.png]"
        "(https://cdn.custom-domain.example/charts/SECRET_CDN_IMG_13.png)\n\n"
        "![矩阵图][ref14]\n\n"
        "[ref14]: https://cdn.custom-domain.example/SECRET_REF_IMG_14.png \"SECRET_IMG_TITLE_14\"\n"
        "[unused]: https://example.com/ok \"SECRET_UNUSED_DEF_16\"\n\n"
        r"\![escaped](https://cdn.custom-domain.example/SECRET_CDN_IMG_13.png)" "\n\n"
        "```mermaid\n"
        "A[\"<img src='https://cdn.custom-domain.example/charts/SECRET_MERMAID_IMG_13.png'/>\"] --> B[\"结论\"]\n"
        "```\n\n"
        "```python title=\"/Users/secret_alice_01/ws/SECRET_FENCE_INFO_18.py\"\n"
        "x = \"https://cdn.example/blobs/u1/SECRET_CODE_URL_18\"\n"
        "```\n\n"
        "<span style=\"background-image: url('https://cdn.custom-domain.example/SECRET_CSS_URL_15.png')\">SafeSpan</span> "
        "<a href=\"https://example.com/ok\" title=\"SECRET_HTML_A_TITLE_16\">SafeA</a> "
        "<svg><foreignObject><img src=\"https://cdn.custom-domain.example/SECRET_FO_15.png\"/></foreignObject></svg>"
        "<thinking>SECRET_COT_TAIL_08"
    )


def _build_warm_slice_lines(
    *, thread_id: str, final_answer_md: str, api_grant_url: str
) -> list[Any]:
    return [
        line_of("replay_turn", {"turn_index": 0, "status": "completed", "response_id": "r0", "open_runs": []}),
        line_of(
            "user_message",
            {
                "turn_index": 0,
                "content": f"请总结核心结论\n<file-comments>SECRET_ANNOT_04</file-comments>\n参考 {api_grant_url}",
                "metadata": {
                    "attachments": [
                        {"filename": "SECRET_USER_FILE_02.pdf", "url": "https://cdn.example/attachments/u/a.pdf"}
                    ],
                    "widget_contexts": [{"html": "SECRET_WIDGET_SNAP_05"}],
                },
            },
        ),
        line_of(
            "steering_delivered",
            {
                "turn_index": 0,
                "messages": [
                    {
                        "content": "补充口径\n<file-comments>SECRET_ANNOT_04</file-comments>",
                        "attachments": [{"filename": "SECRET_STEER_FILE_02.csv"}],
                    },
                    {
                        "content": "SECRET_WAKE_BODY_06",
                        "wakeup": {
                            "v": 1,
                            "notices": [
                                {
                                    "kind": "cron",
                                    "label": "SECRET_WAKE_LABEL_06",
                                    "cron": "17 4 * * 3",
                                    "symbol": "SECRET_WAKE_SYM_06",
                                    "target": 987.65,
                                    "price": 988.12,
                                    "order_id": "SECRET_ORD_06",
                                }
                            ],
                        },
                    },
                    {
                        "content": "SECRET_ALERT_BODY_06",
                        "market_alert": {
                            "target_id": "mt-1",
                            "kind": "price",
                            "notice": "hit",
                            "symbol": "SECRET_TICKER_06",
                            "price": 950.01,
                            "label": "SECRET_WAKE_LABEL_06",
                        },
                    },
                ],
            },
        ),
        line_of("user_message", {"turn_index": 0, "agent": "task:sub1", "content": "SECRET_SUB_INPUT_07"}),
        line_of("tool_call_result", {"turn_index": 0, "agent": "task:sub1", "content": "SECRET_SUB_OUT_07"}),
        line_of(
            "message_chunk",
            {
                "turn_index": 0,
                "agent": "subagent:call_07",
                "role": "assistant",
                "content_type": "text",
                "content": "SECRET_SUBAGENT_FALLBACK_07",
            },
        ),
        line_of(
            "message_chunk",
            {
                "turn_index": 0,
                "agent": "main",
                "role": "assistant",
                "content_type": "text",
                "phase": "commentary",
                "content": "SECRET_COMMENTARY_09",
            },
        ),
        line_of(
            "message_chunk",
            {
                "turn_index": 0,
                "agent": "main",
                "role": "assistant",
                "content_type": "text",
                "content": "SECRET_PRETOOL_09",
            },
        ),
        line_of("tool_calls", {"turn_index": 0, "agent": "main", "tool_calls": [{"name": "Read", "args": {"path": "SECRET_TOOL_10"}}]}),
        line_of(
            "tool_call_result",
            {
                "turn_index": 0,
                "agent": "main",
                "content": "SECRET_TOOL_10",
                "artifact": {
                    "type": "html_widget",
                    "title": "SECRET_WIDGET_TITLE_11",
                    "html": "<div>SECRET_WIDGET_HTML_11</div>",
                    "data": {"k": "SECRET_WIDGET_DATA_11"},
                    "data_ref": {"key": "widgets/legacy_12.json"},
                    "chart_layers": [{"label": "SECRET_LAYER_10"}],
                },
            },
        ),
        line_of(
            "message_chunk",
            {
                "turn_index": 0,
                "agent": "main",
                "role": "assistant",
                "content_type": "reasoning",
                "content": "SECRET_COT_ADJ_08",
            },
        ),
        line_of(
            "message_chunk",
            {
                "turn_index": 0,
                "agent": "main",
                "role": "assistant",
                "content_type": "text",
                "phase": "final_answer",
                "content": final_answer_md,
            },
        ),
        line_of("context_window", {"turn_index": 0, "summary_text": "SECRET_COMPACT_08"}),
        line_of("credit_usage", {"turn_index": 0, "total_tokens": 12345}),
        line_of("legacy_debug", {"turn_index": 0, "dump": "SECRET_VERBATIM_17"}),
        line_of("replay_turn", {"turn_index": 1, "status": "error", "response_id": "r1"}),
        line_of("user_message", {"turn_index": 1, "content": "失败回合测试"}),
        line_of(
            "message_chunk",
            {
                "turn_index": 1,
                "agent": "main",
                "role": "assistant",
                "content_type": "text",
                "content": "SECRET_FAILED_PROJ_09 SECRET_FAILED_ARCHIVE_09",
            },
        ),
        line_of("replay_turn", {"turn_index": 2, "status": "cancelled", "response_id": "r2"}),
        line_of("user_message", {"turn_index": 2, "content": "中止回合测试"}),
        line_of(
            "message_chunk",
            {
                "turn_index": 2,
                "agent": "main",
                "role": "assistant",
                "content_type": "text",
                "content": "SECRET_STOPPED_TAIL_09",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_e2e_s01_to_s19_strict_share_redaction_and_three_replay_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify S01–S19 zero-leakage via REAL public replay/files/media routes and REAL cold/warm/stub selection."""
    thread_id = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    r0_id = str(uuid.uuid4())
    r1_id = str(uuid.uuid4())
    r2_id = str(uuid.uuid4())
    share_token = f"tok-{uuid.uuid4().hex[:16]}"
    preview = PreviewBase("https", "preview.local")
    monkeypatch.setattr("src.server.services.preview_base.preview_base", lambda: preview)
    monkeypatch.setattr("src.server.services.preview_base.artifact_base", lambda: preview)

    grant_16 = "v1.grant_e2e_secret_16"
    cap_token_16 = "b2345672345672345672345672345672"
    mon_grant_16 = "v1.mon_e2e_secret_16"
    pub_code_16 = "PubCode16AbCd"
    media_token_19 = "med_e2e_secret_19"
    api_grant_url = f"https://app.local/api/v1/artifacts/g/{grant_16}/v/1/report.html"

    final_answer_md = _build_final_answer_markdown(
        thread_id=thread_id,
        grant_16=grant_16,
        cap_token_16=cap_token_16,
        mon_grant_16=mon_grant_16,
        pub_code_16=pub_code_16,
        media_token_19=media_token_19,
        preview=preview,
    )
    nested_opening_final_md = (
        "<thinking>outer_open <thinking>inner_open</thinking> "
        "SECRET_COT_NESTED_OUTER_08</thinking>" + final_answer_md
    )

    fake_thread = {
        "conversation_thread_id": thread_id,
        "workspace_id": ws_id,
        "user_id": "u1",
        "owner_id": "u1",
        "title": "SECRET_TITLE_01",
        "share_token": share_token,
        "is_shared": True,
        "share_permissions": {"allow_files": False, "allow_download": True},
        "root_dir": "/Users/secret_alice_01/ws",
        "previous_root_dirs": ["/srv/old_alice_root"],
        "latest_checkpoint_id": "cp-tail-0",
    }
    responses_by_turn = {
        0: {
            "turn_index": 0,
            "status": "completed",
            "conversation_response_id": r0_id,
            "replay_facts": {
                "archive": [
                    {"event": "legacy_debug", "data": {"dump": "SECRET_VERBATIM_17"}},
                    {"event": "message_chunk", "data": {"agent": "subagent:call_07", "role": "assistant", "content_type": "text", "content": "SECRET_SUBAGENT_FALLBACK_07"}},
                    {"event": "message_chunk", "data": {"agent": "main", "role": "assistant", "content_type": "text", "content": nested_opening_final_md}},
                ],
            },
        },
        1: {
            "turn_index": 1,
            "status": "error",
            "conversation_response_id": r1_id,
            "replay_facts": {"partial": {"content": "SECRET_FAILED_ARCHIVE_09"}},
        },
        2: {
            "turn_index": 2,
            "status": "cancelled",
            "conversation_response_id": r2_id,
            "replay_facts": {"partial": {"content": "SECRET_STOPPED_TAIL_09"}},
        },
    }
    queries = [
        {
            "conversation_query_id": str(uuid.uuid4()),
            "turn_index": 0,
            "type": "initial",
            "content": "请总结核心结论\n<file-comments>SECRET_ANNOT_04</file-comments>",
            "metadata": {
                "attachments": [{"filename": "SECRET_USER_FILE_02.pdf", "url": "https://cdn.example/attachments/u/a.pdf"}],
                "widget_contexts": [{"html": "SECRET_WIDGET_SNAP_05"}],
            },
        },
        {
            "conversation_query_id": str(uuid.uuid4()),
            "turn_index": 1,
            "type": "follow_up",
            "content": "失败回合测试",
            "metadata": {},
        },
        {
            "conversation_query_id": str(uuid.uuid4()),
            "turn_index": 2,
            "type": "follow_up",
            "content": "中止回合测试",
            "metadata": {},
        },
    ]

    # Real TurnAnchors & TurnSlices for cold checkpoint projection (NOT mocking read_strict_public_replay_page!)
    anchors = [
        TurnAnchor(
            turn_ordinal=0,
            input_checkpoint_id="cp-in-0",
            tail_checkpoint_id="cp-tail-0",
            turn_index=0,
            end_checkpoint_id="cp-tail-0",
        )
    ]
    ai_mixed = AIMessage(
        id="ai-0",
        content=[
            {"type": "thinking", "thinking": "SECRET_COT_ADJ_08"},
            {"type": "text", "text": "SECRET_COMMENTARY_09", "phase": "commentary"},
            {"type": "text", "text": nested_opening_final_md, "phase": "final_answer"},
        ],
    )
    ai_tool = AIMessage(
        id="ai-pre",
        content="SECRET_PRETOOL_09",
        tool_calls=[{"id": "tc-1", "name": "Read", "args": {"path": "SECRET_TOOL_10"}}],
    )
    tool_msg = ToolMessage(id="tm-1", tool_call_id="tc-1", name="Read", content="SECRET_TOOL_10")
    cold_slice = slices_mod.TurnSlice(
        anchor=anchors[0],
        messages=[HumanMessage(id="h-0", content="请总结核心结论"), ai_tool, tool_msg, ai_mixed],
    )

    reader_stub = create_autospec(CheckpointHistoryReader, instance=True)
    reader_stub.serde = JsonPlusSerializer()
    reader_stub.aget_turn_anchors.return_value = (anchors, "cp-tail-0")
    reader_stub.aget_turn_slices.return_value = [cold_slice]
    reader_stub.aget_tip_interrupts.return_value = []
    monkeypatch.setattr(CheckpointHistoryReader, "get_instance", classmethod(lambda cls: reader_stub))

    stored_slices: dict[str, slices_db.StoredSlice] = {}

    async def _get_turn_lines(rids: list[str]) -> dict[str, slices_db.StoredSlice]:
        return {r: stored_slices[r] for r in rids if r in stored_slices}

    async def _get_turn_slices(rids: list[str]) -> dict[str, slices_db.StoredSlice]:
        return {r: stored_slices[r] for r in rids if r in stored_slices}

    async def _upsert_turn_slices(turn_rows: list[slices_db.TurnRow]) -> None:
        for tr in turn_rows:
            stored_slices[tr.response_id] = tr.slice

    async def _update_turns(updates: list[slices_db.TurnUpdate]) -> None:
        for u in updates:
            cur = stored_slices.get(u.response_id)
            if cur is not None:
                stored_slices[u.response_id] = cur._replace(
                    lines_key=u.lines_key or cur.lines_key,
                    lines=u.lines or cur.lines,
                )

    monkeypatch.setattr(slices_db, "get_turn_lines", create_autospec(slices_db.get_turn_lines, side_effect=_get_turn_lines))
    monkeypatch.setattr(slices_db, "get_turn_slices", create_autospec(slices_db.get_turn_slices, side_effect=_get_turn_slices))
    monkeypatch.setattr(slices_db, "upsert_turn_slices", create_autospec(slices_db.upsert_turn_slices, side_effect=_upsert_turn_slices))
    monkeypatch.setattr(slices_db, "update_turns", create_autospec(slices_db.update_turns, side_effect=_update_turns))

    active_rows = replay_rows_mod.ThreadRows(
        thread_id=thread_id,
        owner_id="u1",
        thread=fake_thread,
        queries=[queries[0]],
        responses_by_turn={0: responses_by_turn[0]},
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=[],
    )
    current_rows_box = [active_rows]

    app = FastAPI()
    app.include_router(public_app.router)
    client = TestClient(app)

    monkeypatch.setattr(share_access, "get_shared_thread", create_autospec(share_access.get_shared_thread, return_value=fake_thread))
    monkeypatch.setattr(public_app, "get_shared_thread", create_autospec(public_app.get_shared_thread, return_value=fake_thread))
    monkeypatch.setattr(
        public_app,
        "get_replay_thread_data",
        create_autospec(public_app.get_replay_thread_data, side_effect=lambda _tid: current_rows_box[0]),
    )
    # Prevent background warm task from mutating stored_slices before our explicit cold assertion
    warm_sched_spy = create_autospec(share_redaction.schedule_warm_shared_thread_slices, return_value=None)
    monkeypatch.setattr(share_redaction, "schedule_warm_shared_thread_slices", warm_sched_spy)

    forbidden_sentinels = _all_sentinels() + (
        grant_16,
        cap_token_16,
        mon_grant_16,
        media_token_19,
        "reports/q3.pdf",
        "reports/财报.pdf",
        "reports/README",
        "out/memo.md",
    )

    def _assert_clean_sse(source_label: str, resp: Any) -> None:
        assert resp.status_code == 200, f"[{source_label}] replay returned {resp.status_code}"
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("clear-site-data") == '"cache"'
        body = resp.text
        for sentinel in forbidden_sentinels:
            assert sentinel not in body, f"[{source_label}] leaked sentinel {sentinel!r}"
        assert f"/a/{pub_code_16}" in body
        assert "<redacted-image" in body
        assert "```mermaid" not in body

    # 1. REAL cold checkpoint projection (turn_slices empty -> cold.project_stale runs with persist=False)
    r_cold = client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert reader_stub.aget_turn_slices.await_count == 1
    assert warm_sched_spy.call_count == 1  # only fires when cold.project_stale ran
    assert r0_id not in stored_slices  # foreground read_strict_public_replay_page uses persist=False
    _assert_clean_sse("real-cold-checkpoint", r_cold)

    # 2. REAL warm turn_slices hit after persist=True warm pass (zero cold projection on replay)
    await replay_mod.read_replay_page(active_rows, persist=True)
    assert r0_id in stored_slices
    assert "SECRET_COT_NESTED_OUTER_08" not in (stored_slices[r0_id].lines or "")
    slice_reads_before_warm = reader_stub.aget_turn_slices.await_count
    warm_sched_spy.reset_mock()
    r_warm = client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert warm_sched_spy.call_count == 0  # warm cache hit -> no cold miss, no warm scheduled
    assert reader_stub.aget_turn_slices.await_count == slice_reads_before_warm
    _assert_clean_sse("real-warm-turn-slices", r_warm)

    # 2b. Full S01-S18 multi-turn warm slice (steering/subagents/widgets/error/cancelled/legacy_debug)
    full_warm_lines = _build_warm_slice_lines(
        thread_id=thread_id, final_answer_md=final_answer_md, api_grant_url=api_grant_url
    )
    stored_slices[r0_id] = stored_slices[r0_id]._replace(
        lines=encode_lines(full_warm_lines),
    )
    r_warm_full = client.get(f"/api/v1/public/shared/{share_token}/replay")
    assert warm_sched_spy.call_count == 0
    _assert_clean_sse("real-warm-full-s01-s18", r_warm_full)

    # 3. REAL stub fallback (no committed latest_checkpoint_id -> read_replay_page falls back to stub_page)
    stub_thread = {**fake_thread, "latest_checkpoint_id": None}
    current_rows_box[0] = replay_rows_mod.ThreadRows(
        thread_id=thread_id,
        owner_id="u1",
        thread=stub_thread,
        queries=queries,
        responses_by_turn=responses_by_turn,
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=[],
    )
    r_stub = client.get(f"/api/v1/public/shared/{share_token}/replay")
    _assert_clean_sse("real-stub-fallback", r_stub)

    # Verify all 5 public /files* routes + PDF export + public /media/* fail closed with 403 + no-store + Clear-Site-Data
    media_ref = share_media.mint_public_media_ref(
        thread_id=thread_id,
        backend="upstream",
        ref_id=f"private-media/response-images/{thread_id}/abc/chart.png",
    )
    for method, url in (
        ("GET", f"/api/v1/public/shared/{share_token}/files"),
        ("POST", f"/api/v1/public/shared/{share_token}/files/resolve"),
        ("GET", f"/api/v1/public/shared/{share_token}/files/read?path=report.md"),
        ("GET", f"/api/v1/public/shared/{share_token}/files/download?path=report.md"),
        ("GET", f"/api/v1/public/shared/{share_token}/files/serve/report.html"),
        ("GET", f"/api/v1/public/shared/{share_token}/files/serve/report.html?format=pdf"),
        ("GET", f"/api/v1/public/shared/{share_token}/media/{media_ref}"),
    ):
        resp = client.post(url, json={"candidates": ["report.md"]}) if method == "POST" else client.get(url)
        assert resp.status_code == 403, f"Expected 403 for {method} {url}, got {resp.status_code}"
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("clear-site-data") == '"cache"'


@pytest.mark.asyncio
@pytest.mark.parametrize("with_owner_secret", [False, True])
async def test_e2e_s19_unkeyed_oss_remote_and_dev_fallback_transcription_ticket_boundary(
    monkeypatch: pytest.MonkeyPatch,
    with_owner_secret: bool,
) -> None:
    """S19 + B8: Remote/proxied unkeyed oss REAL owner routers & dev-fallback transcription tickets return 401."""
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
    monkeypatch.setattr(ws_auth, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.delenv("DALTON_TRANSCRIPTION_TICKET_SECRET", raising=False)
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    if with_owner_secret:
        monkeypatch.setenv(owner_requests.OWNER_SECRET_ENV, "s19-configured-owner-secret")
    else:
        monkeypatch.delenv(owner_requests.OWNER_SECRET_ENV, raising=False)

    # Verify real Caddy edge configuration strips internal identity headers on all proxied routes
    caddyfile = Path(__file__).resolve().parents[3] / "deploy" / "standalone" / "Caddyfile"
    caddy_text = caddyfile.read_text(encoding="utf-8")
    assert "request_header -X-Service-Token" in caddy_text
    assert "request_header -X-User-Id" in caddy_text
    assert "header_up -X-Service-Token" in caddy_text
    assert "header_up -X-User-Id" in caddy_text
    assert "/api/v1/threads/[^/]+/messages/replay" in caddy_text

    job_id = str(uuid.uuid4())
    dev_ticket, _ = asr_svc.issue_transcription_ticket(
        job_id=job_id,
        user_id="local-dev-user",
        workspace_id=str(uuid.uuid4()),
        thread_id=str(uuid.uuid4()),
        source_object_key="transcriptions/ws/job/source/audio.wav",
        source_size_bytes=512,
        source_sha256="b" * 64,
    )

    monkeypatch.setattr(asr_router_mod, "is_transcription_enabled", lambda: True)
    monkeypatch.setattr(
        asr_router_mod,
        "admit_caller",
        create_autospec(asr_router_mod.admit_caller, return_value=None),
    )
    monkeypatch.setattr(
        asr_router_mod,
        "get_dalton_settings",
        lambda: SimpleNamespace(enabled=True),
    )

    # Mount REAL production routers (no hand-written stub endpoints)
    app = FastAPI()
    app.include_router(threads_router)
    app.include_router(workspace_files_router)
    app.include_router(asr_router_mod.router)
    app.include_router(share_media.owner_media_router)

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    rec_id = str(uuid.uuid4())
    # Caddy proxy headers (B8) + remote client IP
    proxied_headers = {"X-Forwarded-For": "203.0.113.19", "Host": "app.example.com"}

    transport = httpx.ASGITransport(app=app, client=("172.31.250.2", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="https://app.example.com") as client:
        for path in (
            f"/api/v1/threads/{tid}/provenance/bodies",
            f"/api/v1/threads/{tid}/provenance/{rec_id}/body?full=true",
            f"/api/v1/threads/{tid}",
            f"/api/v1/workspaces/{ws_id}/files/download-url?path=secret_19.txt",
            f"/api/v1/threads/{tid}/messages/stream",
            f"/api/v1/threads/{tid}/messages/replay",
        ):
            r = await client.get(path, headers=proxied_headers)
            assert r.status_code == 401, f"Expected 401 for {path}, got {r.status_code}"
            assert "set-cookie" not in r.headers

        # Dev-fallback transcription ticket rejected remotely even when LANGALPHA_OWNER_SECRET is set
        for path in (
            f"/api/v1/dalton/transcriptions/{job_id}/result",
            f"/api/v1/dalton/transcriptions/{job_id}/result/raw",
        ):
            r = await client.get(
                path,
                headers={**proxied_headers, "X-Dalton-Transcription-Ticket": dev_ticket},
            )
            assert r.status_code == 401, f"Expected 401 for {path}, got {r.status_code}"


@pytest.mark.asyncio
async def test_e2e_b6_deleted_thread_org_library_and_activity_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
    test_db_pool: Any,
    dalton_schema: Any,
    patched_get_db_connection: Any,
    seed_user: dict[str, Any],
    seed_workspace: dict[str, Any],
) -> None:
    """B6: Real DB migrations (upstream + Dalton d059), real thread insert & delete, and real SQL multi-page cursor pagination."""
    from datetime import timedelta
    from src.server.database import org_library as org_library_db

    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN ('dalton_org_library_title_meta', 'dalton_storage_key_migrations')
                ORDER BY table_name
                """
            )
            rows_tbl = await cur.fetchall()
            tables = [
                row["table_name"] if isinstance(row, dict) else row[0]
                for row in rows_tbl
            ]
    assert tables == ["dalton_org_library_title_meta", "dalton_storage_key_migrations"]

    user_id = str(seed_user["user_id"])
    workspace_id = str(seed_workspace["workspace_id"])
    org_id = f"org-b6-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    kept_tids = [str(uuid.uuid4()) for _ in range(3)]
    deleted_tid = str(uuid.uuid4())
    all_tids = [*kept_tids, deleted_tid]
    published_item_ids: dict[str, str] = {}

    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            for idx, tid in enumerate(all_tids):
                tok = f"b6tok{idx}_{uuid.uuid4().hex[:8]}"
                await cur.execute(
                    """
                    INSERT INTO conversation_threads
                        (conversation_thread_id, workspace_id, user_id, current_status, thread_index,
                         title, is_shared, share_token, share_permissions, shared_at)
                    VALUES (%s::uuid, %s::uuid, %s, 'completed', %s, %s, TRUE, %s, %s::jsonb, NOW())
                    """,
                    (
                        tid,
                        workspace_id,
                        user_id,
                        idx + 1,
                        f"SECRET_AUTO_LIB_{idx}",
                        tok,
                        json.dumps({"allow_files": False, "allow_download": False, "audience": "public"}),
                    ),
                )

    for idx, tid in enumerate(all_tids):
        published = await org_library_db.publish(
            org_id=org_id,
            publisher_id=user_id,
            kind="thread",
            target_id=tid,
            title=None,
            summary=f"Check /api/v1/artifacts/g/v1.grant_lib_summary_{idx}/v/1/a.html reports/财报.pdf reports/README",
            owner_id=user_id,
        )
        assert published is not None
        item_id, _created = published
        published_item_ids[tid] = item_id
        await share_redaction.upsert_org_library_title_meta(item_id, custom_title=False)

    # Spread published_at timestamps deterministically and insert settled (>60s old) activity_events rows.
    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            for idx, tid in enumerate(all_tids):
                await cur.execute(
                    """
                    UPDATE org_library_items
                    SET published_at = %s
                    WHERE item_id = %s::uuid
                    """,
                    (now - timedelta(minutes=10 - idx), published_item_ids[tid]),
                )

            # Insert 3 activity events older than SETTLE_S (60s) so real list_for_org returns them across 2+ pages.
            act_rows = [
                (
                    now - timedelta(minutes=8),
                    activity_svc.Action.LIBRARY_PUBLISHED.value,
                    "library_item",
                    published_item_ids[deleted_tid],
                    None,
                    deleted_tid,
                    {"kind": "thread", "title": "SECRET_DELETED_THREAD_TITLE", "custom_title": False},
                ),
                (
                    now - timedelta(minutes=7),
                    activity_svc.Action.LIBRARY_PUBLISHED.value,
                    "library_item",
                    published_item_ids[kept_tids[0]],
                    workspace_id,
                    kept_tids[0],
                    {"kind": "thread", "title": "SECRET_AUTO_LIB_0", "custom_title": False},
                ),
                (
                    now - timedelta(minutes=6),
                    activity_svc.Action.FILE_PUBLISHED.value,
                    "artifact",
                    str(uuid.uuid4()),
                    workspace_id,
                    None,
                    {"path": "reports/财报.pdf", "version": 1},
                ),
            ]
            for occurred_at, action_val, res_kind, res_id, ws_col, t_col, detail_obj in act_rows:
                await cur.execute(
                    """
                    INSERT INTO activity_events
                        (occurred_at, org_id, actor_id, action, outcome,
                         resource_kind, resource_id, workspace_id, thread_id, detail)
                    VALUES (%s, %s, %s, %s, 'done', %s, %s, %s::uuid, %s::uuid, %s::jsonb)
                    """,
                    (
                        occurred_at,
                        org_id,
                        user_id,
                        action_val,
                        res_kind,
                        res_id,
                        ws_col,
                        t_col,
                        json.dumps(detail_obj),
                    ),
                )

            # Physically delete one thread (cascades its org_library_items row; activity_events row remains).
            await cur.execute(
                "DELETE FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (deleted_tid,),
            )

    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", "s" * 40)

    app = FastAPI()
    app.include_router(org_library_app.router)
    app.include_router(activity_app.router)
    svc_headers = {"X-Service-Token": "s" * 40}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Real SQL pagination on GET /api/v1/org-library/service (limit=2 -> 2 pages for 3 kept items)
        p1 = await client.get(
            f"/api/v1/org-library/service?org_id={org_id}&limit=2",
            headers=svc_headers,
        )
        assert p1.status_code == 200
        b1 = p1.json()
        assert len(b1["items"]) == 2
        assert b1["next_cursor"] is not None

        p2 = await client.get(
            f"/api/v1/org-library/service?org_id={org_id}&limit=2&cursor={b1['next_cursor']}",
            headers=svc_headers,
        )
        assert p2.status_code == 200
        b2 = p2.json()
        assert len(b2["items"]) == 1
        assert b2["next_cursor"] is None

        all_lib_items = [*b1["items"], *b2["items"]]
        assert len(all_lib_items) == 3
        assert {it["thread_id"] for it in all_lib_items} == set(kept_tids)
        assert deleted_tid not in {it["thread_id"] for it in all_lib_items}
        assert all(it["title"] == DEFAULT_SHARED_TITLE for it in all_lib_items)
        combined_lib_text = p1.text + p2.text
        assert "grant_lib_summary_" not in combined_lib_text
        assert "reports/财报.pdf" not in combined_lib_text
        assert "reports/README" not in combined_lib_text
        assert "SECRET_AUTO_LIB_" not in combined_lib_text

        # 2. Real SQL pagination on GET /api/v1/activity (limit=2 -> 2 pages for 3 settled events)
        a1 = await client.get(
            f"/api/v1/activity?org_id={org_id}&limit=2",
            headers=svc_headers,
        )
        assert a1.status_code == 200
        ab1 = a1.json()
        assert len(ab1["items"]) == 2
        assert ab1["has_more"] is True
        assert ab1["next_cursor"] is not None

        a2 = await client.get(
            f"/api/v1/activity?org_id={org_id}&limit=2&cursor={ab1['next_cursor']}",
            headers=svc_headers,
        )
        assert a2.status_code == 200
        ab2 = a2.json()
        assert len(ab2["items"]) == 1
        assert ab2["has_more"] is False

        all_act_items = [*ab1["items"], *ab2["items"]]
        assert len(all_act_items) == 3
        # Deleted thread's historical activity row renders generic safe title and strips custom_title
        deleted_act = [it for it in all_act_items if it.get("thread_id") == deleted_tid]
        assert len(deleted_act) == 1
        assert deleted_act[0]["detail"]["title"] == DEFAULT_SHARED_TITLE
        assert "custom_title" not in deleted_act[0]["detail"]
        # FILE_PUBLISHED path redacted
        file_act = [it for it in all_act_items if it["action"] == activity_svc.Action.FILE_PUBLISHED.value]
        assert len(file_act) == 1
        assert file_act[0]["detail"]["path"] == "[redacted-path]"
        combined_act_text = a1.text + a2.text
        assert "SECRET_DELETED_THREAD_TITLE" not in combined_act_text
        assert "SECRET_AUTO_LIB_" not in combined_act_text
        assert "财报.pdf" not in combined_act_text


@pytest.mark.asyncio
async def test_e2e_b6_r5_omit_permissions_preserves_concurrent_revocation_real_pg(
    test_db_pool: Any,
    dalton_schema: Any,
    patched_get_db_connection: Any,
    seed_user: dict[str, Any],
    seed_workspace: dict[str, Any],
) -> None:
    """AU1 r5 #2 (Real PG): Interleaving revocation with an omit-permissions share update keeps allow_files=False."""
    from src.server.database.conversation import threads_write
    from src.server.models.conversation import SharePermissions

    user_id = str(seed_user["user_id"])
    workspace_id = str(seed_workspace["workspace_id"])
    tid = str(uuid.uuid4())
    tok = f"r5perm_{uuid.uuid4().hex[:8]}"

    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads
                    (conversation_thread_id, workspace_id, user_id, current_status, thread_index,
                     title, is_shared, share_token, share_permissions, shared_at)
                VALUES (%s::uuid, %s::uuid, %s, 'completed', 101, 'Perm Race', TRUE, %s, %s::jsonb, NOW())
                """,
                (
                    tid,
                    workspace_id,
                    user_id,
                    tok,
                    json.dumps({"allow_files": True, "allow_download": True, "audience": "public"}),
                ),
            )

    # Request A reads stale permissions BEFORE concurrent revocation and omits `permissions` in its request body.
    stale_existing = {"allow_files": True, "allow_download": True, "audience": "public"}
    effective_a, patch_a = share_redaction.prepare_share_permissions_update(stale_existing, None)
    assert patch_a is None
    assert effective_a["allow_files"] is True

    # Request B concurrently revokes file access (allow_files=False).
    effective_b, patch_b = share_redaction.prepare_share_permissions_update(
        stale_existing, SharePermissions(allow_files=False)
    )
    assert patch_b == {"allow_files": False}
    revoked = await share_redaction.apply_thread_share_update(
        threads_write.update_thread_sharing,
        tid,
        is_shared=True,
        shared_at=None,
        effective_perms=effective_b,
        patch=patch_b,
    )
    assert revoked is not None
    assert revoked["share_permissions"]["allow_files"] is False
    assert revoked["share_permissions"]["allow_download"] is False

    # Request A now executes its UPDATE with patch=None: MUST NOT overwrite share_permissions back to True.
    after_a = await share_redaction.apply_thread_share_update(
        threads_write.update_thread_sharing,
        tid,
        is_shared=True,
        shared_at=None,
        effective_perms=effective_a,
        patch=patch_a,
    )
    assert after_a is not None
    assert after_a["share_permissions"]["allow_files"] is False
    assert after_a["share_permissions"]["allow_download"] is False

    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT share_permissions FROM conversation_threads WHERE conversation_thread_id = %s::uuid",
                (tid,),
            )
            row = await cur.fetchone()
    assert row is not None
    stored_perms = row["share_permissions"] if isinstance(row, dict) else row[0]
    assert stored_perms["allow_files"] is False
    assert stored_perms["allow_download"] is False


@pytest.mark.asyncio
async def test_e2e_b6_r5_paginated_replay_media_issuance_and_two_gets_real_pg(
    monkeypatch: pytest.MonkeyPatch,
    test_db_pool: Any,
    dalton_schema: Any,
    patched_get_db_connection: Any,
    seed_user: dict[str, Any],
    seed_workspace: dict[str, Any],
) -> None:
    """AU1 r5 #3 (Real PG): Paginated replay SQL joins turn_slices -> conversation_responses, issues controlled media refs, and all media GETs return 200."""
    import re
    from src.server.services.history.replay import lines as replay_lines
    from src.utils import storage as storage_mod

    monkeypatch.setattr(
        storage_mod,
        "load_agent_config",
        create_autospec(storage_mod.load_agent_config, return_value={}),
    )
    monkeypatch.setenv("DALTON_MEDIA_SIGNING_SECRET", "r5-real-pg-media-secret-32-bytes!")
    monkeypatch.setenv("STORAGE_PUBLIC_URL_BASE", "https://cdn.pg.example")
    monkeypatch.setenv("STORAGE_PROVIDER", "s3")
    monkeypatch.setenv("S3_BUCKET_NAME", "pg-media-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.pg.example")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "sk")

    user_id = str(seed_user["user_id"])
    workspace_id = str(seed_workspace["workspace_id"])
    tid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    tok = f"r5med_{uuid.uuid4().hex[:8]}"

    img_key = f"private-media/response-images/{tid}/chart_1.png"
    img_url = f"https://cdn.pg.example/{img_key}"
    att_key = f"private-media/attachments/{tid}/batch1/Q1 Report 财报.pdf"
    att_url = f"https://cdn.pg.example/private-media/attachments/{tid}/batch1/Q1%20Report%20%E8%B4%A2%E6%8A%A5.pdf"
    steer_key = f"private-media/attachments/{tid}/steer1/note file.png"
    steer_url = f"https://cdn.pg.example/private-media/attachments/{tid}/steer1/note%20file.png"

    steer_line = replay_lines.line_of(
        {
            "event": "steering_delivered",
            "data": {
                "thread_id": tid,
                "turn_index": 1,
                "messages": [
                    {
                        "user_id": user_id,
                        "content": "added note",
                        "attachments": [{"name": "note file.png", "url": steer_url}],
                    }
                ],
            },
        }
    )
    encoded_slice_lines = replay_lines.encode([steer_line])

    async with patched_get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO conversation_threads
                    (conversation_thread_id, workspace_id, user_id, current_status, thread_index,
                     title, is_shared, share_token, share_permissions, shared_at)
                VALUES (%s::uuid, %s::uuid, %s, 'completed', 201, 'Paginated Media', TRUE, %s, %s::jsonb, NOW())
                """,
                (
                    tid,
                    workspace_id,
                    user_id,
                    tok,
                    json.dumps({"allow_files": True, "allow_download": True, "audience": "public"}),
                ),
            )
            await cur.execute(
                """
                INSERT INTO conversation_queries
                    (conversation_thread_id, turn_index, content, type, metadata, created_at)
                VALUES (%s::uuid, 1, 'Check report', 'initial', %s::jsonb, NOW())
                """,
                (
                    tid,
                    json.dumps({"attachments": [{"name": "Q1 Report 财报.pdf", "url": att_url}]}),
                ),
            )
            await cur.execute(
                """
                INSERT INTO conversation_responses
                    (conversation_response_id, conversation_thread_id, turn_index, status,
                     metadata, replay_facts, created_at)
                VALUES (%s::uuid, %s::uuid, 1, 'in_progress', %s::jsonb, %s::jsonb, NOW())
                """,
                (
                    rid,
                    tid,
                    json.dumps({"user_id": user_id}),
                    json.dumps({"images": {"c1": img_url}}),
                ),
            )
            await cur.execute(
                """
                UPDATE conversation_responses
                SET status = 'completed'
                WHERE conversation_response_id = %s::uuid
                """,
                (rid,),
            )
            await cur.execute(
                """
                INSERT INTO turn_slices
                    (conversation_response_id, conversation_thread_id,
                     input_checkpoint_id, tail_checkpoint_id, slice_key, slice_codec,
                     slice, lines_key, lines)
                VALUES (%s::uuid, %s::uuid, 'cp-0', 'cp-1', 'sk-1', 'zstd', %s, 'lk-1', %s)
                """,
                (rid, tid, b"slice-bytes", encoded_slice_lines),
            )

    page_items = [
        {
            "event": "user_message",
            "data": {
                "thread_id": tid,
                "turn_index": 1,
                "content": "Check report",
                "metadata": {"attachments": [{"name": "Q1 Report 财报.pdf", "url": att_url}]},
            },
        },
        {
            "event": "message_chunk",
            "data": {
                "thread_id": tid,
                "turn_index": 1,
                "content": f"Chart:\n![c]({img_url})",
            },
        },
    ]
    thread_row = {
        "conversation_thread_id": tid,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "is_shared": True,
        "share_token": tok,
        "share_permissions": {"allow_files": True, "allow_download": True, "audience": "public"},
    }

    # Paginated issuance (page_turns=[1]) executes real turn_slices JOIN conversation_responses SQL
    bindings = await share_media.resolve_thread_media_bindings(
        thread_row,
        [{"turn_index": 1, "metadata": {"attachments": [{"name": "Q1 Report 财报.pdf", "url": att_url}]}}],
        page_items,
    )
    assert bindings is not None
    assert img_url in bindings
    assert att_url in bindings
    assert bindings[att_url] == ("upstream", att_key)
    assert steer_url in bindings
    assert bindings[steer_url] == ("upstream", steer_key)

    rewritten = share_media.rewrite_items_for_public_media(
        [*page_items, steer_line.item()], thread_id=tid, share_token=tok, bindings=bindings
    )
    att_controlled = rewritten[0]["data"]["metadata"]["attachments"][0]["url"]
    img_controlled_match = re.search(r"(/api/v1/public/shared/[^\s)]+)", rewritten[1]["data"]["content"])
    steer_controlled = rewritten[2]["data"]["messages"][0]["attachments"][0]["url"]
    assert att_controlled.startswith(f"/api/v1/public/shared/{tok}/media/")
    assert img_controlled_match is not None
    img_controlled = img_controlled_match.group(1)
    assert steer_controlled.startswith(f"/api/v1/public/shared/{tok}/media/")

    stored_blobs = {
        img_key: b"\x89PNG-CHART-BYTES",
        att_key: b"%PDF-1.7-SPACE-CHINESE",
        steer_key: b"\x89PNG-STEER-BYTES",
    }

    def _fake_read(key: str, *, reader: Any = None) -> bytes | None:
        return stored_blobs.get(key)

    monkeypatch.setattr(
        share_media,
        "read_controlled_object_bytes",
        create_autospec(share_media.read_controlled_object_bytes, side_effect=_fake_read),
    )
    monkeypatch.setattr(
        share_redaction,
        "resolve_effective_share_permissions",
        create_autospec(
            share_redaction.resolve_effective_share_permissions,
            return_value=share_redaction.EffectiveSharePermissions(
                allow_files=True, allow_download=True
            ),
        ),
    )

    app = FastAPI()
    app.include_router(share_media.public_media_router, prefix="/api/v1/public")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        r_att = await client.get(att_controlled)
        r_img = await client.get(img_controlled)
        r_steer = await client.get(steer_controlled)

    assert r_att.status_code == 200
    assert r_att.content == b"%PDF-1.7-SPACE-CHINESE"
    assert r_img.status_code == 200
    assert r_img.content == b"\x89PNG-CHART-BYTES"
    assert r_steer.status_code == 200
    assert r_steer.content == b"\x89PNG-STEER-BYTES"


@pytest.mark.asyncio
async def test_e2e_b7_cold_warm_replay_perf_budget_and_singleflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7: Real cold/warm read_replay_page + turn_slices, PageBudget truncation, tracemalloc bounds, singleflight, and paginated rich replay."""
    tid = str(uuid.uuid4())
    turn_count = 45
    anchors: list[TurnAnchor] = []
    queries: list[dict[str, Any]] = []
    responses_by_turn: dict[int, dict[str, Any]] = {}
    turn_slices_by_idx: dict[int, slices_mod.TurnSlice] = {}

    for t in range(turn_count):
        rid = str(uuid.uuid4())
        a = TurnAnchor(
            turn_ordinal=t,
            input_checkpoint_id=f"cp-in-{t}",
            tail_checkpoint_id=f"cp-tail-{t}",
            turn_index=t,
            end_checkpoint_id=f"cp-tail-{t}",
        )
        anchors.append(a)
        queries.append(
            {
                "conversation_query_id": str(uuid.uuid4()),
                "turn_index": t,
                "type": "initial" if t == 0 else "follow_up",
                "content": f"Question {t}",
                "metadata": {},
            }
        )
        responses_by_turn[t] = {
            "turn_index": t,
            "status": "completed",
            "conversation_response_id": rid,
            "replay_facts": {},
        }
        msgs: list[Any] = [HumanMessage(id=f"h-{t}", content=f"Question {t}")]
        if t == 35:
            # Heavy tool outputs live in raw turn_slices (triggering PageBudget), while strict redaction drops them in O(1)
            for idx in range(10):
                msgs.append(
                    AIMessage(
                        id=f"ai-tool-{t}-{idx}",
                        content="Calling tool",
                        tool_calls=[{"id": f"tc-{t}-{idx}", "name": "Read", "args": {"path": f"x_{idx}.txt"}}],
                    )
                )
                msgs.append(
                    ToolMessage(id=f"tm-{t}-{idx}", tool_call_id=f"tc-{t}-{idx}", name="Read", content="X" * (24 * 1024))
                )
        msgs.append(
            AIMessage(
                id=f"ai-{t}",
                content=[{"type": "text", "text": f"Final thesis for turn {t}.", "phase": "final_answer"}],
            )
        )
        turn_slices_by_idx[t] = slices_mod.TurnSlice(anchor=a, messages=msgs)

    fake_thread = {
        "conversation_thread_id": tid,
        "workspace_id": str(uuid.uuid4()),
        "user_id": "u1",
        "owner_id": "u1",
        "title": "Perf thread",
        "is_shared": True,
        "share_token": "tok-b7-perf",
        "share_permissions": {"allow_files": False, "allow_download": False},
        "latest_checkpoint_id": f"cp-tail-{turn_count - 1}",
    }
    rows = replay_rows_mod.ThreadRows(
        thread_id=tid,
        owner_id="u1",
        thread=fake_thread,
        queries=queries,
        responses_by_turn=responses_by_turn,
        usages=[],
        provenance=[],
        run_facts=[],
        open_runs={},
        failed_steers=[],
    )

    slice_fetches = 0

    async def _aget_slices(_tid: str, req_anchors: list[TurnAnchor]) -> list[slices_mod.TurnSlice]:
        nonlocal slice_fetches
        slice_fetches += 1
        await asyncio.sleep(0.01)
        return [turn_slices_by_idx[int(a.turn_index)] for a in req_anchors]

    reader_stub = create_autospec(CheckpointHistoryReader, instance=True)
    reader_stub.serde = JsonPlusSerializer()
    reader_stub.aget_turn_anchors.return_value = (anchors, f"cp-tail-{turn_count - 1}")
    reader_stub.aget_turn_slices.side_effect = _aget_slices
    reader_stub.aget_tip_interrupts.return_value = []
    monkeypatch.setattr(CheckpointHistoryReader, "get_instance", classmethod(lambda cls: reader_stub))

    stored_slices: dict[str, slices_db.StoredSlice] = {}

    async def _get_turn_lines(rids: list[str]) -> dict[str, slices_db.StoredSlice]:
        return {r: stored_slices[r] for r in rids if r in stored_slices}

    async def _get_turn_slices(rids: list[str]) -> dict[str, slices_db.StoredSlice]:
        return {r: stored_slices[r] for r in rids if r in stored_slices}

    async def _upsert_turn_slices(turn_rows: list[slices_db.TurnRow]) -> None:
        for tr in turn_rows:
            stored_slices[tr.response_id] = tr.slice

    async def _update_turns(updates: list[slices_db.TurnUpdate]) -> None:
        for u in updates:
            cur = stored_slices.get(u.response_id)
            if cur is not None:
                stored_slices[u.response_id] = cur._replace(
                    lines_key=u.lines_key or cur.lines_key,
                    lines=u.lines or cur.lines,
                )

    monkeypatch.setattr(slices_db, "get_turn_lines", create_autospec(slices_db.get_turn_lines, side_effect=_get_turn_lines))
    monkeypatch.setattr(slices_db, "get_turn_slices", create_autospec(slices_db.get_turn_slices, side_effect=_get_turn_slices))
    monkeypatch.setattr(slices_db, "upsert_turn_slices", create_autospec(slices_db.upsert_turn_slices, side_effect=_upsert_turn_slices))
    monkeypatch.setattr(slices_db, "update_turns", create_autospec(slices_db.update_turns, side_effect=_update_turns))

    finish_lines_calls = 0

    def _forbidden_finish(*args: Any, **kwargs: Any) -> Any:
        nonlocal finish_lines_calls
        finish_lines_calls += 1
        raise AssertionError("finish_lines must never be called on strict allow_files=false replay")

    monkeypatch.setattr(replay_mod, "finish_lines", _forbidden_finish)

    # 1. Real PageBudget soft-budget truncation + singleflight cold concurrent read
    budget = PageBudget(min_turns=20, max_bytes=128 * 1024)
    tracemalloc.start()
    try:
        base_cur, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        async def _one_req() -> tuple[float, Any]:
            t0 = time.perf_counter()
            page, _src = await share_redaction.read_strict_public_replay_page(
                rows, limit=turn_count, budget=budget, trigger_background_warm=False
            )
            redactor = share_redaction.StrictNoFilesShareRedactor(
                thread_id=tid, responses_by_turn=responses_by_turn
            )
            redacted = list(redactor.iter_redacted_events(page.lines))
            assert len(redacted) > 0
            return (time.perf_counter() - t0) * 1000.0, page

        cold_results = await asyncio.gather(*[_one_req() for _ in range(10)])
        cold_latencies = [lat for lat, _ in cold_results]
        first_page = cold_results[0][1]
        _, cold_peak = tracemalloc.get_traced_memory()
        cold_peak_mib = max(0.0, (cold_peak - base_cur) / (1024 * 1024))

        assert first_page.has_more is True  # PageBudget truncated older turns past min_turns=20
        assert first_page.first_turn_index == 25
        assert finish_lines_calls == 0
        assert len(stored_slices) == 0  # foreground read used persist=False
        assert sorted(cold_latencies)[9] <= 5000.0
        assert cold_peak_mib <= 96.0
        cold_fetches = slice_fetches
        assert cold_fetches >= 1

        # 2. Background warm pass persists turn_slices once
        await replay_mod.read_replay_page(rows, limit=turn_count, budget=budget, persist=True)
        assert len(stored_slices) >= 20
        fetches_after_warm = slice_fetches

        # 3. Warm concurrent 10x read hits turn_slices with ZERO cold fetches (measure heap with tracemalloc, latency without tracemalloc overhead)
        warm_base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        await asyncio.gather(*[_one_req() for _ in range(10)])
        _, warm_peak = tracemalloc.get_traced_memory()
        warm_peak_mib = max(0.0, (warm_peak - warm_base) / (1024 * 1024))
        assert slice_fetches == fetches_after_warm  # zero cold projection on warm cache
        assert warm_peak_mib <= 8.0
    finally:
        tracemalloc.stop()

    warm_results = await asyncio.gather(*[_one_req() for _ in range(10)])
    warm_latencies = [lat for lat, _ in warm_results]
    assert slice_fetches == fetches_after_warm
    assert sorted(warm_latencies)[9] <= 150.0

    # 4. Paginated older page (before_turn=25) loads earlier turns cleanly
    older_page, _ = await replay_mod.read_replay_page(
        rows, before_turn=25, limit=10, budget=budget, persist=True
    )
    assert older_page.first_turn_index == 15
    assert older_page.has_more is True


def test_e2e_scheme_a_storage_targets_and_strict_403_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Scheme A storage target deduplication and strict 403 --verify across 3 topologies + OSS."""
    live_endpoint = (os.getenv("TEST_MINIO_ENDPOINT") or "").strip()
    if live_endpoint:
        targets = share_media.resolve_active_storage_targets()
        distinct_buckets = {t.bucket_name for t in targets if t.bucket_name}
        assert len(distinct_buckets) >= 3, (
            f"Expected at least 3 distinct physical buckets (primary, chat-upload, transcription), "
            f"got {len(distinct_buckets)}: {sorted(distinct_buckets)}"
        )
        ok, checks = ops_script.verify_storage_targets(targets=targets, probe_write=True)
        assert ok is True, f"Live Scheme A verify failed: {checks}"
        return

    # Deterministic 3-bucket + legacy S3_* + independent signing endpoint verification
    monkeypatch.setenv("S3_BUCKET_NAME", "legacy-primary")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("STORAGE_BROWSER_ENDPOINT_URL", "https://browser-s3.legacy.example")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_PUBLIC_URL_BASE", "https://cdn.legacy.example")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-bucket")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "sk")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "asr-bucket")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "sk")

    targets = share_media.resolve_active_storage_targets()
    assert {t.bucket_name for t in targets} == {"legacy-primary", "chat-bucket", "asr-bucket"}

    cleaned: list[str] = []

    def _put(target: Any, key: str) -> Any:
        return lambda: cleaned.append(f"{target.bucket_name}/{key}")

    ok, checks = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=True,
        http_get_fn=lambda url, timeout_s=10.0: (403, "Forbidden"),
        put_probe_fn=_put,
        find_existing_fn=lambda t, p: [f"{p}existing1.bin", f"{p}existing2.bin"],
    )
    assert ok is True
    assert len(cleaned) > 0
    assert any("cdn.legacy.example/provenance/existing2.bin" in c.url for c in checks)
    assert any("browser-s3.legacy.example" in c.url for c in checks)
    assert any("chat-bucket/channel-snapshots/" in c.url for c in checks)
    assert any("asr-bucket/transcriptions/" in c.url for c in checks)

    # 404 MUST NOT pass --verify
    ok_404, _ = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=True,
        http_get_fn=lambda url, timeout_s=10.0: (404, "Not Found"),
        put_probe_fn=_put,
        find_existing_fn=lambda t, p: [],
    )
    assert ok_404 is False

    # Listing failure MUST fail --verify
    def _boom_list(t: Any, p: str) -> list[str]:
        raise RuntimeError("list denied")

    ok_list_err, _ = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=True,
        http_get_fn=lambda url, timeout_s=10.0: (403, "Forbidden"),
        put_probe_fn=_put,
        find_existing_fn=_boom_list,
    )
    assert ok_list_err is False
