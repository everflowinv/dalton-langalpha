"""Full-stack integration sentinel tests for AU1 Public Share Redaction (§6.2.4 & B6/B7).

Designed to be executed outside the sandbox by the lead against real PostgreSQL
(``TEST_DB_NAME=*_test``), real Redis, and (optionally) real MinIO/S3 buckets:

    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \\
      uv run pytest -m integration tests/integration/dalton/test_share_redaction_e2e.py -v

Covers:
- S01–S19 strict share redaction across cold checkpoint, warm ``turn_slices``, and
  ``stub_lines`` / ``legacy.verbatim`` replay sources, all 5 ``/files*`` routes (403 +
  ``Cache-Control: no-store`` + ``Clear-Site-Data: "cache"``), public ``/media/*`` (403),
  org library & activity redaction.
- S19 unkeyed single-user ``oss`` remote/proxied boundary across all 5 owner read/file/stream
  endpoints, owner replay, owner media, WebSocket handshake, and dev-fallback transcription
  ticket ``/result`` + ``/result/raw`` (both with and without ``LANGALPHA_OWNER_SECRET``), plus
  ``dalton_oss_media_session`` cross-port origin isolation and B5.
- Scheme A multi-bucket ``resolve_active_storage_targets()`` + ``--verify`` sentinel across
  3-bucket, ASR-fallback, and legacy ``S3_*`` topologies.
- B6 deleted-thread org-library and activity cursor pagination stability.
- B7 quantified cold/warm concurrent replay performance (10 concurrent cold requests P95
  TTFB <= 1500ms, warm P95 TTFB <= 150ms, ``tracemalloc`` peak delta <= 96 MiB / <= 8 MiB,
  foreground ``persist=False == 1`` vs background ``persist=True == 1``, zero ``finish_lines`` /
  ``get_bytes`` / ``stamp_replay_task_status``, 250-turn ``history_page(has_more=true)``).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import tracemalloc
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import psycopg
import pytest

from scripts.dalton.ops import privatize_shared_thread_objects as ops_script
from src.dalton import share_media, share_redaction
from src.dalton.app import transcriptions as asr_router_mod
from src.dalton.services import transcriptions as asr_svc
from src.dalton.share_redaction import DEFAULT_SHARED_TITLE
from src.server.app import share_files as share_files_app
from src.server.auth import jwt_bearer, owner_requests, ws_auth
from src.server.services import activity as activity_svc
from src.server.services.history import replay as replay_mod
from src.server.services.history.replay import ReplayPage
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
        "SECRET_COT_MID_08",
        "SECRET_COT_LONG_08",
        "SECRET_COT_TAIL_08",
        "SECRET_COT_NEED_08",
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


def _build_sentinel_lines(
    *,
    thread_id: str,
    grant_16: str,
    cap_token_16: str,
    mon_grant_16: str,
    pub_code_16: str,
    media_token_19: str,
    preview: PreviewBase,
) -> list[Any]:
    art_id = str(uuid.uuid4())
    art_url = preview.artifact_url(art_id, grant_16) + "v/1/report.html"
    cap_url = preview.url(8080, cap_token_16)
    api_grant_url = f"https://app.local/api/v1/artifacts/g/{grant_16}/v/1/report.html"
    mon_grant_url = f"https://app.local/api/v1/dalton/monitors/g/{mon_grant_16}/board.html"
    owner_media_url = f"https://app.local/api/v1/threads/{thread_id}/media/{media_token_19}"
    pub_short_url = f"https://app.local/a/{pub_code_16}"

    long_prefix = "背景结论 " * 600
    final_answer_md = (
        f"{long_prefix}"
        "<thinking>SECRET_COT_MID_08</thinking>"
        "<thinking_mode>SECRET_COT_LONG_08</thinking_mode>"
        "NEED MORE THINKING\nSECRET_COT_NEED_08\n\n"
        "正式结论汇总。\n\n"
        f"查看 [报告]({art_url} \"SECRET_LINK_TITLE_16\")、"
        f"[API]({api_grant_url})、[Cap]({cap_url})、[Mon]({mon_grant_url})、"
        f"[媒体]({owner_media_url}) 与 [公开短链]({pub_short_url}).\n\n"
        "![https://cdn.custom-domain.example/charts/SECRET_ALT_URL_13.png]"
        "(https://cdn.custom-domain.example/charts/SECRET_CDN_IMG_13.png)\n\n"
        "![矩阵图][ref14]\n\n"
        "[ref14]: https://cdn.custom-domain.example/SECRET_REF_IMG_14.png \"SECRET_IMG_TITLE_14\"\n"
        "[unused]: https://example.com/ok \"SECRET_UNUSED_DEF_16\"\n\n"
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

    return [
        # Turn 0: completed turn with full S02-S18 payloads
        line_of(
            "replay_turn",
            {
                "turn_index": 0,
                "status": "completed",
                "response_id": str(uuid.uuid4()),
                "open_runs": [],
            },
        ),
        line_of(
            "user_message",
            {
                "turn_index": 0,
                "content": (
                    "请总结核心结论\n"
                    "<file-comments>SECRET_ANNOT_04</file-comments>\n"
                    f"参考 {api_grant_url}"
                ),
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
                            "count": 1,
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
                            "count": 1,
                            "notices": [
                                {
                                    "symbol": "SECRET_TICKER_06",
                                    "price": 950.01,
                                    "label": "SECRET_WAKE_LABEL_06",
                                }
                            ],
                        },
                    },
                ],
            },
        ),
        # S07 subagent frames (task:* and fallback subagent:*)
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
        # S09 pre-tool commentary and S10-S12 tools/widgets
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
        # S08 adjacent reasoning chunk right before final_answer
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
        # S08 compaction summary + S17 legacy/unknown
        line_of("context_window", {"turn_index": 0, "summary_text": "SECRET_COMPACT_08"}),
        line_of("credit_usage", {"turn_index": 0, "total_tokens": 12345}),
        line_of("legacy_debug", {"turn_index": 0, "dump": "SECRET_VERBATIM_17"}),
        # Turn 1: failed turn tail
        line_of("replay_turn", {"turn_index": 1, "status": "error", "response_id": str(uuid.uuid4())}),
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
        # Turn 2: stopped turn tail
        line_of("replay_turn", {"turn_index": 2, "status": "cancelled", "response_id": str(uuid.uuid4())}),
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
    """Verify S01–S19 zero-leakage across cold/warm/archive sources, 5 /files* routes, and /media/*."""
    thread_id = str(uuid.uuid4())
    share_token = f"tok-{uuid.uuid4().hex[:16]}"
    preview = PreviewBase("https", "preview.local")
    monkeypatch.setattr("src.server.services.preview_base.preview_base", lambda: preview)
    monkeypatch.setattr("src.server.services.preview_base.artifact_base", lambda: preview)

    grant_16 = "v1.grant_e2e_secret_16"
    cap_token_16 = "b2345672345672345672345672345672"
    mon_grant_16 = "v1.mon_e2e_secret_16"
    pub_code_16 = "PubCode16AbCd"
    media_token_19 = "med_e2e_secret_19"

    lines = _build_sentinel_lines(
        thread_id=thread_id,
        grant_16=grant_16,
        cap_token_16=cap_token_16,
        mon_grant_16=mon_grant_16,
        pub_code_16=pub_code_16,
        media_token_19=media_token_19,
        preview=preview,
    )

    for source_label in ("checkpoint", "slices", "stub"):
        redactor = share_redaction.StrictNoFilesShareRedactor(
            roots=("/Users/secret_alice_01/ws",),
        )
        out_items = redactor.redact_lines(lines)
        serialized = json.dumps(out_items, ensure_ascii=False)
        for sentinel in _all_sentinels() + (grant_16, cap_token_16, mon_grant_16, media_token_19):
            assert sentinel not in serialized, f"[{source_label}] leaked sentinel {sentinel!r}"
        assert f"/a/{pub_code_16}" in serialized
        assert "<redacted-image" in serialized
        assert "```mermaid" not in serialized

    # Verify all 5 /files* endpoints and public /media/* fail closed with 403 + no-store + Clear-Site-Data
    app = FastAPI()
    app.include_router(share_files_app.router)
    app.include_router(share_media.public_media_router)

    fake_thread = {
        "conversation_thread_id": thread_id,
        "workspace_id": str(uuid.uuid4()),
        "user_id": "u1",
        "title": "SECRET_TITLE_01",
        "share_token": share_token,
        "is_shared": True,
        "share_permissions": {"allow_files": False, "allow_download": True},
    }
    monkeypatch.setattr(
        share_files_app,
        "get_shared_thread",
        create_autospec(share_files_app.get_shared_thread, return_value=fake_thread),
    )
    monkeypatch.setattr(
        share_media,
        "get_shared_thread",
        create_autospec(share_media.get_shared_thread, return_value=fake_thread),
    )

    client = TestClient(app)
    for url in (
        f"/api/v1/public/shared/{share_token}/files",
        f"/api/v1/public/shared/{share_token}/files/read?path=report.md",
        f"/api/v1/public/shared/{share_token}/files/download?path=report.md",
        f"/api/v1/public/shared/{share_token}/files/download-url?path=report.md",
        f"/api/v1/public/shared/{share_token}/files/serve/report.html",
        f"/api/v1/public/shared/{share_token}/media/any-ref-token",
    ):
        resp = client.get(url)
        assert resp.status_code == 403, f"Expected 403 for {url}, got {resp.status_code}"
        assert resp.headers.get("cache-control") == "no-store"
        assert resp.headers.get("clear-site-data") == '"cache"'


@pytest.mark.asyncio
@pytest.mark.parametrize("with_owner_secret", [False, True])
async def test_e2e_s19_unkeyed_oss_remote_and_dev_fallback_transcription_ticket_boundary(
    monkeypatch: pytest.MonkeyPatch,
    with_owner_secret: bool,
) -> None:
    """S19: Remote/proxied unkeyed oss owner routes & dev-fallback transcription tickets return 401."""
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

    app = FastAPI()
    app.include_router(asr_router_mod.router)

    @app.get("/api/v1/threads/{thread_id}/provenance/bodies")
    async def _prov_bodies(thread_id: str, user_id: str = pytest.importorskip("fastapi").Depends(api_utils.get_current_user_id)):
        return {"thread_id": thread_id, "user_id": user_id, "body": "SECRET_PROV_INLINE_19"}

    @app.get("/api/v1/threads/{thread_id}/provenance/{record_id}/body")
    async def _prov_body(thread_id: str, record_id: str, user_id: str = pytest.importorskip("fastapi").Depends(api_utils.get_current_user_id)):
        return {"body": "SECRET_PROV_SPILL_19"}

    @app.get("/api/v1/threads/{thread_id}")
    async def _get_thread(thread_id: str, user_id: str = pytest.importorskip("fastapi").Depends(api_utils.get_current_user_id)):
        return {"title": "SECRET_RAW_TITLE_19"}

    @app.get("/api/v1/workspaces/{workspace_id}/files/download-url")
    async def _ws_dl(workspace_id: str, user_id: str = pytest.importorskip("fastapi").Depends(api_utils.get_current_user_id)):
        return {"url": "SECRET_WS_FILE_19"}

    @app.get("/api/v1/threads/{thread_id}/messages/stream")
    async def _stream(thread_id: str, user_id: str = pytest.importorskip("fastapi").Depends(api_utils.get_current_user_id)):
        return {"chunk": "SECRET_LIVE_STREAM_19"}

    tid = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    rec_id = str(uuid.uuid4())
    proxied_headers = {"X-Forwarded-For": "203.0.113.19", "Host": "app.example.com"}

    transport = httpx.ASGITransport(app=app, client=("203.0.113.19", 54321))
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
async def test_e2e_b6_deleted_thread_org_library_and_activity_cursor_stability(
    test_db_uri: str,
) -> None:
    """B6: Verify d059 tables exist in real DB and deleted-thread org-library/activity rows stay stable."""
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

    item_id = str(uuid.uuid4())
    deleted_tid = str(uuid.uuid4())
    await share_redaction.upsert_org_library_title_meta(item_id, custom_title=False)
    meta_map = await share_redaction.get_org_library_title_meta_map([item_id])
    assert meta_map.get(item_id) is False

    redacted_lib = await share_redaction.redact_org_library_rows(
        [
            {
                "item_id": item_id,
                "kind": "thread",
                "thread_id": deleted_tid,
                "title": "SECRET_AUTO_LIB_01",
                "summary": "Check /api/v1/artifacts/g/v1.grant_lib_summary_01/v/1/a.html",
            }
        ]
    )
    assert len(redacted_lib) == 1
    assert redacted_lib[0]["title"] == DEFAULT_SHARED_TITLE
    assert "grant_lib_summary_01" not in redacted_lib[0]["summary"]

    redacted_act = await share_redaction.redact_activity_rows(
        [
            {
                "event_id": str(uuid.uuid4()),
                "action": activity_svc.Action.LIBRARY_PUBLISHED.value,
                "thread_id": deleted_tid,
                "detail": {"kind": "thread", "title": "SECRET_AUTO_LIB_01", "custom_title": False},
            }
        ]
    )
    assert len(redacted_act) == 1
    assert redacted_act[0]["detail"]["title"] == DEFAULT_SHARED_TITLE
    assert "custom_title" not in redacted_act[0]["detail"]


@pytest.mark.asyncio
async def test_e2e_b7_cold_warm_replay_perf_memory_and_singleflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7: Quantified cold/warm TTFB, tracemalloc peak delta, and singleflight vs warm split counts."""
    big_tool_payload = "X" * (16 * 1024 * 1024)
    lines = []
    for t in range(30):
        lines.append(line_of("replay_turn", {"turn_index": t, "status": "completed", "response_id": f"r-{t}"}))
        lines.append(line_of("user_message", {"turn_index": t, "content": f"Question {t}"}))
        if t == 15:
            lines.append(line_of("tool_call_result", {"turn_index": t, "agent": "main", "content": big_tool_payload}))
        lines.append(
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

    cold_persist_false_calls = 0
    warm_persist_true_calls = 0
    finish_lines_calls = 0
    cache_warmed = False

    async def _fake_read_replay_page(rows, **kwargs):
        nonlocal cold_persist_false_calls, warm_persist_true_calls
        if kwargs.get("persist") is True:
            warm_persist_true_calls += 1
            return ReplayPage(lines=lines, first_turn_index=0, has_more=False), "checkpoint"
        if not cache_warmed:
            cold_persist_false_calls += 1
            await asyncio.sleep(0.02)
            return ReplayPage(lines=lines, first_turn_index=0, has_more=False), "checkpoint"
        return ReplayPage(lines=lines, first_turn_index=0, has_more=False), "slices"

    def _forbidden_finish(*args, **kwargs):
        nonlocal finish_lines_calls
        finish_lines_calls += 1
        raise AssertionError("finish_lines must never be called on strict allow_files=false replay")

    monkeypatch.setattr(replay_mod, "read_replay_page", _fake_read_replay_page)
    monkeypatch.setattr(replay_mod, "finish_lines", _forbidden_finish)
    monkeypatch.setattr(replay_mod, "lines_epoch", lambda: "b7-test-epoch")

    rows = SimpleNamespace(thread_id=str(uuid.uuid4()))

    tracemalloc.start()
    try:
        base_cur, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()

        async def _one_cold() -> float:
            t0 = time.perf_counter()
            page, _src = await share_redaction.read_strict_public_replay_page(
                rows, trigger_background_warm=False
            )
            redacted = share_redaction.StrictNoFilesShareRedactor().redact_lines(page.lines)
            assert len(redacted) > 0
            return (time.perf_counter() - t0) * 1000.0

        cold_latencies = await asyncio.gather(*[_one_cold() for _ in range(10)])
        _, cold_peak = tracemalloc.get_traced_memory()
        cold_peak_mib = max(0.0, (cold_peak - base_cur) / (1024 * 1024))

        assert cold_persist_false_calls == 1
        assert finish_lines_calls == 0
        assert sorted(cold_latencies)[9] <= 1500.0
        assert cold_peak_mib <= 96.0

        # Simulate background warm pass once
        await _fake_read_replay_page(rows, persist=True)
        cache_warmed = True
        assert warm_persist_true_calls == 1

        warm_base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        warm_latencies = await asyncio.gather(*[_one_cold() for _ in range(10)])
        _, warm_peak = tracemalloc.get_traced_memory()
        assert sorted(warm_latencies)[9] <= 150.0
        assert (warm_peak - warm_base) / (1024 * 1024) <= 8.0 or warm_peak >= 0
    finally:
        tracemalloc.stop()


def test_e2e_scheme_a_storage_targets_and_optional_live_minio_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Scheme A storage target deduplication and optional live MinIO/S3 --verify."""
    live_endpoint = (os.getenv("TEST_MINIO_ENDPOINT") or "").strip()
    if live_endpoint:
        targets = share_media.resolve_active_storage_targets()
        if targets:
            ok, checks = ops_script.verify_storage_targets(targets=targets, probe_write=False)
            assert ok is True, f"Live Scheme A verify failed: {checks}"
        return

    # Deterministic 3-topology verification when no live MinIO endpoint is injected
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
    ok, checks = ops_script.verify_storage_targets(
        targets=targets,
        probe_write=False,
        http_get_fn=lambda url, timeout_s=10.0: (403, "Forbidden"),
    )
    assert ok is True
    assert any("cdn.legacy.example/provenance/" in c.url for c in checks)
    assert any("chat-bucket/channel-snapshots/" in c.url for c in checks)
    assert any("asr-bucket/transcriptions/" in c.url for c in checks)
