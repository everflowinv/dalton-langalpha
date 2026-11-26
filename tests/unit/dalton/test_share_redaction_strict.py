"""Unit tests for AU1 strict share redaction pure functions, phase splitting, and storage target dedup.

Covers §6.2.1 and B2/B3/B4/B9 edge regressions:
- Mixed-phase AIMessage splitting via ``src.dalton.history_phase.split_content_blocks_by_phase``
- Pre-filtering of adjacent ``reasoning`` / ``reasoning_signal`` chunks (S08)
- Full-text ``strip_inline_thinking_markers`` (>3000 chars, nested, whitespace, truncated, NEED MORE THINKING, code literal preservation) (S08, B2)
- Turn status gating (completed vs error / cancelled / stopped / in_progress / open_runs / pure-tools) (S09)
- Main-channel whitelist blocking ``task:*`` and ``subagent:*`` fallback frames (S07, B9)
- Artifact/preview grant URLs, all 3 bucket sensitive prefixes, standalone ``/a/{code}`` preservation (S03, S16, B4)
- ``mermaid`` / ``~~~mermaid`` / ``MERMAID`` / nested fences downgraded to ``text`` with image URLs scrubbed (S13, B3)
- Markdown link ``title``, reference definitions, dangling definitions, fence extra ``info``, HTML resource attrs & CSS ``url()`` stripped (S14, S15, S16, S18)
- Safe share title ignoring ``thread.title`` after Turn 0 edit (S01)
- Org library fixed generic title & ``summary`` grant scrubbing (S01)
- Nested notification 9-key whitelist (S06)
- Top-level JSON scalar scanner on 16 MiB tool frame
- Three-backend physical bucket deduplication ``resolve_active_storage_targets()``
- Migration ``d059_share_redaction_meta`` schema contract
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec
import uuid

from langchain_core.messages import AIMessage
import pytest

from src.dalton.history_phase import split_content_blocks_by_phase
from src.dalton.migrations.versions import d059_share_redaction_meta as d059
from src.dalton.share_media import (
    candidate_upstream_keys,
    is_public_avatar_key,
    resolve_active_storage_targets,
    storage_private_acl_kwargs,
)
from src.dalton.share_redaction import (
    CANCELLED_TURN_NOTICE,
    DEFAULT_SHARED_TITLE,
    FAILED_TURN_NOTICE,
    IN_PROGRESS_TURN_NOTICE,
    MARKET_ALERT_FIXED_NOTICE,
    SAFE_NOTIFICATION_FIELDS,
    TOOLS_ONLY_TURN_NOTICE,
    WAKEUP_FIXED_NOTICE,
    StrictNoFilesShareRedactor,
    clean_org_library_summary,
    extract_tool_line_meta,
    rebuild_user_message_metadata,
    resolve_safe_share_title,
    sanitize_shared_markdown,
    strip_inline_thinking_markers,
)
from src.server.services.history.projector import _project_ai_message
from src.server.services.history.replay.lines import line_of
from src.server.services.preview_base import PreviewBase


def test_split_content_blocks_by_phase_mixed_and_uniform() -> None:
    uniform_content = [
        {"type": "thinking", "thinking": "plan step"},
        {"type": "text", "text": "Part 1. ", "phase": "final_answer"},
        {"type": "text", "text": "Part 2.", "phase": "final_answer"},
    ]
    segs, reasoning = split_content_blocks_by_phase(uniform_content, followed_by_tool_call=False)
    assert reasoning == "plan step"
    assert segs == [("Part 1. Part 2.", "final_answer")]

    mixed_content = [
        {"type": "text", "text": "SECRET_COMMENTARY_09", "phase": "commentary"},
        {"type": "text", "text": "Final thesis.", "phase": "final_answer"},
    ]
    segs2, reasoning2 = split_content_blocks_by_phase(mixed_content, followed_by_tool_call=False)
    assert reasoning2 is None
    assert segs2 == [
        ("SECRET_COMMENTARY_09", "commentary"),
        ("Final thesis.", "final_answer"),
    ]

    # Verify projector emits two separate text HistoryEvents with finish_reason=None on first, "stop" on last
    ai = AIMessage(id="m-mixed", content=mixed_content)
    projected = _project_ai_message(ai, {}, "main")
    text_evs = [e for e in projected if e.kind == "text"]
    assert len(text_evs) == 2
    assert text_evs[0].data["phase"] == "commentary"
    assert text_evs[0].data["finish_reason"] is None
    assert text_evs[1].data["phase"] == "final_answer"
    assert text_evs[1].data["finish_reason"] == "stop"


def test_strip_inline_thinking_markers_s08_and_b2() -> None:
    filler = "A" * 2600
    long_cot = "SECRET_COT_LONG_08 " * 200  # > 3000 chars
    raw = (
        f"Intro {filler} "
        "<thinking>SECRET_COT_MID_08</thinking> "
        f"<thinking_mode>{long_cot}</thinking_mode>\n\n"
        "NEED MORE THINKING\nSECRET_COT_NEED_08\n\n"
        "Formal conclusion with inline `<thinking>` literal.\n"
        "```python\n# <thinking>kept inside fence</thinking>\n```\n"
        "<thinking><thinking>INNER_NESTED</thinking>OUTER_NESTED</thinking>"
        "< Thinking_Mode >WHITESPACE_TAG</ thinking_mode >"
        "<thinking>SECRET_COT_TAIL_08"
    )
    cleaned = strip_inline_thinking_markers(raw)
    for secret in (
        "SECRET_COT_MID_08",
        "SECRET_COT_LONG_08",
        "SECRET_COT_NEED_08",
        "SECRET_COT_TAIL_08",
        "INNER_NESTED",
        "OUTER_NESTED",
        "WHITESPACE_TAG",
    ):
        assert secret not in cleaned
    assert "Formal conclusion with inline `<thinking>` literal." in cleaned
    assert "# <thinking>kept inside fence</thinking>" in cleaned

    # Half-written truncated tag at tail (B2)
    assert strip_inline_thinking_markers("Answer complete.<thinking") == "Answer complete."
    assert strip_inline_thinking_markers("Answer complete.<thinking_mo") == "Answer complete."


def test_strict_redactor_adjacent_reasoning_subagent_fallback_and_turn_statuses() -> None:
    tid = str(uuid.uuid4())
    lines = [
        # Turn 0: completed, with adjacent reasoning chunk (no tool call between reasoning & final text)
        # + subagent task:* user_message + subagent:call_07 fallback text frame (B9)
        # + commentary + html_widget + credit_usage + legacy_debug
        line_of(
            {
                "event": "user_message",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "content": "Summarize thesis <file-comments>SECRET_FILE_QUOTE_02</file-comments>",
                    "metadata": {
                        "chart_selections": [{"comment": "SECRET_CHART_COMMENT_04", "bars": [98765.43]}],
                        "widget_contexts": [{"html": "SECRET_WIDGET_SNAP_05"}],
                        "attachments": [{"name": "a.pdf", "url": "https://cdn.example/attachments/a.pdf"}],
                    },
                },
            }
        ),
        line_of(
            {
                "event": "user_message",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "agent": "task:sub1",
                    "content": "SECRET_SUB_INPUT_07",
                },
            }
        ),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "agent": "subagent:call_07",
                    "role": "assistant",
                    "content": "SECRET_SUBAGENT_FALLBACK_07",
                    "content_type": "text",
                    "finish_reason": "stop",
                },
            }
        ),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "agent": "main",
                    "role": "assistant",
                    "content": "SECRET_COT_ADJ_08",
                    "content_type": "reasoning",
                },
            }
        ),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "agent": "main",
                    "role": "assistant",
                    "content": "SECRET_COMMENTARY_09",
                    "content_type": "text",
                    "phase": "commentary",
                },
            }
        ),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "agent": "main",
                    "role": "assistant",
                    "content": "Visible conclusion.<thinking>SECRET_COT_MID_08</thinking>",
                    "content_type": "text",
                    "phase": "final_answer",
                    "finish_reason": "stop",
                },
            }
        ),
        line_of(
            {
                "event": "artifact",
                "data": {
                    "thread_id": tid,
                    "turn_index": 0,
                    "agent": "main",
                    "artifact_type": "html_widget",
                    "artifact_id": "w-11",
                    "payload": {
                        "title": "SECRET_WIDGET_TITLE_11",
                        "html": "<div>SECRET_WIDGET_HTML_11</div>",
                        "data": {"x": "SECRET_WIDGET_DATA_11"},
                    },
                },
            }
        ),
        line_of({"event": "credit_usage", "data": {"thread_id": tid, "turn_index": 0, "total_credits": 99}}),
        line_of({"event": "legacy_debug", "data": {"thread_id": tid, "turn_index": 0, "dump": "SECRET_VERBATIM_17"}}),
        # Turn 1: failed turn with partial draft
        line_of({"event": "user_message", "data": {"thread_id": tid, "turn_index": 1, "content": "Run failing check"}}),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 1,
                    "agent": "main",
                    "role": "assistant",
                    "content": "SECRET_FAILED_ARCHIVE_09",
                    "content_type": "text",
                },
            }
        ),
        line_of({"event": "error", "data": {"thread_id": tid, "turn_index": 1, "agent": "main", "error": "boom"}}),
        # Turn 2: cancelled/stopped turn
        line_of({"event": "user_message", "data": {"thread_id": tid, "turn_index": 2, "content": "Run stopped check"}}),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 2,
                    "agent": "main",
                    "role": "assistant",
                    "content": "SECRET_STOPPED_TAIL_09",
                    "content_type": "text",
                    "finish_reason": "stopped",
                },
            }
        ),
        # Turn 3: in-progress turn
        line_of({"event": "user_message", "data": {"thread_id": tid, "turn_index": 3, "content": "Still running"}}),
        line_of(
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": tid,
                    "turn_index": 3,
                    "agent": "main",
                    "role": "assistant",
                    "content": "SECRET_IN_PROGRESS_DRAFT",
                    "content_type": "text",
                },
            }
        ),
        # Turn 4: completed pure-tool turn
        line_of({"event": "user_message", "data": {"thread_id": tid, "turn_index": 4, "content": "Only run tools"}}),
        line_of(
            {
                "event": "tool_calls",
                "data": {
                    "thread_id": tid,
                    "turn_index": 4,
                    "agent": "main",
                    "tool_calls": [{"id": "c1", "name": "Read", "args": {"file_path": "SECRET_TOOL_10"}}],
                },
            }
        ),
    ]

    redactor = StrictNoFilesShareRedactor(
        thread_id=tid,
        responses_by_turn={
            0: {"status": "completed"},
            1: {"status": "error"},
            2: {"status": "cancelled"},
            4: {"status": "completed"},
        },
        open_runs={3: "run-3"},
    )
    out = list(redactor.iter_redacted_events(lines))
    dumped = json.dumps(out, ensure_ascii=False)

    for forbidden in (
        "SECRET_FILE_QUOTE_02",
        "SECRET_CHART_COMMENT_04",
        "98765.43",
        "SECRET_WIDGET_SNAP_05",
        "SECRET_SUB_INPUT_07",
        "SECRET_SUBAGENT_FALLBACK_07",
        "SECRET_COT_ADJ_08",
        "SECRET_COT_MID_08",
        "SECRET_COMMENTARY_09",
        "SECRET_WIDGET_TITLE_11",
        "SECRET_WIDGET_HTML_11",
        "SECRET_WIDGET_DATA_11",
        "SECRET_VERBATIM_17",
        "SECRET_FAILED_ARCHIVE_09",
        "SECRET_STOPPED_TAIL_09",
        "SECRET_IN_PROGRESS_DRAFT",
        "SECRET_TOOL_10",
    ):
        assert forbidden not in dumped

    # Turn 0 output checks
    t0_chunks = [e["data"] for e in out if e["event"] == "message_chunk" and e["data"]["turn_index"] == 0]
    assert len(t0_chunks) == 1
    assert t0_chunks[0]["content"] == "Visible conclusion."
    t0_widgets = [e["data"] for e in out if e["event"] == "artifact" and e["data"]["turn_index"] == 0]
    assert len(t0_widgets) == 1
    assert t0_widgets[0]["payload"] == {"redacted": True}

    # Turn 1/2/3/4 fixed notices
    by_turn = {
        e["data"]["turn_index"]: e["data"]["content"]
        for e in out
        if e["event"] == "message_chunk"
    }
    assert by_turn[1] == FAILED_TURN_NOTICE
    assert by_turn[2] == CANCELLED_TURN_NOTICE
    assert by_turn[3] == IN_PROGRESS_TURN_NOTICE
    assert by_turn[4] == TOOLS_ONLY_TURN_NOTICE


def test_markdown_grants_mermaid_fences_html_and_b3_b4(monkeypatch: pytest.MonkeyPatch) -> None:
    art_id = str(uuid.uuid4())
    base = PreviewBase("https", "preview.example.com")
    monkeypatch.setattr("src.server.services.preview_base.preview_base", lambda: base)
    monkeypatch.setattr("src.server.services.preview_base.artifact_base", lambda: base)

    grant_tok = "v1.grant_token_secret_16"
    cap_tok = "a2345672345672345672345672345672"
    art_url = base.artifact_url(art_id, grant_tok) + "v/1/index.html"
    cap_url = base.url(8080, cap_tok)
    api_grant_url = f"https://app.example.com/api/v1/artifacts/g/{grant_tok}/v/1/index.html"
    mon_grant_url = f"https://app.example.com/api/v1/dalton/monitors/g/{grant_tok}/board.html"
    owner_media_url = f"https://app.example.com/api/v1/threads/{art_id}/media/tok_secret_19"
    pub_short = "https://app.example.com/a/AbCdEf123456"

    # B4 variants: uppercase host, explicit :443, percent-encoded %67, Chinese punctuation adjacency
    b4_url = f"HTTPS://ARTIFACT-{art_id.upper()}.PREVIEW.EXAMPLE.COM:443/%67/{grant_tok}/v/1/index.html"

    md = (
        f"See [Report]({art_url} \"SECRET_LINK_TITLE_16\") and [Cap]({cap_url}) and [API]({api_grant_url}) "
        f"and [Mon]({mon_grant_url}) and [Media]({owner_media_url}) and [Public]({pub_short} \"SECRET_PUB_TITLE\").\n\n"
        f"Punctuation test：（{b4_url}）与「8080-{cap_tok}.preview.example.com」。\n\n"
        "Storage URLs: https://cdn.example/provenance/abc123 "
        "https://cdn.example/memo/u1/doc.pdf "
        "https://cdn.example/user-skills/u1/h.zip "
        "https://cdn.example/org-library/o1/h.zip "
        "https://cdn.example/channel-snapshots/t1/1234567890abcdef/chart_a.png "
        "https://cdn.example/transcriptions/ws1/job1/source/a.wav\n\n"
        "![https://cdn.custom-domain.example/charts/SECRET_ALT_URL_13.png](https://cdn.custom-domain.example/charts/SECRET_CDN_IMG_13.png \"SECRET_IMG_TITLE\")\n\n"
        "![Matrix][ref14]\n\n"
        "[ref14]: https://cdn.custom-domain.example/SECRET_REF_IMG_14.png \"SECRET_REF_TITLE_14\"\n"
        "[unused]: https://example.com/public \"SECRET_UNUSED_DEF_16\"\n\n"
        "```mermaid\nA[\"<img src='https://cdn.custom-domain.example/charts/SECRET_MERMAID_IMG_13.png'/>\"] --> B[\"Done\"]\n```\n\n"
        "~~~MERMAID\nX[\"https://cdn.custom-domain.example/b3_img.webp\"] --> Y\n~~~\n\n"
        "````mermaid\nM[\"inner\"] --> N\n```\ninner ticks\n```\n````\n\n"
        "````python title=\"/Users/secret_alice_01/ws/SECRET_FENCE_INFO_18.py\"\n"
        "url = \"https://cdn.example/blobs/u1/SECRET_CODE_URL_18\"\n"
        "````\n\n"
        "<span style=\"background-image: url('https://cdn.custom-domain.example/SECRET_CSS_URL_15.png')\" title=\"SECRET_SPAN_TITLE\">SafeText</span> "
        "<a href=\"https://example.com/ok\" title=\"SECRET_HTML_A_TITLE_16\">OKLink</a> "
        "<svg><foreignObject><img src=\"https://cdn.custom-domain.example/SECRET_FO_15.png\"/></foreignObject></svg>"
    )

    out = sanitize_shared_markdown(md, roots=("/Users/secret_alice_01/ws",))

    for forbidden in (
        grant_tok,
        cap_tok,
        "tok_secret_19",
        "SECRET_LINK_TITLE_16",
        "SECRET_PUB_TITLE",
        "SECRET_ALT_URL_13.png",
        "SECRET_CDN_IMG_13.png",
        "SECRET_IMG_TITLE",
        "SECRET_REF_IMG_14.png",
        "SECRET_REF_TITLE_14",
        "SECRET_UNUSED_DEF_16",
        "SECRET_MERMAID_IMG_13.png",
        "b3_img.webp",
        "SECRET_FENCE_INFO_18.py",
        "SECRET_CODE_URL_18",
        "SECRET_CSS_URL_15.png",
        "SECRET_SPAN_TITLE",
        "SECRET_HTML_A_TITLE_16",
        "SECRET_FO_15.png",
        "```mermaid",
        "~~~MERMAID",
        "foreignObject",
        "provenance/abc123",
        "memo/u1/doc.pdf",
        "user-skills/u1/h.zip",
        "org-library/o1/h.zip",
        "channel-snapshots/t1",
        "transcriptions/ws1",
    ):
        assert forbidden not in out, f"Leaked: {forbidden!r}\nOUTPUT:\n{out}"

    # Standalone /a/{code} short link MUST be preserved without title
    assert f"[Public]({pub_short})" in out
    # Images converted to <redacted-image>
    assert '<redacted-image alt=""></redacted-image>' in out
    assert '<redacted-image alt="Matrix"></redacted-image>' in out
    # Mermaid downgraded to ```text
    assert "```text" in out
    # Chinese outer punctuation preserved (B4)
    assert "（[redacted-url]）" in out
    assert "「[redacted-url]」" in out
    # Safe HTML text & href preserved without title/style
    assert "SafeText" in out
    assert '<a href="https://example.com/ok">OKLink</a>' in out


def test_safe_title_summary_and_nested_notifications_9_keys() -> None:
    # S01: Turn 0 edited from comment to ordinary query while thread.title kept SECRET_COMMENT_TITLE_01
    queries = [{"turn_index": 0, "type": "initial", "content": "请总结核心结论"}]
    assert resolve_safe_share_title(queries) == "请总结核心结论"
    # Notification or pure comment -> Shared Conversation
    assert resolve_safe_share_title([{"turn_index": 0, "type": "wakeup", "content": "SECRET"}]) == DEFAULT_SHARED_TITLE
    assert (
        resolve_safe_share_title(
            [{"turn_index": 0, "type": "initial", "content": "<file-comments>SECRET_ONLY</file-comments>"}]
        )
        == DEFAULT_SHARED_TITLE
    )

    # S01 summary scrubbing
    summary = (
        "Summary <file-comments>SECRET_FC</file-comments> "
        "see /api/v1/artifacts/g/v1.grant_lib_summary_01/v/1/a.html "
        "and /Users/alice/ws/secret.py and https://app.local/a/AbCdEf123456"
    )
    cleaned_sum = clean_org_library_summary(summary, roots=("/Users/alice/ws",))
    assert "SECRET_FC" not in cleaned_sum
    assert "grant_lib_summary_01" not in cleaned_sum
    assert "/Users/alice/ws" not in cleaned_sum
    assert "https://app.local/a/AbCdEf123456" in cleaned_sum

    # S06 nested notification 9-key whitelist
    raw_meta = {
        "msg_type": "wakeup",
        "chart_selections": [{"secret": 1}],
        "attachments": [{"url": "https://x/a.png"}],
        "wakeup": {
            "v": 1,
            "notices": [
                {
                    "wakeup_id": "w-1",
                    "short_id": "s1",
                    "label": "SECRET_WAKE_LABEL_06",
                    "cron": "17 4 * * 3",
                    "kind": "time",
                    "notice": "fired",
                    "seq": 2,
                    "total": 5,
                    "last": False,
                    "skipped": 0,
                    "fired_at": "2026-11-26T10:00:00Z",
                    "timezone": "UTC",
                    "target_id": "t-wake-1",
                    "price": {"symbol": "SECRET_WAKE_SYM_06", "target": 987.65},
                    "order": {"order_id": "SECRET_ORD_06"},
                }
            ],
        },
        "market_target": {
            "notices": [
                {
                    "target_id": "t-1",
                    "kind": "price",
                    "notice": "hit",
                    "seq": 1,
                    "total": 3,
                    "last": False,
                    "skipped": 0,
                    "fired_at": "2026-11-26T10:05:00Z",
                    "timezone": "UTC",
                    "symbol": "SECRET_TICKER_06",
                    "armed_price": 950.01,
                    "fired_price": 988.12,
                }
            ]
        },
    }
    safe_meta = rebuild_user_message_metadata(raw_meta)
    dumped = json.dumps(safe_meta)
    for forbidden in (
        "SECRET_WAKE_LABEL_06",
        "17 4 * * 3",
        "SECRET_WAKE_SYM_06",
        "987.65",
        "SECRET_ORD_06",
        "SECRET_TICKER_06",
        "950.01",
        "988.12",
        "wakeup_id",
        "chart_selections",
        "attachments",
    ):
        assert forbidden not in dumped
    assert set(safe_meta["wakeup"]["notices"][0].keys()) == SAFE_NOTIFICATION_FIELDS
    assert set(safe_meta["market_target"]["notices"][0].keys()) == SAFE_NOTIFICATION_FIELDS

    # Steering notification fixed notices
    red = StrictNoFilesShareRedactor(thread_id="t1", responses_by_turn={0: {"status": "completed"}})
    rebuilt_steer = red._rebuild_steering(
        {
            "agent": "main",
            "timestamp": "2026-11-26T10:00:00Z",
            "messages": [
                {"content": "SECRET_REPORT", "report_source": {"thread_id": "other"}},
                {
                    "content": "SECRET_WAKE_BODY_06",
                    "timestamp": "2026-11-26T10:01:00Z",
                    "wakeup": {"kind": "order", "notice": "filled", "label": "SECRET_WAKE_LABEL_06", "order": {"id": "SECRET_ORD_06"}},
                },
                {
                    "content": "SECRET_ALERT_BODY_06",
                    "timestamp": "2026-11-26T10:02:00Z",
                    "market_alert": {"target_id": "mt1", "kind": "price", "notice": "hit", "symbol": "SECRET_TICKER_06"},
                },
            ],
        },
        turn_index=0,
    )
    assert rebuilt_steer is not None
    assert len(rebuilt_steer["messages"]) == 2
    assert rebuilt_steer["messages"][0]["content"] == WAKEUP_FIXED_NOTICE
    assert rebuilt_steer["messages"][1]["content"] == MARKET_ALERT_FIXED_NOTICE
    assert "SECRET_" not in json.dumps(rebuilt_steer)


def test_top_level_tool_scanner_on_16mib_payload() -> None:
    huge = "x" * (16 * 1024 * 1024)
    line_json = (
        '{"thread_id":"t1","turn_index":7,"agent":"main",'
        f'"content":"{huge}",'
        '"tool_calls":[{"id":"1","args":{}},{"id":"2","args":{}}]}'
    )
    agent, turn_index, count = extract_tool_line_meta(line_json, include_call_count=True)
    assert agent == "main"
    assert turn_index == 7
    assert count == 2


def test_resolve_active_storage_targets_dedup_topologies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clean all storage env vars first
    for key in (
        "STORAGE_PROVIDER",
        "STORAGE_BUCKET_NAME",
        "STORAGE_REGION",
        "STORAGE_ENDPOINT_URL",
        "STORAGE_PUBLIC_URL_BASE",
        "STORAGE_KEY_PREFIX",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "S3_BUCKET_NAME",
        "S3_REGION",
        "S3_ENDPOINT_URL",
        "S3_PUBLIC_URL_BASE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "OSS_BUCKET_NAME",
        "OSS_ENDPOINT",
        "OSS_REGION",
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
        "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX",
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_ASR_STORAGE_KEY_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)

    # Topology 1: 3 distinct physical buckets (legacy S3_* primary + dedicated chat upload + independent ASR)
    monkeypatch.setenv("S3_BUCKET_NAME", "primary-legacy-bucket")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_PUBLIC_URL_BASE", "https://cdn-primary.example.com")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ak1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk1")

    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-bucket")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "ak2")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "sk2")

    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "asr-bucket")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "ak3")
    monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "sk3")

    targets = resolve_active_storage_targets()
    assert len(targets) == 3
    by_bucket = {t.bucket_name: t for t in targets}
    assert "provenance/" in by_bucket["primary-legacy-bucket"].sensitive_prefixes
    assert "memo/" in by_bucket["primary-legacy-bucket"].sensitive_prefixes
    assert "user-skills/" in by_bucket["primary-legacy-bucket"].sensitive_prefixes
    assert "org-library/" in by_bucket["primary-legacy-bucket"].sensitive_prefixes
    assert "https://cdn-primary.example.com" in by_bucket["primary-legacy-bucket"].public_url_bases
    assert "channel-snapshots/" in by_bucket["chat-bucket"].sensitive_prefixes
    assert "transcriptions/" in by_bucket["asr-bucket"].sensitive_prefixes

    # Topology 2: chat_upload falls back to ASR bucket -> deduplicates to 2 physical buckets
    monkeypatch.delenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME")
    targets2 = resolve_active_storage_targets()
    assert len(targets2) == 2
    by_bucket2 = {t.bucket_name: t for t in targets2}
    assert set(by_bucket2["asr-bucket"].roles) == {"chat_upload", "transcription"}
    assert "channel-snapshots/" in by_bucket2["asr-bucket"].sensitive_prefixes
    assert "transcriptions/" in by_bucket2["asr-bucket"].sensitive_prefixes

    # Private ACL skips avatars/ only
    assert is_public_avatar_key("avatars/u1.png") is True
    assert storage_private_acl_kwargs("avatars/u1.png") == {}
    assert storage_private_acl_kwargs("blobs/u1/abc") == {"ACL": "private"}
    assert storage_private_acl_kwargs("provenance/abc", provider="oss") == {"acl": "private"}
    assert candidate_upstream_keys("widgets/t1/sha.json") == (
        "private-media/widgets/t1/sha.json",
        "widgets/t1/sha.json",
    )


def test_d059_migration_contract() -> None:
    assert d059.revision == "d059"
    assert d059.down_revision == "d055"
    executed: list[str] = []
    fake_op = create_autospec(d059.op, instance=False)
    fake_op.execute.side_effect = lambda sql: executed.append(str(sql))
    orig_op = d059.op
    try:
        d059.op = fake_op  # type: ignore[assignment]
        d059.upgrade()
        joined = "\n".join(executed)
        assert "dalton_org_library_title_meta" in joined
        assert "dalton_storage_key_migrations" in joined
        executed.clear()
        d059.downgrade()
        joined_down = "\n".join(executed)
        assert "DROP TABLE IF EXISTS dalton_storage_key_migrations" in joined_down
        assert "DROP TABLE IF EXISTS dalton_org_library_title_meta" in joined_down
    finally:
        d059.op = orig_op


def test_need_more_thinking_with_inline_code_strips_entire_paragraph() -> None:
    """Codex High: NEED MORE THINKING paragraphs containing inline code must be stripped completely."""
    raw = (
        "NEED MORE THINKING\n"
        "Check `secret_model.py` and verify SECRET_AFTER_CODE_01 before answering.\n\n"
        "正式回答：保留这段内容。\n\n"
        "NEED MORE THINKING: inspect `a/b.csv` then `c/d.json` and SECRET_TAIL_02."
    )
    cleaned = strip_inline_thinking_markers(raw)
    assert "NEED MORE THINKING" not in cleaned
    assert "secret_model.py" not in cleaned
    assert "SECRET_AFTER_CODE_01" not in cleaned
    assert "a/b.csv" not in cleaned
    assert "SECRET_TAIL_02" not in cleaned
    assert "正式回答：保留这段内容。" in cleaned

    # Literal inline code mid-paragraph and fenced code remain untouched
    safe = (
        "Mentioning `NEED MORE THINKING` inside a sentence stays.\n\n"
        "```text\nNEED MORE THINKING\nin fence\n```"
    )
    cleaned_safe = strip_inline_thinking_markers(safe)
    assert "`NEED MORE THINKING`" in cleaned_safe
    assert "in fence" in cleaned_safe


def test_markdown_reassembly_prevents_escaped_image_and_html_injection() -> None:
    """Codex High: Escaped Markdown images / HTML must not become active nodes after AST serialization."""
    from src.dalton.share_redaction import _MD_PARSER

    payloads = [
        r"\![secret alt](https://evil.example/leak.png)",
        r"\<img src='https://evil.example/leak2.png' onerror='alert(1)'>",
        r"&lt;img src=&quot;https://evil.example/leak3.png&quot;&gt;",
        r"\[click](https://evil.example/secret.png)",
        r"\<svg onload='alert(1)'><image href='https://evil.example/s.svg'/></svg>",
    ]
    for payload in payloads:
        sanitized = sanitize_shared_markdown(payload)
        assert "leak.png" not in sanitized
        assert "leak2.png" not in sanitized
        assert "leak3.png" not in sanitized
        assert "secret.png" not in sanitized
        assert "s.svg" not in sanitized
        # Re-parse serialized Markdown and assert zero active image / html / link nodes
        tokens = _MD_PARSER.parse(sanitized)
        stack = list(tokens)
        while stack:
            tok = stack.pop()
            if tok.children:
                stack.extend(tok.children)
            assert tok.type != "image", f"Unexpected image token from {payload!r} -> {sanitized!r}"
            if tok.type in ("html_inline", "html_block"):
                assert tok.content.startswith("<redacted-image") or tok.content == "</redacted-image>", (
                    f"Unexpected HTML token {tok.content!r} from {payload!r} -> {sanitized!r}"
                )
            assert tok.type != "link_open", f"Unexpected link token from {payload!r} -> {sanitized!r}"


def test_workspace_relative_paths_scrubbed_consistently_across_markdown_and_code() -> None:
    """Grok Medium: Workspace-relative paths (reports/q3.pdf, `out/memo.md`) scrubbed everywhere."""
    md = (
        "详见 reports/q3.pdf 和 `out/memo.md` 以及 ./charts/fig1.png。\n\n"
        "指标 P/E 为 18.5，日期 2025/11/26，保留 [短链](https://app.local/a/AbCdEf123456) "
        "和 https://example.com/docs/guide.html。\n\n"
        "```python\n"
        "df = read_csv('data/q3/metrics.csv')\n"
        "```\n"
    )
    sanitized = sanitize_shared_markdown(md)
    assert "reports/q3.pdf" not in sanitized
    assert "out/memo.md" not in sanitized
    assert "./charts/fig1.png" not in sanitized
    assert "data/q3/metrics.csv" not in sanitized
    assert "[redacted-path]" in sanitized
    # Benign ratios, dates, and public URLs / short links preserved
    assert "P/E" in sanitized
    assert "2025/11/26" in sanitized
    assert "https://app.local/a/AbCdEf123456" in sanitized
    assert "https://example.com/docs/guide.html" in sanitized

    # Org library summary scrubbing also removes workspace-relative paths
    cleaned_sum = clean_org_library_summary("详见 reports/q3.pdf 与 out/memo.md")
    assert "reports/q3.pdf" not in cleaned_sum
    assert "out/memo.md" not in cleaned_sum
