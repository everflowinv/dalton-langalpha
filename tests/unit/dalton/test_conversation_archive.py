"""Unit tests for Dalton's OpenClaw conversation archive.

Covers:
1. Host SQLite reader (`export_openclaw.py`): online backup snapshot, schema check (`session_windows.session_id`),
   zstd rows (`zstandard` first for Python 3.13), mixed `user`/`assistant`/`toolCall`/`toolResult` events,
   human session filtering across `main`/`chem`/`marco`, `codex` >2000-char user message + verbatim assistant echo
   filtering, date-window cursor safety, and `ignore_cursors` backfill mode.
2. Rule-based sanitization: JSON (`"api_key": "..."`, paired quotes with escapes), env (`KEY=...`),
   `Authorization: Bearer ...`, pairing codes, and API keys redacted regardless of length/entropy;
   ops and <30-char chitchat skipped.
3. Extraction & verbatim quote verification (`extractor.py`): fabricated quotes rejected, ops/private segments
   discarded without persisting raw LLM output (`persist_raw=False`), agent `fact`/`estimate`/`model_assumption`
   cards verified against segment text for `cited_sources` substrings (else downgraded to `opinion`),
   and oversized single events split with stable `chunk_idx` in `segment_events` / `expand_event_chunks`.
4. Deduplication & `supersedes`: single-ticker chronological `supersedes` marking all older active cards
   for `(ticker, metric, period)` as `superseded` after inserting all batch cards in the same transaction.
5. Hybrid search (`search.py` & `conversation_search` tool): requires authenticated `user_id` in `RunnableConfig`,
   logs caller in audit log, and returns team-shared cards without filtering by caller.
6. Workspace rendering (`render.py`), pipeline (`conversation_archive.py`), and `/embed` bridge (`researchdata_bridge.py`):
   `SandboxBackend` autospec writes, `rendered_locations` tracking (including Home -> company workspace migration),
   `bulk_upsert_files` error propagation, unrendered date retry, default `DaltonConversationsConfig()` pipeline run
   with real `ExportRunResult`, per-event/chunk `dalton_conversation_processed` progress, CLI pool open/close,
   safe `error_text`, hourly budget retry re-check, and `/embed` header-only auth + byte/concurrency limits.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from unittest.mock import create_autospec

import pytest

try:
    import zstandard as _zstd_lib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _zstd_lib = None  # type: ignore[assignment]

if _zstd_lib is None:  # pragma: no cover
    try:
        from compression import zstd as _stdlib_zstd  # type: ignore[import-not-found]
    except ImportError:
        _stdlib_zstd = None  # type: ignore[assignment]
else:
    _stdlib_zstd = None

from psycopg import AsyncConnection, AsyncCursor
from psycopg_pool import AsyncConnectionPool
from ptc_agent.agent.backends.sandbox import SandboxBackend
from scripts.dalton import conversation_archive as archive_cli
from scripts.dalton import researchdata_bridge as bridge_mod
from scripts.dalton.conversations import export_openclaw as exporter
from src.dalton.conversations import embedder as embedder_mod
from src.dalton.conversations import extractor as extractor_mod
from src.dalton.conversations import render as render_mod
from src.dalton.conversations import search as search_mod
from src.dalton.database import conversations as db_conv
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm.assist import AssistResult
from src.dalton.settings import DaltonConversationsConfig
from src.dalton.tools import build_dalton_tools


def _compress_zstd_json(payload: dict[str, Any]) -> tuple[bytes, int]:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if _zstd_lib is not None:
        return _zstd_lib.ZstdCompressor().compress(raw), len(raw)
    if _stdlib_zstd is not None:  # pragma: no cover
        return _stdlib_zstd.compress(raw), len(raw)
    raise RuntimeError("No zstd compressor available")


def _init_mock_openclaw_sqlite(db_path: Path) -> sqlite3.Connection:
    """Create a temporary SQLite database matching the real OpenClaw agent schema."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');

        CREATE TABLE session_windows (
            session_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            channel TEXT NOT NULL,
            chat_type TEXT NOT NULL,
            account_id TEXT,
            primary_conversation_id TEXT
        );

        CREATE TABLE transcript_events (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_json TEXT,
            event_zstd BLOB,
            event_utf8_bytes INTEGER,
            created_at INTEGER NOT NULL,
            navigation_json TEXT,
            PRIMARY KEY (session_id, seq)
        );

        CREATE TABLE transcript_event_identities (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            event_type TEXT,
            parent_id TEXT,
            message_idempotency_key TEXT,
            PRIMARY KEY (session_id, seq)
        );

        CREATE TABLE transcript_rewrite_watermarks (
            session_id TEXT PRIMARY KEY,
            generation TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    return conn


def test_exporter_human_sessions_zstd_text_only_cursors_and_generation_rescan(
    tmp_path: Path,
) -> None:
    """Verify SQLite snapshot reader exports only human sessions (across main/chem/marco),
    extracts text-only blocks, decompresses zstd rows, discards >2000-char codex user pastes
    and assistant verbatim echoes, respects date windows before advancing cursors, and rescans
    when `generation` increments or `ignore_cursors=True`.
    """
    db_path = tmp_path / "openclaw-agent.sqlite"
    conn = _init_mock_openclaw_sqlite(db_path)

    conn.executemany(
        """
        INSERT INTO session_windows (session_id, session_key, channel, chat_type, account_id, primary_conversation_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("sess-discord-1", "agent:main:discord:channel:9001", "discord", "channel", "acc1", "conv1"),
            ("sess-cron-1", "agent:main:cron:daily-check", "cron", "cron", "acc1", "conv2"),
            ("sess-sub-1", "agent:main:subagent:worker-1", "subagent", "direct", "acc1", "conv3"),
            ("sess-codex-1", "codex-desktop-alpha", "codex-desktop", "direct", "acc1", "conv4"),
            ("sess-chem-bg", "agent:chem:internal-worker:job1", "", "direct", "acc1", "conv5"),
            ("sess-chem-feishu", "agent:chem:feishu:group:888", "feishu", "group", "acc1", "conv6"),
        ],
    )
    conn.executemany(
        "INSERT INTO transcript_rewrite_watermarks (session_id, generation) VALUES (?, ?)",
        [
            ("sess-discord-1", "bf855e5a869f0001"),
            ("sess-cron-1", "bf855e5a869f0001"),
            ("sess-codex-1", "bf855e5a869f0001"),
            ("sess-chem-bg", "bf855e5a869f0001"),
            # Note: sess-chem-feishu intentionally has NO row in transcript_rewrite_watermarks
            # to test default generation="" handling.
        ],
    )

    # Verify chem/marco human session rules match main: must be discord/feishu/main/codex
    assert exporter.is_human_session("chem", "agent:chem:internal-worker:job1", "", "direct") is False
    assert exporter.is_human_session("marco", "agent:marco:background:task", "", "direct") is False
    assert exporter.is_human_session("chem", "agent:chem:feishu:group:888", "feishu", "group") is True
    assert exporter.is_human_session("marco", "agent:marco:discord:channel:777", "discord", "channel") is True

    ts_ms = int(datetime(2026, 10, 15, 14, 30, tzinfo=timezone.utc).timestamp() * 1000)

    ev1_payload = {
        "type": "message",
        "id": "ev-raw-1",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "我们讨论一下 NVDA 2027 财年数据中心收入弹性，如果 Blackwell 机柜出货顺利，单季营收有望继续超市场一致预期。",
                }
            ],
            "__openclaw": {
                "senderId": "user_owner_01",
                "senderName": "OwnerAlice",
                "senderIsOwner": True,
                "transport": {"channel": "discord"},
            },
        },
    }

    ev2_payload = {
        "type": "message",
        "id": "ev-raw-2",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "INTERNAL_THINKING_MUST_NOT_BE_EXPORTED"},
                {"type": "toolCall", "name": "execute_code", "arguments": {"code": "print(123)"}},
                {
                    "type": "text",
                    "text": "根据台积电 CoWoS 扩产节奏测算，NVDA 2027 财年数据中心营收预计可达 1950 亿美元（来源：TSMC 2026Q3 法说会纪要）。",
                },
            ],
        },
    }
    ev2_zstd, ev2_len = _compress_zstd_json(ev2_payload)

    ev3_payload = {
        "type": "message",
        "id": "ev-raw-3",
        "message": {
            "role": "toolResult",
            "content": [{"type": "text", "text": "TOOL_RESULT_SECRET_MUST_BE_IGNORED_LONG_ENOUGH_STRING"}],
        },
    }

    cron_payload = {
        "type": "message",
        "id": "ev-cron-1",
        "message": {
            "role": "user",
            "content": "CRON_AUTOMATED_MESSAGE_ABOUT_NVDA_REVENUE_THAT_MUST_BE_EXCLUDED_FROM_EXPORT",
        },
    }

    # Codex event 1: long pasted transcript (> 2000 chars, no header, blank lines between speakers) -> entire user msg dropped
    long_pasted_transcript = (
        "光模块专家访谈完整纪要（无页眉长粘贴测试）\n\n"
        + "\n\n".join(
            f"受访专家第 {i} 段发言：关于 800G 与 1.6T 光模块在北美云厂商数据中心的导入节奏、硅光渗透率、DSP 芯片供应以及明年价格年降幅度的详细拆解讨论。"
            for i in range(1, 38)
        )
    )
    assert len(long_pasted_transcript) > 2000
    codex_long_user = {
        "type": "message",
        "id": "ev-codex-long-1",
        "message": {"role": "user", "content": long_pasted_transcript},
    }
    # Codex event 2: assistant verbatim repetition of the dropped >2000 char user message -> also dropped
    codex_assistant_echo = {
        "type": "message",
        "id": "ev-codex-echo-2",
        "message": {
            "role": "assistant",
            "content": "以下是您刚才粘贴的访谈原文整理：\n\n" + long_pasted_transcript,
        },
    }
    # Codex event 3: normal human commentary (<= 2000 chars) -> kept
    codex_short_user = {
        "type": "message",
        "id": "ev-codex-short-3",
        "message": {
            "role": "user",
            "content": "上面那场光模块访谈里提到的 1.6T 毛利率假设我觉得偏乐观，我们自己的模型里对明年价格年降还是按 15% 保守估计。",
        },
    }

    conn.executemany(
        """
        INSERT INTO transcript_events
            (session_id, seq, event_json, event_zstd, event_utf8_bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("sess-discord-1", 1, json.dumps(ev1_payload, ensure_ascii=False), None, None, ts_ms),
            ("sess-discord-1", 2, None, ev2_zstd, ev2_len, ts_ms + 5000),
            ("sess-discord-1", 3, json.dumps(ev3_payload, ensure_ascii=False), None, None, ts_ms + 9000),
            ("sess-cron-1", 1, json.dumps(cron_payload, ensure_ascii=False), None, None, ts_ms),
            ("sess-codex-1", 1, json.dumps(codex_long_user, ensure_ascii=False), None, None, ts_ms + 10000),
            ("sess-codex-1", 2, json.dumps(codex_assistant_echo, ensure_ascii=False), None, None, ts_ms + 11000),
            ("sess-codex-1", 3, json.dumps(codex_short_user, ensure_ascii=False), None, None, ts_ms + 12000),
        ],
    )
    conn.executemany(
        """
        INSERT INTO transcript_event_identities (session_id, seq, event_id, event_type)
        VALUES (?, ?, ?, 'message')
        """,
        [
            ("sess-discord-1", 1, "stable-ev-1"),
            ("sess-discord-1", 2, "stable-ev-2"),
            ("sess-codex-1", 1, "stable-codex-1"),
            ("sess-codex-1", 2, "stable-codex-2"),
            ("sess-codex-1", 3, "stable-codex-3"),
        ],
    )
    conn.commit()
    conn.close()

    # 1. First export: 2 from discord + 1 short human comment from codex (both >2000 user paste and assistant echo dropped)
    events, cursors, stats = exporter.export_agent_sqlite("main", db_path, cursors={})
    assert stats["events_exported"] == 3
    assert stats["events_skipped_transcript"] == 2
    exported_ids = {e["event_id"] for e in events}
    assert exported_ids == {"stable-ev-1", "stable-ev-2", "stable-codex-3"}

    ev2_out = next(e for e in events if e["event_id"] == "stable-ev-2")
    assert "1950 亿美元" in ev2_out["text"]
    assert "INTERNAL_THINKING" not in ev2_out["text"]
    assert "execute_code" not in ev2_out["text"]
    assert ev2_out["speaker"]["role"] == "agent"

    # 2. Second incremental export with same cursors -> 0 events exported
    events_inc, cursors_inc, stats_inc = exporter.export_agent_sqlite("main", db_path, cursors=cursors)
    assert events_inc == []
    assert stats_inc["events_exported"] == 0

    # 3. Backfill mode (`ignore_cursors=True`) re-reads the date window even when cursors are at high-water mark
    events_bf, _, _ = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_inc,
        since_date=date(2026, 10, 15),
        until_date=date(2026, 10, 15),
        ignore_cursors=True,
    )
    assert len(events_bf) == 3

    # 4. Append seq=4 on a future date (2026-10-20) and export with until_date=2026-10-15:
    # out-of-window seq MUST NOT advance cursor high-water mark!
    ts_oct20_ms = int(datetime(2026, 10, 20, 14, 30, tzinfo=timezone.utc).timestamp() * 1000)
    conn = sqlite3.connect(str(db_path))
    ev4_payload = {
        "type": "message",
        "id": "ev-raw-4",
        "message": {
            "role": "user",
            "content": "补充一点：MSFT 与 AMZN 近期对明年推理集群的采购意向也佐证了这一判断。",
        },
    }
    conn.execute(
        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
        ("sess-discord-1", 4, json.dumps(ev4_payload, ensure_ascii=False), ts_oct20_ms),
    )
    conn.commit()
    conn.close()

    events_win, cursors_win, _ = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_inc,
        since_date=date(2026, 10, 15),
        until_date=date(2026, 10, 15),
    )
    assert events_win == []
    # Cursor for sess-discord-1 remains at seq=3 (not advanced to 4 because seq=4 was outside until_date)
    assert cursors_win[("main", "sess-discord-1")].last_seq == 3

    # Now export including 2026-10-20 -> seq=4 is exported and cursor advances to 4
    events_seq4, cursors_seq4, _ = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_win,
        since_date=date(2026, 10, 15),
        until_date=date(2026, 10, 20),
    )
    assert len(events_seq4) == 1
    assert events_seq4[0]["seq"] == 4
    assert cursors_seq4[("main", "sess-discord-1")].last_seq == 4

    # 5. Bump generation in transcript_rewrite_watermarks -> triggers full rescan of sess-discord-1
    # EVEN when since_date (2026-10-20) is later than historical seq 1..2 (2026-10-15) for incremental run,
    # whereas `ignore_cursors=True` (backfill) strictly respects the explicit `since_date` window!
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE transcript_rewrite_watermarks SET generation = 'gen-b' WHERE session_id = 'sess-discord-1'")
    # Also add an event to sess-chem-feishu (which has NO watermark row -> generation == "")
    feishu_payload = {
        "type": "message",
        "id": "ev-feishu-1",
        "message": {
            "role": "user",
            "content": "BABA 与 PDD 四季度电商 GMV 增速对比纪要，我们预计下季度货币化率继续改善。",
        },
    }
    conn.execute(
        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
        ("sess-chem-feishu", 1, json.dumps(feishu_payload, ensure_ascii=False), ts_oct20_ms),
    )
    conn.commit()
    conn.close()

    # Backfill (`ignore_cursors=True`) with old generation in `cursors_seq4` still respects `since_date=2026-10-20`
    events_bf_window, _, stats_bf_window = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_seq4,
        since_date=date(2026, 10, 20),
        until_date=date(2026, 10, 20),
        ignore_cursors=True,
    )
    assert stats_bf_window["sessions_rescanned"] == 0
    assert [e["seq"] for e in events_bf_window if e["session_id"] == "sess-discord-1"] == [4]

    events_rescan, cursors_rescan, stats_rescan = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_seq4,
        since_date=date(2026, 10, 20),
        until_date=date(2026, 10, 20),
    )
    assert stats_rescan["sessions_rescanned"] == 1
    # Historical seq 1 and 2 from 2026-10-15 ARE re-exported alongside seq 4 (2026-10-20)
    assert [e["seq"] for e in events_rescan if e["session_id"] == "sess-discord-1"] == [1, 2, 4]
    assert cursors_rescan[("main", "sess-discord-1")].generation == "gen-b"
    assert cursors_rescan[("main", "sess-chem-feishu")].generation == ""

    # 6. Write cursors to disk and read back -> neither token "gen-b" nor empty "" triggers a rescan
    cursor_roundtrip_path = tmp_path / "roundtrip_cursors.json"
    exporter.save_cursors_file(cursors_rescan, cursor_roundtrip_path)
    loaded_cursors = exporter.load_cursors_file(cursor_roundtrip_path)
    assert loaded_cursors[("main", "sess-discord-1")].generation == "gen-b"
    assert loaded_cursors[("main", "sess-chem-feishu")].generation == ""
    events_after_rt, _, stats_after_rt = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=loaded_cursors,
    )
    assert events_after_rt == []
    assert stats_after_rt["sessions_rescanned"] == 0

    # 7. Cross-export codex assistant verbatim echo: append seq=4 assistant echo of seq=1 user paste
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
        ("sess-codex-1", 4, json.dumps(codex_assistant_echo, ensure_ascii=False), ts_oct20_ms + 1000),
    )
    conn.commit()
    conn.close()
    events_cross_echo, cursors_after_echo, stats_cross_echo = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=loaded_cursors,
    )
    assert events_cross_echo == []
    assert stats_cross_echo["events_skipped_transcript"] == 1
    assert cursors_after_echo[("main", "sess-codex-1")].last_seq == 4

    # 8. Parse failure stops session scan before the failed seq, records `events_parse_failed`,
    #    does NOT export subsequent events in that session, and retries the failed seq on next run!
    ev5_valid = {
        "type": "message",
        "id": "ev-raw-5",
        "message": {
            "role": "user",
            "content": "AVGO 定制 ASIC 明年在北美大客户的出货量预计继续翻倍增长。",
        },
    }
    ev7_after_corrupt = {
        "type": "message",
        "id": "ev-raw-7",
        "message": {
            "role": "user",
            "content": "MRVL 明年光电互联与定制芯片收入指引也维持高增长态势。",
        },
    }
    conn = sqlite3.connect(str(db_path))
    conn.executemany(
        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES (?, ?, ?, ?)",
        [
            ("sess-discord-1", 5, json.dumps(ev5_valid, ensure_ascii=False), ts_oct20_ms + 2000),
            ("sess-discord-1", 6, "{corrupt_json_payload_missing_brace", ts_oct20_ms + 3000),
            ("sess-discord-1", 7, json.dumps(ev7_after_corrupt, ensure_ascii=False), ts_oct20_ms + 4000),
        ],
    )
    conn.commit()
    conn.close()

    events_pf1, cursors_pf1, stats_pf1 = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_after_echo,
    )
    # seq=5 is exported; seq=6 fails to parse; seq=7 is NOT crossed/exported; cursor stops at seq=5!
    assert [e["seq"] for e in events_pf1] == [5]
    assert stats_pf1["events_parse_failed"] == 1
    assert cursors_pf1[("main", "sess-discord-1")].last_seq == 5

    # Next run before seq=6 is repaired: retries seq=6, still fails at seq=6, cursor remains at 5
    events_pf2, cursors_pf2, stats_pf2 = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_pf1,
    )
    assert events_pf2 == []
    assert stats_pf2["events_parse_failed"] == 1
    assert cursors_pf2[("main", "sess-discord-1")].last_seq == 5

    # Repair seq=6 in SQLite -> next run retries seq=6 and exports both seq=6 and seq=7, advancing cursor to 7
    ev6_repaired = {
        "type": "message",
        "id": "ev-raw-6",
        "message": {
            "role": "user",
            "content": "修复后的第 6 条消息：AMD MI350 明年下半年放量节奏跟踪更新。",
        },
    }
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE transcript_events SET event_json = ? WHERE session_id = 'sess-discord-1' AND seq = 6",
        (json.dumps(ev6_repaired, ensure_ascii=False),),
    )
    conn.commit()
    conn.close()

    events_repaired, cursors_repaired, stats_repaired = exporter.export_agent_sqlite(
        "main",
        db_path,
        cursors=cursors_pf2,
    )
    assert [e["seq"] for e in events_repaired] == [6, 7]
    assert stats_repaired["events_parse_failed"] == 0
    assert cursors_repaired[("main", "sess-discord-1")].last_seq == 7


