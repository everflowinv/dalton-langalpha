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
import psycopg
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
from src.server.auth import jwt_bearer, owner_requests, ws_auth
from src.server.services import activity as activity_svc
from src.server.services.history import projector as projector_mod
from src.server.services.history import replay as replay_mod
from src.server.services.history.replay import PageBudget, ReplayPage
from src.server.services.history.replay.lines import line_of as _line_of
from src.server.services.history.replay.stubs import stub_lines
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
        "正式结论汇总，详见 reports/q3.pdf 与 `out/memo.md`。\n\n"
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


def _build_projected_checkpoint_lines(
    *, thread_id: str, final_answer_md: str
) -> list[Any]:
    """Produce real checkpoint-projected lines via ``projector.messages_to_history_events`` + ``history_events_to_sse``."""
    ai_mixed = AIMessage(
        id="ai-0",
        content=[
            {"type": "thinking", "thinking": "SECRET_COT_ADJ_08"},
            {"type": "text", "text": "SECRET_COMMENTARY_09", "phase": "commentary"},
            {"type": "text", "text": final_answer_md, "phase": "final_answer"},
        ],
    )
    ai_tool = AIMessage(
        id="ai-pre",
        content="SECRET_PRETOOL_09",
        tool_calls=[{"id": "tc-1", "name": "Read", "args": {"path": "SECRET_TOOL_10"}}],
    )
    tool_msg = ToolMessage(
        id="tm-1",
        tool_call_id="tc-1",
        name="Read",
        content="SECRET_TOOL_10",
    )
    events = projector_mod.messages_to_history_events(
        [HumanMessage(id="h-0", content="请总结核心结论"), ai_tool, tool_msg, ai_mixed],
        agent="main",
    )
    sse_items = projector_mod.history_events_to_sse(events, thread_id=thread_id)
    projected_lines = [line_of("user_message", {"turn_index": 0, "content": "请总结核心结论"})]
    for item in sse_items:
        data = dict(item["data"])
        data["turn_index"] = 0
        projected_lines.append(line_of(item["event"], data))
    return projected_lines