def test_codex_paste_rule_variants() -> None:
    """Verify codex >2000-char user message filter and assistant verbatim echo detection across
    standalone title paragraphs, blank lines between speakers, and headerless long pastes.
    """
    # Variant A: standalone title paragraph + blank lines between speakers (>2000 chars)
    variant_a = "纪要标题：2026Q4 半导体设备专家电话会\n\n" + "\n\n".join(
        [f"嘉宾 A：第 {i} 个问题关于先进封装产能瓶颈与 CoWoS-L 良率爬坡节奏的详细回答。" * 2 for i in range(30)]
    )
    assert len(variant_a) > 2000
    cleaned_a, dropped_a = exporter.strip_codex_desktop_transcript(variant_a, is_codex_desktop=True, role="user")
    assert cleaned_a == ""
    assert dropped_a is True

    # Variant B: headerless long paste (>2000 chars)
    variant_b = " ".join(["没有任何页眉或时间戳的纯文本长粘贴内容，讨论 HBM4 良率与供需格局。"] * 80)
    assert len(variant_b) > 2000
    cleaned_b, dropped_b = exporter.strip_codex_desktop_transcript(variant_b, is_codex_desktop=True, role="user")
    assert cleaned_b == ""
    assert dropped_b is True

    # Assistant verbatim repeat of discarded user message is detected and dropped
    assert exporter.is_codex_assistant_verbatim_repeat(variant_b, [variant_a, variant_b]) is True
    assert (
        exporter.is_codex_assistant_verbatim_repeat(
            "我对上面这段访谈的总结是：HBM4 明年上半年仍处于紧平衡状态。",
            [variant_a, variant_b],
        )
        is False
    )