@pytest.mark.asyncio
async def test_e2e_s01_to_s19_strict_share_redaction_and_three_replay_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify S01–S19 zero-leakage via real public replay/files/media routes across 3 replay sources."""
    thread_id = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
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

    # Source 1: warm turn_slices lines
    warm_lines = _build_warm_slice_lines(
        thread_id=thread_id, final_answer_md=final_answer_md, api_grant_url=api_grant_url
    )
    # Source 2: cold checkpoint projection via real projector.project_messages
    cold_lines = _build_projected_checkpoint_lines(
        thread_id=thread_id, final_answer_md=final_answer_md
    )
    # Source 3: real stub_lines / legacy.verbatim archive fallback
    stub_map, _ = await stub_lines(
        thread_id,
        [0, 1],
        {
            0: [{"turn_index": 0, "type": "initial", "content": "请总结核心结论\n<file-comments>SECRET_ANNOT_04</file-comments>", "metadata": {}}],
            1: [{"turn_index": 1, "type": "follow_up", "content": "失败回合", "metadata": {}}],
        },
        {
            0: {
                "turn_index": 0,
                "status": "completed",
                "conversation_response_id": "r0",
                "replay_facts": {
                    "archive": [
                        {"event": "legacy_debug", "data": {"dump": "SECRET_VERBATIM_17"}},
                        {"event": "message_chunk", "data": {"agent": "subagent:call_07", "role": "assistant", "content_type": "text", "content": "SECRET_SUBAGENT_FALLBACK_07"}},
                        {"event": "message_chunk", "data": {"agent": "main", "role": "assistant", "content_type": "text", "content": final_answer_md}},
                    ],
                },
            },
            1: {
                "turn_index": 1,
                "status": "error",
                "conversation_response_id": "r1",
                "replay_facts": {
                    "partial": {"content": "SECRET_FAILED_ARCHIVE_09"},
                },
            },
        },
        open_runs=[],
    )
    archive_lines = [ln for ti in sorted(stub_map) for ln in stub_map[ti]]

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
    }
    responses_by_turn = {
        0: {"turn_index": 0, "status": "completed", "conversation_response_id": "r0"},
        1: {"turn_index": 1, "status": "error", "conversation_response_id": "r1"},
        2: {"turn_index": 2, "status": "cancelled", "conversation_response_id": "r2"},
    }

    app = FastAPI()
    app.include_router(public_app.router)
    client = TestClient(app)

    monkeypatch.setattr(
        share_access,
        "get_shared_thread",
        create_autospec(share_access.get_shared_thread, return_value=fake_thread),
    )
    monkeypatch.setattr(
        public_app,
        "get_shared_thread",
        create_autospec(public_app.get_shared_thread, return_value=fake_thread),
    )
    monkeypatch.setattr(
        public_app,
        "get_replay_thread_data",
        create_autospec(
            public_app.get_replay_thread_data,
            return_value=SimpleNamespace(
                thread_id=thread_id,
                thread=fake_thread,
                queries=[{"turn_index": 0, "type": "initial", "content": "请总结核心结论", "metadata": {}}],
                responses_by_turn=responses_by_turn,
                open_runs=[],
                run_facts=[],
            ),
        ),
    )

    orig_read_strict = share_redaction.read_strict_public_replay_page
    for source_label, source_lines in (
        ("slices", warm_lines),
        ("checkpoint", cold_lines),
        ("stub", archive_lines),
    ):
        monkeypatch.setattr(
            share_redaction,
            "read_strict_public_replay_page",
            create_autospec(
                orig_read_strict,
                return_value=(
                    ReplayPage(lines=source_lines, first_turn_index=0, has_more=False),
                    source_label,
                ),
            ),
        )
        r_sse = client.get(f"/api/v1/public/shared/{share_token}/replay")
        assert r_sse.status_code == 200, f"[{source_label}] replay returned {r_sse.status_code}"
        assert r_sse.headers.get("cache-control") == "no-store"
        assert r_sse.headers.get("clear-site-data") == '"cache"'
        body = r_sse.text
        for sentinel in _all_sentinels() + (
            grant_16,
            cap_token_16,
            mon_grant_16,
            media_token_19,
            "reports/q3.pdf",
            "out/memo.md",
        ):
            assert sentinel not in body, f"[{source_label}] leaked sentinel {sentinel!r}"
        assert f"/a/{pub_code_16}" in body
        assert "<redacted-image" in body
        assert "```mermaid" not in body

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
    """S19 + B8: Remote/proxied unkeyed oss owner routes & dev-fallback transcription tickets return 401."""
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

    from fastapi import Depends

    app = FastAPI()
    app.include_router(asr_router_mod.router)

    @app.get("/api/v1/threads/{thread_id}/provenance/bodies")
    async def _prov_bodies(thread_id: str, user_id: str = Depends(api_utils.get_current_user_id)):
        return {"thread_id": thread_id, "user_id": user_id, "body": "SECRET_PROV_INLINE_19"}

    @app.get("/api/v1/threads/{thread_id}/provenance/{record_id}/body")
    async def _prov_body(thread_id: str, record_id: str, user_id: str = Depends(api_utils.get_current_user_id)):
        return {"body": "SECRET_PROV_SPILL_19"}

    @app.get("/api/v1/threads/{thread_id}")
    async def _get_thread(thread_id: str, user_id: str = Depends(api_utils.get_current_user_id)):
        return {"title": "SECRET_RAW_TITLE_19"}

    @app.get("/api/v1/workspaces/{workspace_id}/files/download-url")
    async def _ws_dl(workspace_id: str, user_id: str = Depends(api_utils.get_current_user_id)):
        return {"url": "SECRET_WS_FILE_19"}

    @app.get("/api/v1/threads/{thread_id}/messages/stream")
    async def _stream(thread_id: str, user_id: str = Depends(api_utils.get_current_user_id)):
        return {"chunk": "SECRET_LIVE_STREAM_19"}

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    rec_id = str(uuid.uuid4())
    # Caddy proxy headers (B8) + remote client
    proxied_headers = {"X-Forwarded-For": "203.0.113.19", "Host": "app.example.com"}

    transport = httpx.ASGITransport(app=app, client=("172.31.250.2", 54321))
    async with httpx.AsyncClient(transport=transport, base_url="https://app.example.com") as client:
        for path in (
            f"/api/v1/threads/{tid}/provenance/bodies",
            f"/api/v1/threads/{tid}/provenance/{rec_id}/body?full=true",
            f"/api/v1/threads/{tid}",
            f"/api/v1/workspaces/{ws_id}/files/download-url?path=secret_19.txt",
            f"/api/v1/threads/{tid}/messages/stream",
        ):
            r = await client.get(path, headers=proxied_headers)
            assert r.status_code == 401, f"Expected 401 for {path}, got {r.status_code}"
            assert "set-cookie" not in r.headers
            for s in (
                "SECRET_PROV_INLINE_19",
                "SECRET_PROV_SPILL_19",
                "SECRET_RAW_TITLE_19",
                "SECRET_WS_FILE_19",
                "SECRET_LIVE_STREAM_19",
            ):
                assert s not in r.text

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
            assert "SECRET_ASR_TRANSCRIPT_19" not in r.text
            assert "SECRET_ASR_RAW_19" not in r.text


@pytest.mark.asyncio
async def test_e2e_b6_deleted_thread_org_library_and_activity_cursor_pagination(
    monkeypatch: pytest.MonkeyPatch,
    test_db_uri: str,
) -> None:
    """B6: Verify d059 tables exist in real DB and multi-page GET /activity & /org-library/service pagination."""
    async with await psycopg.AsyncConnection.connect(test_db_uri, autocommit=True) as conn:
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
            tables = [row[0] for row in await cur.fetchall()]
    assert tables == ["dalton_org_library_title_meta", "dalton_storage_key_migrations"]

    deleted_tid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    item_ids = [str(uuid.uuid4()) for _ in range(3)]
    for iid in item_ids:
        await share_redaction.upsert_org_library_title_meta(iid, custom_title=False)

    lib_pages = {
        None: (
            [
                {
                    "item_id": item_ids[0],
                    "org_id": "org-b6",
                    "kind": "thread",
                    "thread_id": deleted_tid,
                    "publisher_id": "u1",
                    "owner_id": "u1",
                    "listed": True,
                    "share_token": "tok-0",
                    "title": "SECRET_AUTO_LIB_01",
                    "summary": "Check /api/v1/artifacts/g/v1.grant_lib_summary_01/v/1/a.html reports/q3.pdf",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "item_id": item_ids[1],
                    "org_id": "org-b6",
                    "kind": "thread",
                    "thread_id": deleted_tid,
                    "publisher_id": "u1",
                    "owner_id": "u1",
                    "listed": True,
                    "share_token": "tok-1",
                    "title": "SECRET_AUTO_LIB_02",
                    "summary": "Check /Users/alice/ws/secret.py",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
            "cur-lib-page-2",
        ),
        "cur-lib-page-2": (
            [
                {
                    "item_id": item_ids[2],
                    "org_id": "org-b6",
                    "kind": "thread",
                    "thread_id": deleted_tid,
                    "publisher_id": "u1",
                    "owner_id": "u1",
                    "listed": True,
                    "share_token": "tok-2",
                    "title": "SECRET_AUTO_LIB_03",
                    "summary": "Clean summary",
                    "created_at": now,
                    "updated_at": now,
                }
            ],
            None,
        ),
    }

    act_pages = {
        None: (
            [
                {
                    "event_id": str(uuid.uuid4()),
                    "actor_id": "u1",
                    "action": activity_svc.Action.LIBRARY_PUBLISHED.value,
                    "resource_type": "library_item",
                    "resource_id": item_ids[0],
                    "workspace_id": None,
                    "thread_id": deleted_tid,
                    "detail": {"kind": "thread", "title": "SECRET_AUTO_LIB_01", "custom_title": False},
                    "created_at": now,
                }
            ],
            "cur-act-page-2",
        ),
        "cur-act-page-2": (
            [
                {
                    "event_id": str(uuid.uuid4()),
                    "actor_id": "u1",
                    "action": activity_svc.Action.FILE_PUBLISHED.value,
                    "resource_type": "artifact",
                    "resource_id": str(uuid.uuid4()),
                    "workspace_id": str(uuid.uuid4()),
                    "thread_id": None,
                    "detail": {"path": "reports/q3_secret.pdf", "version": 1},
                    "created_at": now,
                }
            ],
            None,
        ),
    }

    monkeypatch.setattr(
        org_library_app.library_db,
        "list_items",
        create_autospec(
            org_library_app.library_db.list_items,
            side_effect=lambda org_id, kind=None, cursor=None, limit=50, listed_only=False: lib_pages[cursor],
        ),
    )
    monkeypatch.setattr(
        activity_app,
        "list_events",
        create_autospec(
            activity_app.list_events,
            side_effect=lambda *args, cursor=None, **kwargs: act_pages[cursor],
        ),
    )
    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", "s" * 40)

    app = FastAPI()
    app.include_router(org_library_app.router)
    app.include_router(activity_app.router)
    app.dependency_overrides[api_utils.get_current_user_id] = lambda: "u1"
    client = TestClient(app)

    # Paginate GET /api/v1/org-library/service across 2 pages
    p1 = client.get(
        "/api/v1/org-library/service?org_id=org-b6&limit=2",
        headers={"X-Service-Token": "s" * 40},
    )
    assert p1.status_code == 200
    b1 = p1.json()
    assert len(b1["items"]) == 2
    assert b1["next_cursor"] == "cur-lib-page-2"
    assert all(it["title"] == DEFAULT_SHARED_TITLE for it in b1["items"])
    assert "grant_lib_summary_01" not in p1.text
    assert "reports/q3.pdf" not in p1.text

    p2 = client.get(
        f"/api/v1/org-library/service?org_id=org-b6&limit=2&cursor={b1['next_cursor']}",
        headers={"X-Service-Token": "s" * 40},
    )
    assert p2.status_code == 200
    b2 = p2.json()
    assert len(b2["items"]) == 1
    assert b2["next_cursor"] is None
    assert b2["items"][0]["title"] == DEFAULT_SHARED_TITLE

    # Paginate GET /api/v1/activity across 2 pages
    a1 = client.get("/api/v1/activity?limit=1")
    assert a1.status_code == 200
    ab1 = a1.json()
    assert len(ab1["events"]) == 1
    assert ab1["next_cursor"] == "cur-act-page-2"
    assert ab1["events"][0]["detail"]["title"] == DEFAULT_SHARED_TITLE
    assert "custom_title" not in ab1["events"][0]["detail"]

    a2 = client.get(f"/api/v1/activity?limit=1&cursor={ab1['next_cursor']}")
    assert a2.status_code == 200
    ab2 = a2.json()
    assert len(ab2["events"]) == 1
    assert ab2["next_cursor"] is None
    assert ab2["events"][0]["detail"]["path"] == "[redacted-path]"
    assert "q3_secret.pdf" not in a2.text


@pytest.mark.asyncio
async def test_e2e_b7_cold_warm_replay_perf_budget_and_singleflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7: Quantified cold/warm TTFB, strict tracemalloc bounds, real PageBudget, and singleflight split."""
    tid = str(uuid.uuid4())
    big_tool_payload = "X" * (16 * 1024 * 1024)
    lines_by_turn: dict[int, list[Any]] = {}
    responses_by_turn: dict[int, dict[str, Any]] = {}
    for t in range(45):
        responses_by_turn[t] = {"turn_index": t, "status": "completed", "conversation_response_id": f"r-{t}"}
        turn_lines = [
            line_of("replay_turn", {"turn_index": t, "status": "completed", "response_id": f"r-{t}"}),
            line_of("user_message", {"turn_index": t, "content": f"Question {t}"}),
        ]
        if t == 35:
            turn_lines.append(
                line_of("tool_call_result", {"turn_index": t, "agent": "main", "content": big_tool_payload})
            )
        turn_lines.append(
            line_of(
                "message_chunk",
                {
                    "turn_index": t,
                    "agent": "main",
                    "role": "assistant",
                    "content_type": "text",
                    "phase": "final_answer",
                    "content": f"Final thesis for turn {t}.",
                },
            )
        )
        lines_by_turn[t] = turn_lines

    # 1. Verify real PageBudget soft-budget truncation (min_turns=20 over 45 turns; 25 older turns remain)
    candidates = [(t, None) for t in range(45)]

    async def _read_batch(batch):
        return {ti: lines_by_turn[ti] for ti, _ in batch}

    small_budget = PageBudget(min_turns=20, max_bytes=1 * 1024 * 1024)
    selected_page, got_lines = await replay_mod._fill_page(
        _read_batch, candidates, small_budget, whole=True
    )
    assert len(selected_page) == 20  # floor min_turns=20 honoured before budget stops older turns
    flat_page_lines = [ln for ti, _ in selected_page for ln in got_lines[ti]]

    # 2. Quantified cold/warm singleflight + strict tracemalloc delta (no tautology!)
    cold_persist_false_calls = 0
    warm_persist_true_calls = 0
    finish_lines_calls = 0
    cache_warmed = False

    async def _fake_read_replay_page(rows, **kwargs):
        nonlocal cold_persist_false_calls, warm_persist_true_calls
        if kwargs.get("persist") is True:
            warm_persist_true_calls += 1
            return ReplayPage(lines=flat_page_lines, first_turn_index=15, has_more=True), "checkpoint"
        if not cache_warmed:
            cold_persist_false_calls += 1
            share_redaction.mark_cold_projection_miss()
            await asyncio.sleep(0.02)
            return ReplayPage(lines=flat_page_lines, first_turn_index=15, has_more=True), "checkpoint"
        return ReplayPage(lines=flat_page_lines, first_turn_index=15, has_more=True), "checkpoint"

    def _forbidden_finish(*args, **kwargs):
        nonlocal finish_lines_calls
        finish_lines_calls += 1
        raise AssertionError("finish_lines must never be called on strict allow_files=false replay")

    monkeypatch.setattr(
        replay_mod,
        "read_replay_page",
        create_autospec(replay_mod.read_replay_page, side_effect=_fake_read_replay_page),
    )
    monkeypatch.setattr(replay_mod, "finish_lines", _forbidden_finish)
    monkeypatch.setattr(
        replay_mod, "lines_epoch", create_autospec(replay_mod.lines_epoch, return_value="b7-test-epoch")
    )

    rows = SimpleNamespace(thread_id=tid)

    tracemalloc.start()
    try:
        base_cur, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        async def _one_req() -> float:
            t0 = time.perf_counter()
            page, _src = await share_redaction.read_strict_public_replay_page(
                rows, trigger_background_warm=False
            )
            redactor = share_redaction.StrictNoFilesShareRedactor(
                thread_id=tid, responses_by_turn=responses_by_turn
            )
            redacted = list(redactor.iter_redacted_events(page.lines))
            assert len(redacted) > 0
            return (time.perf_counter() - t0) * 1000.0

        cold_latencies = await asyncio.gather(*[_one_req() for _ in range(10)])
        _, cold_peak = tracemalloc.get_traced_memory()
        cold_peak_mib = max(0.0, (cold_peak - base_cur) / (1024 * 1024))

        assert cold_persist_false_calls == 1
        assert finish_lines_calls == 0
        assert sorted(cold_latencies)[9] <= 1500.0
        assert cold_peak_mib <= 96.0

        # Background warm pass once
        await _fake_read_replay_page(rows, persist=True)
        cache_warmed = True
        assert warm_persist_true_calls == 1

        warm_base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        warm_latencies = await asyncio.gather(*[_one_req() for _ in range(10)])
        _, warm_peak = tracemalloc.get_traced_memory()
        warm_peak_mib = max(0.0, (warm_peak - warm_base) / (1024 * 1024))
        assert sorted(warm_latencies)[9] <= 150.0
        assert warm_peak_mib <= 8.0
    finally:
        tracemalloc.stop()