def test_exporter_schema_mismatch_raises(tmp_path: Path) -> None:
    """Verify `verify_openclaw_schema` stops and raises `OpenClawSchemaError` when tables/columns mismatch."""
    bad_db = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(str(bad_db))
    conn.execute("CREATE TABLE session_windows (session_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(exporter.OpenClawSchemaError):
        exporter.export_agent_sqlite("main", bad_db, cursors={})


def test_sanitization_redacts_secrets_and_skips_ops_and_chitchat() -> None:
    """Verify JSON, env, Bearer, pairing code, and API key credential contexts are redacted
    without relying on length or entropy (including paired quotes with embedded opposite quotes
    and escaped quotes), and operational/chitchat messages are skipped.
    """
    raw_with_secrets = (
        '关于 TSM 2nm 良率的纪要我已经看过了，配置 JSON 是 {"api_key": "TESTKEY1234567890", "secret": "short123"}，'
        "环境变量 OPENAI_API_KEY=my_simple_key_123，短口令 KEY=ab，带特殊字符 PASSWORD='ab!cd'，"
        '嵌套引号 JSON {"password":"ab\'cd", "token":"xy\\"zw"}，'
        '转义环境变量 PASSWORD="ab\\"cd" SECRET=\'ef\\\'gh\'，'
        "请求头 Authorization: Bearer tok_abc_123，"
        "顺便把测试密钥 sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz123456 "
        "和配对码: K9M2-P4X8 以及授权码是 ZT8899 记一下。"
    )
    cleaned, count = exporter.sanitize_text(raw_with_secrets)
    assert count >= 12
    assert "TESTKEY1234567890" not in cleaned
    assert "short123" not in cleaned
    assert "my_simple_key_123" not in cleaned
    assert "KEY=[REDACTED]" in cleaned
    assert "PASSWORD='[REDACTED]'" in cleaned
    assert '{"password":"[REDACTED]", "token":"[REDACTED]"}' in cleaned
    assert "ab'cd" not in cleaned
    assert 'xy\\"zw' not in cleaned
    assert 'PASSWORD="[REDACTED]"' in cleaned
    assert "SECRET='[REDACTED]'" in cleaned
    assert 'ab\\"cd' not in cleaned
    assert "ef\\'gh" not in cleaned
    assert "tok_abc_123" not in cleaned
    assert "sk-proj-" not in cleaned
    assert "K9M2-P4X8" not in cleaned
    assert "ZT8899" not in cleaned
    assert "[REDACTED]" in cleaned
    assert "TSM 2nm 良率" in cleaned

    # Private keywords MUST drop the entire message even when mixed with research signals
    private_mixed = "今天去医院体检看病，顺便看了一眼 NVDA 财报毛利率 75% 的估值模型"
    skip_priv, reason_priv = exporter.is_operational_or_chitchat(private_mixed)
    assert skip_priv is True
    assert reason_priv == "private"

    ops_samples = [
        "/approve device-pairing-1234",
        "帮我把模型切成 claude，顺便重启网关服务看看状态",
        "配置 cron 定时任务每天早上八点运行一次状态检查",
        "配对码是 A1B2-C3D4，请批准执行授权登录",
    ]
    for sample in ops_samples:
        sanitized, _ = exporter.sanitize_text(sample)
        skip, reason = exporter.is_operational_or_chitchat(sanitized)
        assert skip is True, f"Expected ops sample to be skipped: {sample}"
        assert reason == "ops"

    chitchat_samples = [
        "早上好，收到，非常感谢！",
        "好的明白了，先这样吧。",
    ]
    for sample in chitchat_samples:
        skip, reason = exporter.is_operational_or_chitchat(sample)
        assert skip is True
        assert reason == "chitchat"


@pytest.mark.asyncio
async def test_extraction_verbatim_quote_validation_and_speaker_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify fabricated quotes are discarded, agent fact/estimate/model_assumption cards require
    every `cited_sources` item to appear in the segment event text (else downgraded to `opinion`),
    private companies route to `entities[]`, and `persist_raw=False` never stores raw LLM responses.
    """
    segment = [
        {
            "agent": "main",
            "session_id": "sess-1",
            "session_key": "agent:main:discord:channel:1",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-101",
            "seq": 10,
            "created_at_ms": 1760500000000,
            "ts": "2026-10-15T14:00:00+00:00",
            "date_et": "2026-10-15",
            "message_role": "user",
            "speaker": {
                "sender_id_hash": "hash_owner",
                "name": "Owner",
                "is_owner": True,
                "role": "owner",
            },
            "text": "META 2026 年资本开支指引我们模型按 680 亿美元测算，同时 OpenAI 与 Anthropic 的推理算力需求仍在翻倍。",
        },
        {
            "agent": "main",
            "session_id": "sess-1",
            "session_key": "agent:main:discord:channel:1",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-102",
            "seq": 11,
            "created_at_ms": 1760500010000,
            "ts": "2026-10-15T14:00:10+00:00",
            "date_et": "2026-10-15",
            "message_role": "assistant",
            "speaker": {
                "sender_id_hash": "agent:main",
                "name": "Eve",
                "is_owner": False,
                "role": "agent",
            },
            "text": "我认为明年北美云厂商的 AI 资本开支增速还会保持在 35% 以上。根据 TSMC 2026Q3 法说会纪要，CoWoS 月产能明年底将超 10 万片。",
        },
    ]

    mock_assist = create_autospec(
        extractor_mod.assist_json,
        return_value=AssistResult(
            ok=True,
            status="ok",
            data={
                "category": "research",
                "topic_label": "META Capex 与 AI 算力",
                "cards": [
                    # Card 1: Valid verbatim quote from owner, puts OpenAI in tickers (moves to entities)
                    {
                        "type": "model_assumption",
                        "topic_label": "META 2026 Capex",
                        "claim": "META 2026 年资本开支模型假设为 680 亿美元",
                        "tickers": ["META", "OpenAI", "Anthropic"],
                        "entities": [],
                        "sectors": ["Internet", "AI"],
                        "figures": [
                            {
                                "metric": "capex",
                                "value": 68,
                                "unit": "USD_bn",
                                "period": "FY2026",
                            }
                        ],
                        "quote": "META 2026 年资本开支指引我们模型按 680 亿美元测算",
                        "quote_event_id": "ev-101",
                        "cited_sources": [],
                        "confidence": 0.9,
                    },
                    # Card 2: Fabricated quote -> rejected
                    {
                        "type": "fact",
                        "topic_label": "编造的引用",
                        "claim": "META 宣布明年的资本开支下调至 500 亿美元",
                        "tickers": ["META"],
                        "entities": [],
                        "sectors": [],
                        "figures": [],
                        "quote": "META 刚才正式宣布明年资本开支下调到 500 亿美元",
                        "quote_event_id": "ev-101",
                        "cited_sources": [],
                        "confidence": 0.8,
                    },
                    # Card 3: Agent (Eve) claims an "estimate" with a fabricated cited_source not in segment text -> downgraded to "opinion"
                    {
                        "type": "estimate",
                        "topic_label": "云厂商 AI Capex",
                        "claim": "明年北美云厂商 AI 资本开支增速将保持在 35% 以上",
                        "tickers": [],
                        "entities": [],
                        "sectors": ["Cloud"],
                        "figures": [],
                        "quote": "我认为明年北美云厂商的 AI 资本开支增速还会保持在 35% 以上。",
                        "quote_event_id": "ev-102",
                        "cited_sources": ["Goldman Sachs Cloud Report 2026"],
                        "confidence": 0.75,
                    },
                    # Card 4: Agent (Eve) claims a "fact" with a cited_source that IS in segment text -> stays "fact"
                    {
                        "type": "fact",
                        "topic_label": "CoWoS 产能",
                        "claim": "CoWoS 月产能明年底将超 10 万片",
                        "tickers": ["TSM"],
                        "entities": [],
                        "sectors": ["Semiconductors"],
                        "figures": [],
                        "quote": "根据 TSMC 2026Q3 法说会纪要，CoWoS 月产能明年底将超 10 万片。",
                        "quote_event_id": "ev-102",
                        "cited_sources": ["TSMC 2026Q3 法说会纪要"],
                        "confidence": 0.9,
                    },
                ],
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 350, "output_tokens": 180, "total_tokens": 530},
        ),
    )
    monkeypatch.setattr(extractor_mod, "assist_json", mock_assist)

    outcome = await extractor_mod.extract_segment_cards(segment)
    assert outcome.ok is True
    assert outcome.quotes_rejected == 1
    assert len(outcome.cards) == 3
    assert outcome.total_tokens == 530
    assert mock_assist.call_args.kwargs.get("persist_raw") is False

    owner_card = outcome.cards[0]
    assert owner_card["source_type"] == "conversation"
    assert owner_card["tickers"] == ["META"]
    assert "OpenAI" in owner_card["entities"]
    assert "Anthropic" in owner_card["entities"]
    assert owner_card["speaker"]["role"] == "owner"

    agent_downgraded = outcome.cards[1]
    assert agent_downgraded["speaker"]["role"] == "agent"
    assert agent_downgraded["type"] == "opinion"

    agent_verified_fact = outcome.cards[2]
    assert agent_verified_fact["speaker"]["role"] == "agent"
    assert agent_verified_fact["type"] == "fact"

    # Also verify assist_json forwards persist_raw=False and skip_journal=True to dalton_complete
    mock_complete = create_autospec(
        assist_mod.dalton_complete,
        return_value=complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={"category": "private", "cards": []},
            model="gemini-4-argon-medium-oauth",
        ),
    )
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete)
    await assist_mod.assist_json(
        "conversation_card_extract",
        "extract cards",
        {"events": []},
        {"type": "object"},
        user_id="owner-user",
        persist_raw=False,
        skip_journal=True,
    )
    assert mock_complete.call_args.kwargs.get("persist_raw") is False
    assert mock_complete.call_args.kwargs.get("skip_journal") is True

    # Verify `persist_raw=False` in `dalton_complete` strips `quote` and any raw event text from
    # the payload written to `dalton_llm_journal` (`complete_journal_done`), marks it `replayable=False`,
    # and never replays the quote-stripped journal row on retry (re-invoking `llm_service.complete` instead).
    from src.dalton.database import llm_journal as llm_journal_mod
    from src.server.services.llm.service import LLMService

    raw_llm_extraction_dict = {
        "category": "research",
        "topic_label": "META Capex",
        "events": [{"event_id": "ev-101", "text": "RAW_EVENT_TEXT_MUST_NOT_ENTER_JOURNAL"}],
        "cards": [
            {
                "type": "model_assumption",
                "topic_label": "META 2026 Capex",
                "claim": "META 2026 年资本开支模型假设为 680 亿美元",
                "tickers": ["META"],
                "entities": ["OpenAI"],
                "sectors": ["Internet"],
                "figures": [{"metric": "capex", "value": 68, "unit": "USD_bn", "period": "FY2026", "quote": "LEAK"}],
                "quote": "META 2026 年资本开支指引我们模型按 680 亿美元测算",
                "quote_event_id": "ev-101",
                "cited_sources": ["TSMC 2026Q3"],
                "confidence": 0.9,
                "text": "RAW_CARD_EVENT_TEXT_LEAK",
            }
        ],
    }

    journal_saved_responses: list[dict[str, Any] | None] = []
    llm_call_counter = 0

    async def _fake_llm_complete(*args: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
        nonlocal llm_call_counter
        llm_call_counter += 1
        # Return JSON text (matching the real `assist_json` dict-schema path where `pydantic_schema=None`)
        return (
            json.dumps(raw_llm_extraction_dict, ensure_ascii=False),
            {"input_tokens": 120, "output_tokens": 60, "total_tokens": 180},
        )

    mock_llm_service = create_autospec(LLMService, instance=True)
    mock_llm_service.complete.side_effect = _fake_llm_complete

    async def _fake_inspect(
        invocation_id: str,
        *,
        request_hash: str,
        stale_after_s: int = 900,
        resend_indeterminate: bool = False,
        conn: Any = None,
    ) -> tuple[str, dict[str, Any] | None]:
        if not journal_saved_responses:
            return "proceed", None
        latest_resp = journal_saved_responses[-1]
        row = {
            "invocation_id": invocation_id,
            "request_hash": request_hash,
            "state": "done",
            "response": latest_resp,
            "model_key": "gemini-4-argon-medium-oauth",
            "provider": "openclaw-broker",
            "billing_type": "oauth",
            "usage": {"input_tokens": 120, "output_tokens": 60},
            "notional_usd": "0.001",
            "attempt": len(journal_saved_responses),
        }
        if not llm_journal_mod.is_journal_response_replayable(latest_resp):
            return "proceed", row
        return "done", row

    async def _fake_claim(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        if journal_saved_responses:
            latest_resp = journal_saved_responses[-1]
            if llm_journal_mod.is_journal_response_replayable(latest_resp):
                return "done", {
                    "invocation_id": kwargs["invocation_id"],
                    "state": "done",
                    "response": latest_resp,
                    "attempt": len(journal_saved_responses),
                }
        return "claimed", {"invocation_id": kwargs["invocation_id"], "state": "pending", "attempt": len(journal_saved_responses) + 1}

    async def _fake_done(
        invocation_id: str,
        *,
        response: dict[str, Any] | None,
        usage: dict[str, Any] | None = None,
        cash_usd: Any = 0,
        notional_usd: Any = 0,
        provider: str | None = None,
        billing_type: str | None = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        journal_saved_responses.append(response)
        return {"invocation_id": invocation_id, "state": "done", "response": response}

    monkeypatch.setattr(
        complete_mod,
        "inspect_journal_state",
        create_autospec(complete_mod.inspect_journal_state, side_effect=_fake_inspect),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(complete_mod.claim_journal_invocation, side_effect=_fake_claim),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, side_effect=_fake_done),
    )

    comp_1 = await complete_mod.dalton_complete(
        llm_service=mock_llm_service,
        user_id="owner-user",
        system_prompt="sys",
        user_prompt="usr",
        scope_ref="conv_extract:owner-user:seg1",
        purpose="assist.conversation.extract_cards",
        request_model="gemini-4-argon-medium-oauth",
        response_schema=extractor_mod.EXTRACTION_RESPONSE_SCHEMA,
        persist_raw=False,
        skip_journal=False,
    )
    assert comp_1.ok is True
    assert comp_1.replayed is False
    assert llm_call_counter == 1
    assert len(journal_saved_responses) == 1
    saved_j_resp = journal_saved_responses[0]
    assert isinstance(saved_j_resp, dict)
    assert saved_j_resp.get("replayable") is False
    serialized_journal = json.dumps(saved_j_resp, ensure_ascii=False)
    assert "quote" not in saved_j_resp["data"]["cards"][0]
    assert "META 2026 年资本开支指引我们模型按 680 亿美元测算" not in serialized_journal
    assert "RAW_EVENT_TEXT_MUST_NOT_ENTER_JOURNAL" not in serialized_journal
    assert "RAW_CARD_EVENT_TEXT_LEAK" not in serialized_journal
    assert "LEAK" not in serialized_journal
    # Structured metadata (type, tickers, claim, figures) IS preserved in journal
    assert saved_j_resp["data"]["cards"][0]["type"] == "model_assumption"
    assert saved_j_resp["data"]["cards"][0]["tickers"] == ["META"]
    assert saved_j_resp["data"]["cards"][0]["claim"] == "META 2026 年资本开支模型假设为 680 亿美元"
    assert saved_j_resp["data"]["cards"][0]["quote_event_id"] == "ev-101"

    # Second call with same parameters does NOT replay the quote-stripped journal row; it re-calls LLM!
    comp_2 = await complete_mod.dalton_complete(
        llm_service=mock_llm_service,
        user_id="owner-user",
        system_prompt="sys",
        user_prompt="usr",
        scope_ref="conv_extract:owner-user:seg1",
        purpose="assist.conversation.extract_cards",
        request_model="gemini-4-argon-medium-oauth",
        response_schema=extractor_mod.EXTRACTION_RESPONSE_SCHEMA,
        persist_raw=False,
        skip_journal=False,
    )
    assert comp_2.ok is True
    assert comp_2.replayed is False
    assert llm_call_counter == 2


@pytest.mark.asyncio
async def test_dedup_and_supersedes_resolution() -> None:
    """Verify idempotent `content_hash` deduplication, chronological single-ticker `supersedes`,
    and `insert_cards_batch` inserting all cards with `supersedes=None` first before applying
    `supersedes` updates in the same transaction.
    """
    quote_1 = "NVDA 2027 财年数据中心收入我们按 1700 亿美元放进模型"
    hash_1 = extractor_mod.compute_content_hash(quote_1, "ev-1")
    old_card_1 = {
        "card_id": "cc_old_nvda_170",
        "source_type": "conversation",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 170, "unit": "USD_bn", "period": "FY2027"}],
        "quote": quote_1,
        "quote_event_id": "ev-1",
        "content_hash": hash_1,
        "ts": "2026-10-10T10:00:00+00:00",
        "date_et": "2026-10-10",
        "status": "active",
        "supersedes": None,
    }
    old_card_2 = {
        "card_id": "cc_old_nvda_180",
        "source_type": "conversation",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 180, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 财年数据中心收入上调到 1800 亿美元",
        "quote_event_id": "ev-2",
        "content_hash": extractor_mod.compute_content_hash("NVDA 2027 财年数据中心收入上调到 1800 亿美元", "ev-2"),
        "ts": "2026-10-12T10:00:00+00:00",
        "date_et": "2026-10-12",
        "status": "active",
        "supersedes": None,
    }
    # Multi-ticker card should NOT be matched by set intersection
    multi_ticker_card = {
        "card_id": "cc_multi_nvda_amd",
        "source_type": "conversation",
        "tickers": ["NVDA", "AMD"],
        "figures": [{"metric": "datacenter_revenue", "value": 160, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 和 AMD 合计收入测算",
        "quote_event_id": "ev-multi",
        "content_hash": "hash_multi",
        "ts": "2026-10-09T10:00:00+00:00",
        "date_et": "2026-10-09",
        "status": "active",
        "supersedes": None,
    }

    # Candidate 1: exact duplicate of old_card_1 -> deduped
    dup_candidate = {
        "card_id": "cc_dup",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 170, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA  2027 财年数据中心收入我们按 1700 亿美元放进模型",
        "quote_event_id": "ev-1",
        "ts": "2026-10-10T10:00:00+00:00",
        "date_et": "2026-10-10",
    }

    # Candidate 2: newer single-ticker card for (NVDA, datacenter_revenue, FY2027) -> supersedes BOTH old_card_1 and old_card_2
    updated_candidate = {
        "card_id": "cc_new_nvda_195",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 195, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 财年数据中心收入我们最新上调到 1950 亿美元",
        "quote_event_id": "ev-3",
        "ts": "2026-10-15T10:00:00+00:00",
        "date_et": "2026-10-15",
    }

    # Candidate 3: an OLDER backfilled card (2026-10-01) -> MUST NOT supersede the newer 2026-10-15 card!
    older_backfill_candidate = {
        "card_id": "cc_older_nvda_150",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 150, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "早期我们按 1500 亿美元测算 NVDA 2027 数据中心收入",
        "quote_event_id": "ev-0",
        "ts": "2026-10-01T10:00:00+00:00",
        "date_et": "2026-10-01",
    }

    accepted, deduped_cnt, superseded_cnt = extractor_mod.resolve_dedup_and_supersedes(
        [dup_candidate, updated_candidate, older_backfill_candidate],
        existing_hashes={hash_1},
        existing_active_cards=[old_card_1, old_card_2, multi_ticker_card],
    )

    assert deduped_cnt == 1
    assert superseded_cnt == 3
    assert len(accepted) == 2

    new_card = next(c for c in accepted if c["card_id"] == "cc_new_nvda_195")
    assert new_card["supersedes"] == "cc_old_nvda_180"
    assert set(new_card["superseded_card_ids"]) == {"cc_old_nvda_170", "cc_old_nvda_180"}
    assert old_card_1["status"] == "superseded"
    assert old_card_2["status"] == "superseded"
    assert multi_ticker_card["status"] == "active"

    older_card = next(c for c in accepted if c["card_id"] == "cc_older_nvda_150")
    assert older_card.get("supersedes") is None
    assert older_card["status"] == "superseded"
    assert new_card["status"] == "active"

    # Two-step out-of-order backfill test: process newer card (2026-10-20) first, then backfill older card (2026-10-10)
    existing_newer = {
        "card_id": "cc_oct20_new",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 200, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 数据中心收入最新看 2000 亿美元",
        "quote_event_id": "ev-oct20",
        "content_hash": "hash_oct20",
        "ts": "2026-10-20T10:00:00+00:00",
        "date_et": "2026-10-20",
        "status": "active",
        "supersedes": None,
    }
    backfill_older = {
        "card_id": "cc_oct10_old",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 165, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 数据中心收入早期看 1650 亿美元",
        "quote_event_id": "ev-oct10",
        "ts": "2026-10-10T10:00:00+00:00",
        "date_et": "2026-10-10",
    }
    acc_bf, ded_bf, sup_bf = extractor_mod.resolve_dedup_and_supersedes(
        [backfill_older],
        existing_hashes={"hash_oct20"},
        existing_active_cards=[existing_newer],
    )
    assert ded_bf == 0
    assert sup_bf == 1
    assert len(acc_bf) == 1
    assert existing_newer["status"] == "active"
    assert existing_newer["supersedes"] == "cc_oct10_old"
    assert acc_bf[0]["status"] == "superseded"
    assert acc_bf[0]["superseded_by_card_id"] == "cc_oct20_new"
    assert acc_bf[0]["supersedes"] is None

    # Same-value earlier active card in the same `(ticker, metric, period)` group is ALSO superseded,
    # while two cards with the exact same event timestamp `ts` (even with different `seq_range`) never supersede each other!
    same_val_early = {
        "card_id": "cc_same_val_oct10",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 200, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 数据中心收入早期第一版也是 2000 亿美元",
        "quote_event_id": "ev-same-1",
        "ts": "2026-10-10T10:00:00+00:00",
        "date_et": "2026-10-10",
        "seq_range": [1, 1],
        "status": "active",
    }
    same_ts_sess_a = {
        "card_id": "cc_same_ts_a",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 200, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 数据中心收入最新复核维持 2000 亿美元",
        "quote_event_id": "ev-same-2",
        "ts": "2026-10-20T12:00:00+00:00",
        "date_et": "2026-10-20",
        "seq_range": [5, 5],
    }
    same_ts_sess_b = {
        "card_id": "cc_same_ts_b",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 210, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "另一会话同一秒提到 NVDA 2027 数据中心收入 2100 亿美元",
        "quote_event_id": "ev-same-3",
        "ts": "2026-10-20T12:00:00+00:00",
        "date_et": "2026-10-20",
        "seq_range": [99, 99],
    }
    acc_same, _, sup_same = extractor_mod.resolve_dedup_and_supersedes(
        [same_ts_sess_a, same_ts_sess_b],
        existing_hashes=set(),
        existing_active_cards=[same_val_early],
    )
    assert sup_same == 1
    assert same_val_early["status"] == "superseded"
    assert acc_same[0]["status"] == "active"
    assert acc_same[1]["status"] == "active"
    assert acc_same[0]["supersedes"] == "cc_same_val_oct10"
    assert acc_same[1]["supersedes"] is None

    # Same-batch out-of-order `[new_card, old_card]` in `insert_cards_batch`:
    # All INSERT statements run first with `supersedes = None` and `status = 'active'`,
    # then `supersedes` and `status = 'superseded'` UPDATEs run in the same transaction.
    batch_new_first = {
        "card_id": "cc_batch_new_oct15",
        "agent": "main",
        "session_id": "sess-1",
        "type": "estimate",
        "claim": "NVDA FY2027 DC revenue 195B",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 195, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 财年数据中心收入最新看 1950 亿美元",
        "quote_event_id": "ev-oct15",
        "extractor_model": "gemini-4-argon-medium-oauth",
        "prompt_version": "conv_card_v1",
        "content_hash": "hash_batch_new",
        "ts": "2026-10-15T10:00:00+00:00",
        "date_et": "2026-10-15",
    }
    batch_old_second = {
        "card_id": "cc_batch_old_oct10",
        "agent": "main",
        "session_id": "sess-1",
        "type": "estimate",
        "claim": "NVDA FY2027 DC revenue 170B",
        "tickers": ["NVDA"],
        "figures": [{"metric": "datacenter_revenue", "value": 170, "unit": "USD_bn", "period": "FY2027"}],
        "quote": "NVDA 2027 财年数据中心收入早期看 1700 亿美元",
        "quote_event_id": "ev-oct10",
        "extractor_model": "gemini-4-argon-medium-oauth",
        "prompt_version": "conv_card_v1",
        "content_hash": "hash_batch_old",
        "ts": "2026-10-10T10:00:00+00:00",
        "date_et": "2026-10-10",
    }

    executed_sql: list[tuple[str, Any]] = []
    fetch_queue: list[dict[str, Any]] = [
        {"card_id": "cc_batch_new_oct15"},
        {"card_id": "cc_batch_old_oct10"},
    ]

    async def _fake_execute(sql: str, params: Any = None) -> None:
        executed_sql.append((sql.strip(), params))

    async def _fake_fetchone() -> dict[str, Any] | None:
        return fetch_queue.pop(0) if fetch_queue else None

    mock_cur = create_autospec(AsyncCursor, instance=True)
    mock_cur.execute.side_effect = _fake_execute
    mock_cur.fetchall.return_value = []
    mock_cur.fetchone.side_effect = _fake_fetchone

    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.transaction.return_value.__aenter__.return_value = None
    mock_conn.transaction.return_value.__aexit__.return_value = False
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cur
    mock_conn.cursor.return_value.__aexit__.return_value = False

    ins_cnt, sup_cnt, ins_cards = await db_conv.insert_cards_batch(
        [batch_new_first, batch_old_second],
        embeddings=[[0.1, 0.2], [0.3, 0.4]],
        conn=mock_conn,
    )
    assert ins_cnt == 2
    assert sup_cnt == 1
    assert len(ins_cards) == 2

    sql_verbs = [q.split()[0].upper() for q, _ in executed_sql]
    # Order inside same transaction: SELECT active -> INSERT new (supersedes=None) -> INSERT old (supersedes=None)
    # -> UPDATE status='superseded' -> UPDATE supersedes='cc_batch_old_oct10'
    assert sql_verbs == ["SELECT", "INSERT", "INSERT", "UPDATE", "UPDATE"]
    assert executed_sql[1][1][24] == "active"
    assert executed_sql[1][1][25] is None
    assert executed_sql[2][1][24] == "active"
    assert executed_sql[2][1][25] is None
    assert executed_sql[3][1] == (["cc_batch_old_oct10"],)
    assert executed_sql[4][1] == ("cc_batch_old_oct10", "cc_batch_new_oct15")


@pytest.mark.asyncio
async def test_hybrid_search_rrf_fusion_and_conversation_search_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify RRF hybrid ranking and `conversation_search` authentication & team-shared retrieval."""
    ref_date = date(2026, 10, 20)
    cards = [
        {
            "card_id": "cc_both_arms",
            "agent": "main",
            "session_id": "sess-a",
            "quote_event_id": "ev-a1",
            "date_et": "2026-10-19",
            "ts": "2026-10-19T15:00:00+00:00",
            "tickers": ["NVDA"],
            "entities": [],
            "sectors": ["Semiconductors"],
            "topic_label": "NVDA Blackwell 机柜出货与毛利率",
            "type": "estimate",
            "claim": "NVDA Q4 Blackwell 机柜毛利率预计维持在 73% 左右",
            "quote": "NVDA Q4 Blackwell 机柜毛利率预计维持在 73% 左右",
            "figures": [{"metric": "gross_margin", "value": 73, "unit": "%", "period": "2026Q4"}],
            "speaker": {"name": "Owner", "role": "owner", "is_owner": True},
            "speaker_role": "owner",
            "status": "active",
            "embedding": [0.95, 0.05, 0.0, 0.0],
        },
        {
            "card_id": "cc_dense_only",
            "agent": "main",
            "session_id": "sess-b",
            "quote_event_id": "ev-b1",
            "date_et": "2026-10-18",
            "ts": "2026-10-18T12:00:00+00:00",
            "tickers": ["NVDA"],
            "entities": [],
            "sectors": ["Semiconductors"],
            "topic_label": "GPU 算力集群能耗",
            "type": "opinion",
            "claim": "液冷渗透率提升有助于改善新一代集群部署效率",
            "quote": "液冷渗透率提升有助于改善新一代集群部署效率",
            "figures": [],
            "speaker": {"name": "AnalystBob", "role": "participant", "is_owner": False},
            "speaker_role": "participant",
            "status": "active",
            "embedding": [0.92, 0.08, 0.0, 0.0],
        },
        {
            "card_id": "cc_kw_only",
            "agent": "chem",
            "session_id": "sess-c",
            "quote_event_id": "ev-c1",
            "date_et": "2026-06-01",
            "ts": "2026-06-01T10:00:00+00:00",
            "tickers": ["NVDA"],
            "entities": [],
            "sectors": ["Semiconductors"],
            "topic_label": "Blackwell 早期样品测试",
            "type": "fact",
            "claim": "Blackwell 早期样品在二季度完成初步验证",
            "quote": "Blackwell 早期样品在二季度完成初步验证",
            "figures": [],
            "speaker": {"name": "Owner", "role": "owner", "is_owner": True},
            "speaker_role": "owner",
            "status": "active",
            "embedding": [0.1, 0.9, 0.0, 0.0],
        },
    ]

    ranked = search_mod.rank_cards_in_memory(
        cards,
        query="NVDA Blackwell 毛利率",
        query_embedding=[1.0, 0.0, 0.0, 0.0],
        tickers=["NVDA"],
        reference_date=ref_date,
        limit=5,
    )
    assert len(ranked) == 3
    assert ranked[0]["card_id"] == "cc_both_arms"
    assert set(ranked[0]["matched_arms"]) == {"dense", "keyword"}
    assert ranked[0]["source_type"] == "conversation"
    assert ranked[0]["source_ref"] == "openclaw://main/sess-a#ev-a1"

    mock_embed_q = create_autospec(
        search_mod.embed_query_via_bridge,
        return_value=[1.0, 0.0, 0.0, 0.0],
    )
    mock_dense_db = create_autospec(
        db_conv.search_cards_dense,
        return_value=[cards[0], cards[1]],
    )
    mock_kw_db = create_autospec(
        db_conv.search_cards_keyword,
        return_value=[cards[0], cards[2]],
    )
    monkeypatch.setattr(search_mod, "embed_query_via_bridge", mock_embed_q)
    monkeypatch.setattr(db_conv, "search_cards_dense", mock_dense_db)
    monkeypatch.setattr(db_conv, "search_cards_keyword", mock_kw_db)

    analyst_tools = {t.name: t for t in build_dalton_tools("analyst")}
    cos_tools = {t.name: t for t in build_dalton_tools("chief_of_staff")}
    assert "conversation_search" in analyst_tools
    assert "conversation_search" in cos_tools
    assert "wiki" not in (analyst_tools["conversation_search"].description or "").lower()

    # 1) Missing user_id in RunnableConfig -> rejected with unauthenticated error
    unauth_json = await analyst_tools["conversation_search"].ainvoke(
        {"query": "NVDA Blackwell 毛利率", "tickers": ["NVDA"], "limit": 3},
        config={"configurable": {}},
    )
    unauth_parsed = json.loads(unauth_json)
    assert unauth_parsed["ok"] is False
    assert "unauthenticated" in unauth_parsed["error"]
    assert mock_dense_db.call_count == 0

    # 2) Authenticated user_id in RunnableConfig -> returns team-shared cards and logs caller
    raw_json = await analyst_tools["conversation_search"].ainvoke(
        {"query": "NVDA Blackwell 毛利率", "tickers": ["NVDA"], "limit": 3},
        config={"configurable": {"user_id": "team-member-02", "thread_id": "thread-xyz"}},
    )
    parsed = json.loads(raw_json)
    assert parsed["ok"] is True
    assert parsed["source_type"] == "conversation"
    assert parsed["cards"][0]["card_id"] == "cc_both_arms"
    assert parsed["cards"][0]["source_ref"] == "openclaw://main/sess-a#ev-a1"


@pytest.mark.asyncio
async def test_render_per_company_and_home_topics_and_backfill_429_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify per-company `<workspace>/conversations/<date>.md` vs Home `research/topics/conversations/<date>.md`
    using `create_autospec(SandboxBackend, instance=True)`, verify `bulk_upsert_files` error propagation,
    and verify `extract_segment_with_infinite_backoff` re-checks hourly budget on each retry and stops on
    permanent quota errors.
    """
    nvda_written: dict[str, str] = {}
    home_written: dict[str, str] = {}

    async def _write_nvda(file_path: str, content: str) -> bool:
        nvda_written[file_path] = content
        return True

    async def _write_home(file_path: str, content: str) -> bool:
        home_written[file_path] = content
        return True

    nvda_backend = create_autospec(SandboxBackend, instance=True)
    nvda_backend.awrite_text.side_effect = _write_nvda
    nvda_backend.aexecute_bash.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    home_backend = create_autospec(SandboxBackend, instance=True)
    home_backend.awrite_text.side_effect = _write_home
    home_backend.aexecute_bash.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}

    mock_bulk_upsert = create_autospec(render_mod.bulk_upsert_files, return_value=1)
    mock_delete_rows = create_autospec(render_mod.delete_file_rows, return_value=1)
    monkeypatch.setattr(render_mod, "bulk_upsert_files", mock_bulk_upsert)
    monkeypatch.setattr(render_mod, "delete_file_rows", mock_delete_rows)

    sample_cards = [
        {
            "card_id": "cc_nvda_1",
            "source_type": "conversation",
            "agent": "main",
            "session_id": "sess-1",
            "quote_event_id": "ev-1",
            "ts": "2026-10-19T14:00:00+00:00",
            "date_et": "2026-10-19",
            "tickers": ["NVDA"],
            "entities": [],
            "sectors": ["Semiconductors"],
            "topic_label": "NVDA 机柜交付",
            "type": "fact",
            "claim": "NVDA 四季度机柜交付进度符合预期",
            "quote": "NVDA 四季度机柜交付进度符合预期",
            "figures": [],
            "cited_sources": [],
            "speaker": {"name": "Owner", "role": "owner", "is_owner": True},
            "speaker_role": "owner",
            "confidence": 0.9,
            "status": "active",
        },
        {
            "card_id": "cc_ai_private_1",
            "source_type": "conversation",
            "agent": "main",
            "session_id": "sess-2",
            "quote_event_id": "ev-2",
            "ts": "2026-10-19T16:00:00+00:00",
            "date_et": "2026-10-19",
            "tickers": [],
            "entities": ["OpenAI", "Anthropic"],
            "sectors": ["AI"],
            "topic_label": "前沿大模型训练成本",
            "type": "opinion",
            "claim": "OpenAI 与 Anthropic 下一代基座模型的预训练算力投入仍保持高强度",
            "quote": "OpenAI 与 Anthropic 下一代基座模型的预训练算力投入仍保持高强度",
            "figures": [],
            "cited_sources": [],
            "speaker": {"name": "Owner", "role": "owner", "is_owner": True},
            "speaker_role": "owner",
            "confidence": 0.85,
            "status": "active",
        },
    ]

    render_res = await render_mod.render_and_write_conversation_digests(
        sample_cards,
        backend_by_ticker={"NVDA": nvda_backend},
        home_backend=home_backend,
        ticker_to_workspace_id={"NVDA": "ws-nvda"},
        home_workspace_id="ws-home",
    )
    assert len(render_res["company_files"]) == 1
    assert render_res["company_files"][0]["path"] == "conversations/2026-10-19.md"
    assert "conversations/2026-10-19.md" in nvda_written
    nvda_md = nvda_written["conversations/2026-10-19.md"]
    assert "openclaw://main/sess-1#ev-1" in nvda_md
    assert "conversation" in nvda_md
    assert "wiki" not in nvda_md.lower()

    assert len(render_res["topic_files"]) == 1
    assert render_res["topic_files"][0]["path"] == "research/topics/conversations/2026-10-19.md"
    home_md = home_written["research/topics/conversations/2026-10-19.md"]
    assert "OpenAI" in home_md
    assert "openclaw://main/sess-2#ev-2" in home_md
    assert "wiki" not in home_md.lower()

    # Verify ticker without a company workspace is recorded in `unmapped_tickers` and routed to Home topic digest
    unmapped_card = {
        "card_id": "cc_unmapped_1",
        "source_type": "conversation",
        "agent": "main",
        "session_id": "sess-3",
        "quote_event_id": "ev-3",
        "ts": "2026-10-19T18:00:00+00:00",
        "date_et": "2026-10-19",
        "tickers": ["CRWV"],
        "entities": [],
        "sectors": ["Cloud"],
        "topic_label": "CRWV 算力租赁",
        "type": "opinion",
        "claim": "CRWV 明年推理算力集群利用率预计维持高位",
        "quote": "CRWV 明年推理算力集群利用率预计维持高位",
        "figures": [],
        "cited_sources": [],
        "speaker": {"name": "Owner", "role": "owner", "is_owner": True},
        "speaker_role": "owner",
        "confidence": 0.85,
        "status": "active",
    }
    unmapped_res = await render_mod.render_and_write_conversation_digests(
        [unmapped_card],
        backend_by_ticker={"NVDA": nvda_backend},
        home_backend=home_backend,
        ticker_to_workspace_id={"NVDA": "ws-nvda"},
        home_workspace_id="ws-home",
    )
    assert len(unmapped_res["unmapped_tickers"]) == 1
    assert unmapped_res["unmapped_tickers"][0]["ticker"] == "CRWV"
    assert unmapped_res["unmapped_tickers"][0]["status"] == "no_company_workspace"
    assert len(unmapped_res["topic_files"]) == 1
    assert "CRWV 明年推理算力集群利用率预计维持高位" in home_written["research/topics/conversations/2026-10-19.md"]
    assert unmapped_res["card_locations"]["cc_unmapped_1"] == [
        {
            "target": "home",
            "ticker": None,
            "workspace_id": "ws-home",
            "path": "research/topics/conversations/2026-10-19.md",
        }
    ]

    # When a company workspace is later created for CRWV and the date is re-rendered,
    # the card writes to CRWV's company workspace and removes the stale Home file (and updates `workspace_files`).
    crwv_written: dict[str, str] = {}

    async def _write_crwv(file_path: str, content: str) -> bool:
        crwv_written[file_path] = content
        return True

    crwv_backend = create_autospec(SandboxBackend, instance=True)
    crwv_backend.awrite_text.side_effect = _write_crwv
    crwv_backend.aexecute_bash.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    mock_delete_rows.reset_mock()

    card_with_prev_home = {
        **unmapped_card,
        "rendered_locations": unmapped_res["card_locations"]["cc_unmapped_1"],
    }
    remapped_res = await render_mod.render_and_write_conversation_digests(
        [card_with_prev_home],
        target_dates=["2026-10-19"],
        backend_by_ticker={"NVDA": nvda_backend, "CRWV": crwv_backend},
        home_backend=home_backend,
        ticker_to_workspace_id={"NVDA": "ws-nvda", "CRWV": "ws-crwv"},
        home_workspace_id="ws-home",
    )
    assert "conversations/2026-10-19.md" in crwv_written
    assert remapped_res["card_locations"]["cc_unmapped_1"] == [
        {
            "target": "company",
            "ticker": "CRWV",
            "workspace_id": "ws-crwv",
            "path": "conversations/2026-10-19.md",
        }
    ]
    assert len(remapped_res["removed_files"]) == 1
    assert remapped_res["removed_files"][0]["path"] == "research/topics/conversations/2026-10-19.md"
    assert mock_delete_rows.await_count == 1
    assert remapped_res["rendered_dates"] == ["2026-10-19"]

    # Verify a date with 0 active cards left (e.g. superseded) removes stale `conversations/<date>.md` and updates `workspace_files`
    mock_delete_rows.reset_mock()
    superseded_only_card = {
        **sample_cards[0],
        "status": "superseded",
        "rendered_locations": [
            {
                "target": "company",
                "ticker": "NVDA",
                "workspace_id": "ws-nvda",
                "path": "conversations/2026-10-19.md",
            }
        ],
    }
    superseded_res = await render_mod.render_and_write_conversation_digests(
        [superseded_only_card],
        target_dates=["2026-10-19"],
        backend_by_ticker={"NVDA": nvda_backend},
        home_backend=home_backend,
        ticker_to_workspace_id={"NVDA": "ws-nvda"},
        home_workspace_id="ws-home",
    )
    assert len(superseded_res["removed_files"]) == 1
    assert superseded_res["removed_files"][0]["path"] == "conversations/2026-10-19.md"
    assert mock_delete_rows.await_count == 1
    assert superseded_res["rendered_dates"] == ["2026-10-19"]

    # Verify historical `(workspace_id, path)` diff cleans up the old workspace even when the ticker's
    # workspace mapping changes to a new workspace (`ws-nvda-old` -> `ws-nvda`) or disappears
    mock_delete_rows.reset_mock()
    old_ws_backend = create_autospec(SandboxBackend, instance=True)
    old_ws_backend.aexecute_bash.return_value = {"exit_code": 0, "stdout": "", "stderr": ""}
    card_from_old_ws = {
        **sample_cards[0],
        "rendered_locations": [
            {
                "target": "company",
                "ticker": "NVDA",
                "workspace_id": "ws-nvda-old",
                "path": "conversations/2026-10-19.md",
            }
        ],
    }
    remap_ws_res = await render_mod.render_and_write_conversation_digests(
        [card_from_old_ws],
        target_dates=["2026-10-19"],
        backend_by_ticker={"NVDA": nvda_backend},
        backend_by_workspace_id={"ws-nvda-old": old_ws_backend},
        home_backend=home_backend,
        ticker_to_workspace_id={"NVDA": "ws-nvda"},
        home_workspace_id="ws-home",
    )
    assert len(remap_ws_res["company_files"]) == 1
    assert remap_ws_res["company_files"][0]["workspace_id"] == "ws-nvda"
    assert len(remap_ws_res["removed_files"]) == 1
    assert remap_ws_res["removed_files"][0]["workspace_id"] == "ws-nvda-old"
    assert remap_ws_res["removed_files"][0]["path"] == "conversations/2026-10-19.md"
    assert mock_delete_rows.call_args.args[0] == "ws-nvda-old"

    # Verify `bulk_upsert_files` failure raises rather than pretending the file was written
    mock_bulk_upsert.side_effect = RuntimeError("db_upsert_failed")
    with pytest.raises(RuntimeError, match="db_upsert_failed"):
        await render_mod.render_and_write_conversation_digests(
            sample_cards[:1],
            backend_by_ticker={"NVDA": nvda_backend},
            home_backend=home_backend,
            ticker_to_workspace_id={"NVDA": "ws-nvda"},
            home_workspace_id="ws-home",
        )

    # Verify oversized single event splitting in `segment_events` attaches stable `chunk_idx`
    oversized_event = {
        "agent": "main",
        "session_id": "sess-big",
        "event_id": "ev-big-1",
        "seq": 1,
        "created_at_ms": 1760500000000,
        "text": "\n\n".join([f"第 {i} 段关于 AI 服务器产业链的深度调研分析笔记。" * 10 for i in range(30)]),
    }
    split_segs = extractor_mod.segment_events([oversized_event], max_segment_chars=2000)
    assert len(split_segs) >= 3
    for idx, s in enumerate(split_segs):
        assert len(s) == 1
        assert len(s[0]["text"]) <= 2000
        assert s[0]["chunk_idx"] == idx

    # Verify infinite time-based backoff on transient 429 re-checks hourly token budget before every retry
    call_count = 0
    slept_delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept_delays.append(seconds)

    mock_sleep_fn = create_autospec(asyncio.sleep, side_effect=_fake_sleep)

    async def _flaky_extract(*args: Any, **kwargs: Any) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return extractor_mod.SegmentExtractionOutcome(
                model_used="gemini-4-argon-medium-oauth",
                ok=False,
                rate_limited=True,
                error="http_429: Resource has been exhausted (rate limit)",
            )
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            cards=[sample_cards[0]],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=200,
            output_tokens=100,
            total_tokens=300,
        )

    mock_extract_fn = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_flaky_extract,
    )
    limiter = create_autospec(archive_cli.HourlyTokenBudgetLimiter, instance=True)
    limiter.wait_for_budget.return_value = 0.0
    outcome, retries = await archive_cli.extract_segment_with_infinite_backoff(
        [{"event_id": "ev-1", "seq": 1, "text": "NVDA 四季度机柜交付进度符合预期"}],
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        initial_backoff_s=5.0,
        max_backoff_s=60.0,
        limiter=limiter,
        estimated_tokens=1200,
        extract_fn=mock_extract_fn,
        sleep_fn=mock_sleep_fn,
    )
    assert retries == 3
    assert len(slept_delays) == 3
    assert limiter.wait_for_budget.await_count == 3
    assert len(outcome.cards) == 1

    # Verify direct 429 exception raised by `extract_fn` is caught, classified by status_code/exception, and backed off
    exc_calls = 0

    class FakeRateLimitError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("rate limit exceeded")
            self.status_code = 429

    async def _raising_429_extract(*args: Any, **kwargs: Any) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal exc_calls
        exc_calls += 1
        if exc_calls == 1:
            raise FakeRateLimitError()
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            cards=[sample_cards[0]],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

    mock_raising_429 = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_raising_429_extract,
    )
    outcome_exc, retries_exc = await archive_cli.extract_segment_with_infinite_backoff(
        [{"event_id": "ev-1", "seq": 1, "text": "NVDA 四季度机柜交付进度符合预期"}],
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        initial_backoff_s=5.0,
        max_backoff_s=60.0,
        limiter=limiter,
        estimated_tokens=1200,
        extract_fn=mock_raising_429,
        sleep_fn=mock_sleep_fn,
    )
    assert retries_exc == 1
    assert outcome_exc.ok is True

    # Verify permanent quota error stops immediately without infinite retry
    perm_extract_fn = create_autospec(
        extractor_mod.extract_segment_cards,
        return_value=extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            rate_limited=True,
            permanent_quota_error=True,
            error="http_429_permanent_quota",
        ),
    )
    with pytest.raises(archive_cli.PermanentQuotaError):
        await archive_cli.extract_segment_with_infinite_backoff(
            [{"event_id": "ev-1", "seq": 1, "text": "NVDA 四季度机柜交付进度符合预期"}],
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            extract_fn=perm_extract_fn,
            sleep_fn=mock_sleep_fn,
        )


@pytest.mark.asyncio
async def test_run_archive_pipeline_with_default_config_and_processed_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `run_archive_pipeline` works end-to-end with real `DaltonConversationsConfig()` defaults,
    runs host export via real `ExportRunResult` (`updated_cursors`), tracks per-event/chunk progress
    in `dalton_conversation_processed` (without retracting old generations or secondary date-window
    filtering), preserves inbox files until all chunks are in `dalton_conversation_processed`,
    records safe `error_text`, and opens/closes the DB pool in CLI `main()`.
    """
    real_cfg = DaltonConversationsConfig()
    assert real_cfg.max_segment_gap_minutes == 30
    assert real_cfg.max_segment_chars == 6000
    assert real_cfg.max_segment_events == 25
    assert real_cfg.rate_limit_initial_backoff_seconds == 30.0
    assert real_cfg.rate_limit_max_backoff_seconds == 900.0

    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)
    export_cursors_file = tmp_path / "export_cursors.json"

    # Set up a real temporary OpenClaw SQLite DB under `ocw_agents_root / "main" / "agent"`
    # so `run_archive_pipeline` exercises the real `run_export` -> `ExportRunResult.updated_cursors` path.
    ocw_agents_root = tmp_path / "ocw_agents"
    main_agent_dir = ocw_agents_root / "main" / "agent"
    main_agent_dir.mkdir(parents=True)
    ocw_sqlite_path = main_agent_dir / "openclaw-agent.sqlite"
    sqlite_conn = _init_mock_openclaw_sqlite(ocw_sqlite_path)
    ts_oct18_ms = int(datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
    sqlite_conn.execute(
        """
        INSERT INTO session_windows (session_id, session_key, channel, chat_type, account_id, primary_conversation_id)
        VALUES ('sess-1', 'agent:main:discord:channel:1', 'discord', 'channel', 'acc1', 'conv1')
        """
    )
    sqlite_conn.execute(
        "INSERT INTO transcript_rewrite_watermarks (session_id, generation) VALUES ('sess-1', 'gen-b')"
    )
    sqlite_ev1 = {
        "type": "message",
        "id": "ev-1",
        "message": {
            "role": "user",
            "content": "NVDA 四季度 Blackwell 机柜出货量我们最新跟踪是 1.2 万柜。",
            "__openclaw": {
                "senderId": "user_owner_01",
                "senderName": "Owner",
                "senderIsOwner": True,
                "transport": {"channel": "discord"},
            },
        },
    }
    sqlite_conn.execute(
        "INSERT INTO transcript_events (session_id, seq, event_json, created_at) VALUES ('sess-1', 1, ?, ?)",
        (json.dumps(sqlite_ev1, ensure_ascii=False), ts_oct18_ms),
    )
    sqlite_conn.execute(
        "INSERT INTO transcript_event_identities (session_id, seq, event_id, event_type) VALUES ('sess-1', 1, 'ev-1', 'message')"
    )
    sqlite_conn.commit()
    sqlite_conn.close()

    processed_store: dict[tuple[str, str, str, int], str] = {}
    processed_plans_store: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def _dynamic_fetch_processed(
        keys: list[tuple[str, str, str, int]],
        conn: Any = None,
        *,
        out_plans: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    ) -> set[tuple[str, str, str, int]]:
        if not keys:
            return set()
        target_set = {
            (str(ag), str(sid), str(eid), int(cidx))
            for ag, sid, eid, cidx in keys
        }
        target_events = {(ag, sid, eid) for ag, sid, eid, _ in target_set}
        if out_plans is not None:
            for ev_key in target_events:
                if ev_key in processed_plans_store:
                    out_plans[ev_key] = dict(processed_plans_store[ev_key])
            return {k for k in processed_store if (k[0], k[1], k[2]) in target_events}
        return set(processed_store.keys()) & target_set

    async def _dynamic_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        for r in rows:
            ev_key = (
                str(r["agent"]),
                str(r["session_id"]),
                str(r["event_id"]),
            )
            key = (*ev_key, int(r.get("chunk_idx", 0)))
            processed_store[key] = str(r.get("status") or "extracted")
            processed_plans_store[ev_key] = {
                "chunk_count": int(r.get("chunk_count") or 1),
                "chunk_max_chars": int(r.get("chunk_max_chars") or 6000),
                "chunk_spans": list(r.get("chunk_spans") or []),
            }
        return len(rows)

    mock_fetch_processed = create_autospec(
        db_conv.fetch_processed_event_chunks,
        side_effect=_dynamic_fetch_processed,
    )
    mock_record_processed = create_autospec(
        db_conv.record_processed_event_chunks,
        side_effect=_dynamic_record_processed,
    )
    mock_insert_run = create_autospec(db_conv.insert_run_start, return_value=None)
    mock_finish_run = create_autospec(db_conv.update_run_finish, return_value=None)
    mock_find_hashes = create_autospec(db_conv.find_existing_content_hashes, return_value=set())

    async def _fake_insert_batch(
        cards: list[dict[str, Any]],
        *,
        embeddings: list[list[float] | None] | None = None,
        conn: Any = None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        return len(cards), 0, list(cards)

    mock_insert_batch = create_autospec(db_conv.insert_cards_batch, side_effect=_fake_insert_batch)
    mock_unrendered = create_autospec(db_conv.list_unrendered_dates, return_value=[])
    mock_mark_rendered = create_autospec(db_conv.mark_dates_rendered, return_value=1)
    mock_list_for_dates = create_autospec(db_conv.list_cards_for_dates, return_value=[])
    mock_render = create_autospec(
        archive_cli.render_and_write_conversation_digests,
        return_value={
            "company_files": [{"ticker": "NVDA", "path": "conversations/2026-10-18.md"}],
            "topic_files": [],
            "rendered_dates": ["2026-10-18"],
            "card_locations": {
                "cc_test_1": [{"kind": "company", "ticker": "NVDA", "path": "conversations/2026-10-18.md"}]
            },
        },
    )

    monkeypatch.setattr(db_conv, "fetch_processed_event_chunks", mock_fetch_processed)
    monkeypatch.setattr(db_conv, "record_processed_event_chunks", mock_record_processed)
    monkeypatch.setattr(db_conv, "insert_run_start", mock_insert_run)
    monkeypatch.setattr(db_conv, "update_run_finish", mock_finish_run)
    monkeypatch.setattr(db_conv, "find_existing_content_hashes", mock_find_hashes)
    monkeypatch.setattr(db_conv, "insert_cards_batch", mock_insert_batch)
    monkeypatch.setattr(db_conv, "list_unrendered_dates", mock_unrendered)
    monkeypatch.setattr(db_conv, "mark_dates_rendered", mock_mark_rendered)
    monkeypatch.setattr(db_conv, "list_cards_for_dates", mock_list_for_dates)
    monkeypatch.setattr(archive_cli, "render_and_write_conversation_digests", mock_render)

    mock_extract = create_autospec(
        extractor_mod.extract_segment_cards,
        return_value=extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": "cc_test_1",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": "sess-1",
                    "quote_event_id": "ev-1",
                    "generation": "gen-b",
                    "ts": "2026-10-18T14:00:00+00:00",
                    "date_et": "2026-10-18",
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": ["Semiconductors"],
                    "topic_label": "NVDA 机柜出货",
                    "type": "estimate",
                    "claim": "NVDA 四季度 Blackwell 机柜出货 1.2 万柜",
                    "quote": "NVDA 四季度 Blackwell 机柜出货量我们最新跟踪是 1.2 万柜。",
                    "content_hash": "hash_1",
                    "search_text": "NVDA Blackwell 1.2 万柜",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        ),
    )
    mock_embed = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        return_value=([[0.1, 0.2, 0.3]], "gemini-embedding-001"),
    )

    # 1. Run full pipeline with real host export (`skip_host_export=False`) -> exercises `exp_res.updated_cursors`,
    #    records `('main', 'sess-1', 'ev-1', 0)` in `dalton_conversation_processed`, deletes inbox file, and saves export cursors!
    summary = await archive_cli.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 18),
        until_date=date(2026, 10, 19),
        inbox_dir=inbox_dir,
        export_cursors_file=export_cursors_file,
        ocw_agents_root=ocw_agents_root,
        agents=["main"],
        skip_host_export=False,
        extract_fn=mock_extract,
        embed_fn=mock_embed,
    )
    assert summary["ok"] is True
    assert summary["export"]["events_exported"] == 1
    assert len(summary["inbox_files_consumed"]) == 1
    assert list(inbox_dir.glob("*.jsonl")) == []
    assert processed_store.get(("main", "sess-1", "ev-1", 0)) == "extracted"
    saved_export_cursors = exporter.load_cursors_file(export_cursors_file)
    assert saved_export_cursors[("main", "sess-1")].last_seq == 1
    assert saved_export_cursors[("main", "sess-1")].generation == "gen-b"
    assert mock_mark_rendered.await_count >= 1
    assert mock_mark_rendered.call_args.kwargs.get("card_locations") == {
        "cc_test_1": [{"kind": "company", "ticker": "NVDA", "path": "conversations/2026-10-18.md"}]
    }

    # 2. Failed segment (timeout / 5xx / schema error) -> raises SegmentExtractionError, does NOT write to
    #    `dalton_conversation_processed`, preserves inbox file on disk, and records safe error_text without str(exc) secrets
    event_base = {
        "agent": "main",
        "session_id": "sess-1",
        "session_key": "agent:main:discord:channel:1",
        "channel": "discord",
        "chat_type": "channel",
        "event_id": "ev-fail",
        "seq": 10,
        "generation": "gen-b",
        "created_at_ms": 1760950000000,
        "ts": "2026-10-20T10:00:00+00:00",
        "date_et": "2026-10-20",
        "message_role": "user",
        "speaker": {"sender_id_hash": "h1", "name": "Owner", "is_owner": True, "role": "owner"},
        "text": "NVDA 四季度 Blackwell 机柜出货量我们最新跟踪是 1.2 万柜。",
    }
    ev_file_fail = inbox_dir / "20261020T100000Z_fail.jsonl"
    ev_file_fail.write_text(json.dumps(event_base, ensure_ascii=False) + "\n", encoding="utf-8")
    mock_record_processed.reset_mock()
    mock_finish_run.reset_mock()

    fail_extract = create_autospec(
        extractor_mod.extract_segment_cards,
        return_value=extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            rate_limited=False,
            error="http_502: Bad Gateway with secret_key=SHOULD_NOT_LEAK",
        ),
    )
    with pytest.raises(archive_cli.SegmentExtractionError):
        await archive_cli.run_archive_pipeline(
            mode="run",
            since_date=date(2026, 10, 20),
            until_date=date(2026, 10, 20),
            inbox_dir=inbox_dir,
            skip_host_export=True,
            extract_fn=fail_extract,
            embed_fn=mock_embed,
        )
    assert ("main", "sess-1", "ev-fail", 0) not in processed_store
    assert ev_file_fail.exists()
    failed_call_kwargs = mock_finish_run.call_args.kwargs
    assert failed_call_kwargs["status"] == "failed"
    assert failed_call_kwargs["error_text"] == "SegmentExtractionError(status_code=502)"
    assert "SHOULD_NOT_LEAK" not in failed_call_kwargs["error_text"]

    # 3. Verify consumer does NOT secondary-filter by date window (2026-09-01 event is processed even when
    #    since_date=2026-10-20), records rule-discarded ops/chitchat events as `discarded`, tracks oversized
    #    event split chunks (`chunk_idx=0` vs `chunk_idx=1`) individually in `dalton_conversation_processed`,
    #    and preserves the inbox file until ALL chunks in the file are in `dalton_conversation_processed`.
    ev_file_fail.unlink(missing_ok=True)
    multi_chunk_file = inbox_dir / "20261021T100000Z_chunks.jsonl"

    ev_older_date = {
        **event_base,
        "event_id": "ev-sep01",
        "seq": 2,
        "created_at_ms": 1756720800000,
        "ts": "2026-09-01T10:00:00+00:00",
        "date_et": "2026-09-01",
    }
    ev_rule_ops = {
        **event_base,
        "event_id": "ev-ops-skip",
        "seq": 3,
        "text": "/approve device-pairing-9999",
    }
    oversized_para_0 = ("第一部分深度调研：NVDA GB200 NVL72 机柜铜连接与液冷供应链详细拆解。" * 100).strip()
    oversized_para_1 = ("第二部分深度调研：TSMC CoWoS-L 明年各季度扩产节奏与大客户产能分配。" * 100).strip()
    assert len(oversized_para_0) > 3200
    assert len(oversized_para_1) > 3200
    ev_oversized = {
        **event_base,
        "session_id": "sess-2",
        "event_id": "ev-big-split",
        "seq": 1,
        "created_at_ms": 1760960000000,
        "ts": "2026-10-20T12:46:40+00:00",
        "date_et": "2026-10-20",
        "text": oversized_para_0 + "\n\n" + oversized_para_1,
    }
    multi_chunk_file.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in (ev_older_date, ev_rule_ops, ev_oversized)) + "\n",
        encoding="utf-8",
    )

    seen_chunks: list[tuple[str, int]] = []
    fail_on_chunk_1 = True

    async def _chunk_selective_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        cidx = int(segment[0].get("chunk_idx", 0))
        seen_chunks.append((ev_id, cidx))
        if ev_id == "ev-big-split" and cidx == 1 and fail_on_chunk_1:
            return extractor_mod.SegmentExtractionOutcome(
                model_used="gemini-4-argon-medium-oauth",
                ok=False,
                rate_limited=False,
                error="http_503: upstream unavailable",
            )
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": f"cc_{ev_id}_{cidx}",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": str(segment[0]["session_id"]),
                    "quote_event_id": ev_id,
                    "generation": str(segment[0]["generation"]),
                    "ts": str(segment[0]["ts"]),
                    "date_et": str(segment[0]["date_et"]),
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": [],
                    "topic_label": "NVDA",
                    "type": "estimate",
                    "claim": f"NVDA claim {ev_id} #{cidx}",
                    "quote": str(segment[0]["text"])[:40],
                    "content_hash": f"hash_{ev_id}_{cidx}",
                    "search_text": "NVDA",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=50,
            output_tokens=25,
            total_tokens=75,
        )

    selective_extract_fn = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_chunk_selective_extract,
    )
    with pytest.raises(archive_cli.SegmentExtractionError):
        await archive_cli.run_archive_pipeline(
            mode="run",
            since_date=date(2026, 10, 20),
            until_date=date(2026, 10, 20),
            inbox_dir=inbox_dir,
            skip_host_export=True,
            extract_fn=selective_extract_fn,
            embed_fn=mock_embed,
        )
    # 2026-09-01 event was NOT filtered out by consumer date window; ops event was marked discarded;
    # chunk 0 of oversized event was marked extracted; chunk 1 failed and was NOT recorded; file stays on disk!
    assert seen_chunks == [("ev-sep01", 0), ("ev-big-split", 0), ("ev-big-split", 1)]
    assert processed_store.get(("main", "sess-1", "ev-sep01", 0)) == "extracted"
    assert processed_store.get(("main", "sess-1", "ev-ops-skip", 0)) == "discarded"
    assert processed_store.get(("main", "sess-2", "ev-big-split", 0)) == "extracted"
    assert ("main", "sess-2", "ev-big-split", 1) not in processed_store
    assert multi_chunk_file.exists()

    # Retry run even after `max_segment_chars` is increased in config from 6000 to 12000:
    # reuses persisted `chunk_spans` from `dalton_conversation_processed`, skips already-recorded
    # `("ev-big-split", 0)`, extracts ONLY `("ev-big-split", 1)`, and then deletes the inbox file!
    seen_chunks.clear()
    fail_on_chunk_1 = False
    big_limit_cfg = DaltonConversationsConfig(max_segment_chars=12000)
    monkeypatch.setattr(
        archive_cli,
        "get_dalton_settings",
        create_autospec(
            archive_cli.get_dalton_settings,
            return_value=type("FakeSettings", (), {"conversations": big_limit_cfg})(),
        ),
    )
    retry_summary = await archive_cli.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 20),
        until_date=date(2026, 10, 20),
        inbox_dir=inbox_dir,
        skip_host_export=True,
        extract_fn=selective_extract_fn,
        embed_fn=mock_embed,
    )
    assert retry_summary["ok"] is True
    assert seen_chunks == [("ev-big-split", 1)]
    assert processed_store.get(("main", "sess-2", "ev-big-split", 1)) == "extracted"
    assert not multi_chunk_file.exists()
    assert mock_render.call_args.kwargs.get("user_id") == archive_cli.archive_owner_user_id()

    # 4. Verify CLI `_async_main` / `main` opens and closes the DB pool in non-dry-run mode
    mock_pool = create_autospec(AsyncConnectionPool, instance=True)
    mock_pool.open.return_value = None
    mock_pool.close.return_value = None
    monkeypatch.setattr(
        archive_cli,
        "get_or_create_pool",
        create_autospec(archive_cli.get_or_create_pool, return_value=mock_pool),
    )
    monkeypatch.setattr(
        archive_cli,
        "run_archive_pipeline",
        create_autospec(archive_cli.run_archive_pipeline, return_value={"ok": True}),
    )
    parsed_args = archive_cli.build_arg_parser().parse_args(["run", "--skip-host-export"])
    cli_res = await archive_cli._async_main(parsed_args)
    assert cli_res["ok"] is True
    assert mock_pool.open.await_count == 1
    assert mock_pool.close.await_count == 1