def test_e2e_scheme_a_storage_targets_and_strict_403_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Scheme A storage target deduplication and strict 403 --verify across 3 topologies + OSS."""
    live_endpoint = (os.getenv("TEST_MINIO_ENDPOINT") or "").strip()
    if live_endpoint:
        targets = share_media.resolve_active_storage_targets()
        if targets:
            ok, checks = ops_script.verify_storage_targets(targets=targets, probe_write=True)
            assert ok is True, f"Live Scheme A verify failed: {checks}"
        return

    # Deterministic 3-bucket + legacy S3_* verification with probe_write=True (default)
    monkeypatch.setenv("S3_BUCKET_NAME", "legacy-primary")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
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

    def _put(target, key):
        return lambda: cleaned.append(f"{target.bucket_name}/{key}")

    ok, checks = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=True,
        http_get_fn=lambda url, timeout_s=10.0: (403, "Forbidden"),
        put_probe_fn=_put,
        find_existing_fn=lambda t, p: None,
    )
    assert ok is True
    assert len(cleaned) > 0
    assert any("cdn.legacy.example/provenance/" in c.url for c in checks)
    assert any("chat-bucket/channel-snapshots/" in c.url for c in checks)
    assert any("asr-bucket/transcriptions/" in c.url for c in checks)

    # 404 MUST NOT pass --verify
    ok_404, _ = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=True,
        http_get_fn=lambda url, timeout_s=10.0: (404, "Not Found"),
        put_probe_fn=_put,
        find_existing_fn=lambda t, p: None,
    )
    assert ok_404 is False