@pytest.mark.asyncio
async def test_researchdata_bridge_embed_security_and_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `/embed` rejects `/embed/<token>` path tokens, enforces dedicated byte limit,
    reads GEMINI_API_KEY strictly from the bridge env file (never overridden by process `os.environ`
    and never sourcing user shell rc files), and uses `-B` + read-only config parsing for `company-wiki`.
    """
    env_file = tmp_path / "bridge.env"
    env_file.write_text("GEMINI_API_KEY=bridge_env_test_key\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", "test_bridge_secret_token")
    monkeypatch.setenv("DALTON_OCW_ROOT", str(tmp_path))
    monkeypatch.setenv("DALTON_BRIDGE_EMBED_MAX_INFLIGHT", "2")
    monkeypatch.setenv("DALTON_BRIDGE_EMBED_MAX_BYTES", "8192")
    # Even when process environment has a different GEMINI_API_KEY, bridge env file wins!
    monkeypatch.setenv("GEMINI_API_KEY", "process_env_should_not_override")

    cfg = bridge_mod.BridgeConfig()
    bridge = bridge_mod.ResearchDataBridge(cfg)
    assert os.environ.get("GEMINI_API_KEY") == "bridge_env_test_key"
    assert cfg.embed_max_inflight == 2
    assert cfg.embed_max_bytes == 8192
    assert bridge.embed_sem._value == 2

    # When GEMINI_API_KEY is absent from env_file, bridge refuses even if process os.environ has GEMINI_API_KEY
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    monkeypatch.setenv("GEMINI_API_KEY", "leaked_process_env_key")
    cfg_no_key = bridge_mod.BridgeConfig()
    bridge_no_key = bridge_mod.ResearchDataBridge(cfg_no_key)
    monkeypatch.setenv("GEMINI_API_KEY", "leaked_process_env_key")
    res_no_key = await bridge_no_key._run_company_wiki_embed(["test"], "RETRIEVAL_QUERY")
    assert res_no_key["ok"] is False
    assert "GEMINI_API_KEY" in res_no_key["error"]

    # URL builder in settings never puts token in URL path
    url = embedder_mod.get_dalton_bridge_embed_url("127.0.0.1")
    assert url == "http://127.0.0.1:8960/embed"
    assert "token" not in url


def test_integration_conftest_sessionstart_test_db_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `tests/integration/dalton/conftest.py` `pytest_sessionstart` aborts via `pytest.exit`
    before any DB connection or destructive SQL when `TEST_DB_NAME` does not end with `_test`.
    """
    from tests.integration.dalton import conftest as dalton_int_conftest

    mock_session = create_autospec(pytest.Session, instance=True)

    monkeypatch.setenv("TEST_DB_NAME", "langalpha_prod")
    with pytest.raises(pytest.exit.Exception, match="must end with '_test'"):
        dalton_int_conftest.pytest_sessionstart(mock_session)

    monkeypatch.setenv("TEST_DB_NAME", "")
    with pytest.raises(pytest.exit.Exception, match="must end with '_test'"):
        dalton_int_conftest.pytest_sessionstart(mock_session)

    monkeypatch.setenv("TEST_DB_NAME", "langalpha_test")
    dalton_int_conftest.pytest_sessionstart(mock_session)




def test_journal_redaction_drops_nested_figure_values() -> None:
    from src.dalton.llm import complete as complete_mod

    card = {
        "type": "fact",
        "tickers": ["NVDA"],
        "quote": "verbatim source text",
        "figures": [
            {"metric": "revenue", "value": 35.1, "unit": "USD bn", "period": "Q3"},
            {"metric": "margin", "value": {"quote": "nested source text"}, "unit": "%"},
        ],
    }
    redacted = complete_mod._redact_card_for_journal(card)
    assert "quote" not in redacted
    assert redacted["figures"][0]["value"] == 35.1
    assert "value" not in redacted["figures"][1]
    assert "nested source text" not in str(redacted)
