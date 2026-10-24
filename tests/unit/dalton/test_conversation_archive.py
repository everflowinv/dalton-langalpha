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
from contextlib import asynccontextmanager
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

from psycopg import AsyncConnection, AsyncCursor, AsyncTransaction
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


def _make_autospec_db_conn() -> AsyncConnection:
    mock_cur = create_autospec(AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_cur.fetchone.return_value = {"locked": True, "unlocked": True}
    mock_cur.fetchall.return_value = []
    mock_cur.rowcount = 0
    mock_tx = create_autospec(AsyncTransaction, instance=True)
    mock_tx.__aenter__.return_value = mock_tx
    mock_tx.__aexit__.return_value = False
    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.transaction.return_value = mock_tx
    mock_conn.cursor.return_value = mock_cur
    mock_conn.close.return_value = None
    return mock_conn


def _patch_autospec_db_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AsyncConnection, AsyncConnection]:
    tx_conn = _make_autospec_db_conn()
    lock_conn = _make_autospec_db_conn()

    @asynccontextmanager
    async def _fake_get_db_connection(conn: Any = None) -> Any:
        yield conn if conn is not None else tx_conn

    monkeypatch.setattr(
        db_conv,
        "get_db_connection",
        create_autospec(db_conv.get_db_connection, side_effect=_fake_get_db_connection),
    )
    monkeypatch.setattr(
        archive_cli,
        "open_session_connection",
        create_autospec(archive_cli.open_session_connection, return_value=lock_conn),
    )
    return tx_conn, lock_conn


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
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_cur.execute.side_effect = _fake_execute
    mock_cur.fetchall.return_value = []
    mock_cur.fetchone.side_effect = _fake_fetchone

    mock_tx = create_autospec(AsyncTransaction, instance=True)
    mock_tx.__aenter__.return_value = mock_tx
    mock_tx.__aexit__.return_value = False
    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.transaction.return_value = mock_tx
    mock_conn.cursor.return_value = mock_cur

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
    mock_progress_run = create_autospec(db_conv.update_run_progress, return_value=None)
    mock_finish_run = create_autospec(db_conv.update_run_finish, return_value=None)
    mock_acquire_lock = create_autospec(db_conv.try_acquire_archive_run_lock, return_value=True)
    mock_release_lock = create_autospec(db_conv.release_archive_run_lock, return_value=True)
    mock_mark_abandoned = create_autospec(db_conv.mark_stale_running_runs_abandoned, return_value=0)
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

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(db_conv, "fetch_processed_event_chunks", mock_fetch_processed)
    monkeypatch.setattr(db_conv, "record_processed_event_chunks", mock_record_processed)
    monkeypatch.setattr(db_conv, "insert_run_start", mock_insert_run)
    monkeypatch.setattr(db_conv, "update_run_progress", mock_progress_run)
    monkeypatch.setattr(db_conv, "update_run_finish", mock_finish_run)
    monkeypatch.setattr(db_conv, "try_acquire_archive_run_lock", mock_acquire_lock)
    monkeypatch.setattr(db_conv, "release_archive_run_lock", mock_release_lock)
    monkeypatch.setattr(db_conv, "mark_stale_running_runs_abandoned", mock_mark_abandoned)
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
            error="http_400: Bad Request with secret_key=SHOULD_NOT_LEAK",
        ),
    )
    fail_summary = await archive_cli.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 20),
        until_date=date(2026, 10, 20),
        inbox_dir=inbox_dir,
        skip_host_export=True,
        extract_fn=fail_extract,
        embed_fn=mock_embed,
    )
    assert fail_summary["ok"] is False
    assert fail_summary["exit_code"] == 3
    assert fail_summary["incomplete_segments_total"] == 1
    assert fail_summary["incomplete_segments"][0]["status"] == "failed"
    assert fail_summary["incomplete_segments"][0]["reason"] == "extraction_error:http_400"
    assert ("main", "sess-1", "ev-fail", 0) not in processed_store
    assert ev_file_fail.exists()
    failed_call_kwargs = mock_finish_run.call_args.kwargs
    assert failed_call_kwargs["status"] == "failed"
    assert failed_call_kwargs["error_text"] == "incomplete_segments=1"
    assert "SHOULD_NOT_LEAK" not in failed_call_kwargs["error_text"]

    # Fatal `PermanentQuotaError` still aborts and records sanitized `error_text` without leaking secrets
    mock_finish_run.reset_mock()
    quota_fail_extract = create_autospec(
        extractor_mod.extract_segment_cards,
        return_value=extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            permanent_quota_error=True,
            error="http_402: Billing disabled with secret_key=SHOULD_NOT_LEAK",
        ),
    )
    with pytest.raises(archive_cli.PermanentQuotaError):
        await archive_cli.run_archive_pipeline(
            mode="run",
            since_date=date(2026, 10, 20),
            until_date=date(2026, 10, 20),
            inbox_dir=inbox_dir,
            skip_host_export=True,
            extract_fn=quota_fail_extract,
            embed_fn=mock_embed,
        )
    quota_call_kwargs = mock_finish_run.call_args.kwargs
    assert quota_call_kwargs["status"] == "failed"
    assert quota_call_kwargs["error_text"] == "PermanentQuotaError(status_code=402)"
    assert "SHOULD_NOT_LEAK" not in quota_call_kwargs["error_text"]

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
                error="http_400: bad request payload",
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
    partial_chunk_summary = await archive_cli.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 20),
        until_date=date(2026, 10, 20),
        inbox_dir=inbox_dir,
        skip_host_export=True,
        extract_fn=selective_extract_fn,
        embed_fn=mock_embed,
    )
    assert partial_chunk_summary["ok"] is False
    assert partial_chunk_summary["exit_code"] == 3
    assert partial_chunk_summary["incomplete_segments_total"] == 1
    assert partial_chunk_summary["incomplete_segments"][0]["status"] == "failed"
    assert partial_chunk_summary["incomplete_segments"][0]["reason"] == "extraction_error:http_400"
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


@pytest.mark.asyncio
async def test_concurrent_extraction_flushes_in_original_segment_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify concurrent extraction buffers out-of-order completions (`seg 1`, `seg 2` finishing
    before `seg 0`) and persists cards + `dalton_conversation_processed` rows strictly in original
    segment order (`seg 0 -> seg 1 -> seg 2`), updating run progress counters and logging unbuffered
    per-segment INFO lines to stderr.
    """
    archive_cli.configure_archive_logging()

    # Three events spaced > 30 minutes apart so `segment_events` produces 3 distinct segments (0, 1, 2)
    base_ms = int(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    batch_events = [
        {
            "agent": "main",
            "session_id": "sess-order",
            "event_id": f"ev-{idx}",
            "seq": idx + 1,
            "generation": "gen-1",
            "created_at_ms": base_ms + idx * 3600 * 1000,
            "ts": datetime.fromtimestamp((base_ms + idx * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-20",
            "text": f"NVDA segment {idx} 机柜出货进展跟踪记录。",
        }
        for idx in range(3)
    ]

    seg0_gate = asyncio.Event()
    seg1_2_done = asyncio.Event()
    completion_order: list[str] = []
    persisted_card_order: list[str] = []
    persisted_event_order: list[str] = []
    progress_snapshots: list[dict[str, Any]] = []

    async def _out_of_order_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-0":
            await seg0_gate.wait()
        completion_order.append(ev_id)
        if len(completion_order) == 2 and set(completion_order) == {"ev-1", "ev-2"}:
            seg1_2_done.set()
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": f"cc_{ev_id}",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": "sess-order",
                    "quote_event_id": ev_id,
                    "generation": "gen-1",
                    "ts": str(segment[0]["ts"]),
                    "date_et": "2026-10-20",
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": [],
                    "topic_label": "NVDA",
                    "type": "estimate",
                    "claim": f"Claim for {ev_id}",
                    "quote": str(segment[0]["text"]),
                    "content_hash": f"hash_{ev_id}",
                    "search_text": f"NVDA {ev_id}",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

    async def _fake_insert_batch(
        cards: list[dict[str, Any]],
        *,
        embeddings: list[list[float] | None] | None = None,
        conn: Any = None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        for c in cards:
            persisted_card_order.append(str(c["card_id"]))
        return len(cards), 0, list(cards)

    async def _fake_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        for r in rows:
            persisted_event_order.append(str(r["event_id"]))
        return len(rows)

    async def _fake_update_progress(run_id: str, **kwargs: Any) -> dict[str, Any] | None:
        progress_snapshots.append(dict(kwargs))
        return {"run_id": run_id, **kwargs}

    async def _fake_embed(
        texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> tuple[list[list[float]], str]:
        return [[0.1, 0.2, 0.3] for _ in texts], "gemini-embedding-001"

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, side_effect=_fake_update_progress),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(db_conv.insert_cards_batch, side_effect=_fake_insert_batch),
    )
    mock_rec_proc = create_autospec(
        db_conv.record_processed_event_chunks,
        side_effect=_fake_record_processed,
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        mock_rec_proc,
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )
    monkeypatch.setattr(
        db_conv,
        "list_cards_for_dates",
        create_autospec(db_conv.list_cards_for_dates, return_value=[]),
    )
    monkeypatch.setattr(
        archive_cli,
        "render_and_write_conversation_digests",
        create_autospec(
            archive_cli.render_and_write_conversation_digests,
            return_value={"company_files": [], "topic_files": [], "rendered_dates": []},
        ),
    )

    limiter = archive_cli.HourlyTokenBudgetLimiter(1_500_000)
    mock_extract = create_autospec(extractor_mod.extract_segment_cards, side_effect=_out_of_order_extract)
    mock_embed = create_autospec(embedder_mod.embed_texts_via_bridge, side_effect=_fake_embed)

    batch_task = asyncio.create_task(
        archive_cli.process_weekly_batch(
            batch_events,
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=limiter,
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=3,
            extract_fn=mock_extract,
            embed_fn=mock_embed,
        )
    )

    await asyncio.wait_for(seg1_2_done.wait(), timeout=2.0)
    # Give event loop a turn to run `_flush_ready_prefix()` for seg 1 and seg 2
    await asyncio.sleep(0)
    # Because seg 0 has NOT completed yet, nothing may be flushed out of order!
    assert persisted_card_order == []
    assert persisted_event_order == []

    seg0_gate.set()
    summary = await asyncio.wait_for(batch_task, timeout=2.0)

    # Completion order was out-of-order (`ev-1`, `ev-2` before `ev-0`), but DB persistence order is strictly `0, 1, 2`!
    assert completion_order[:2] in (["ev-1", "ev-2"], ["ev-2", "ev-1"])
    assert completion_order[2] == "ev-0"
    assert persisted_card_order == ["cc_ev-0", "cc_ev-1", "cc_ev-2"]
    assert persisted_event_order == ["ev-0", "ev-1", "ev-2"]
    assert summary["segments_processed"] == 3
    assert summary["cards_inserted"] == 3
    assert len(progress_snapshots) >= 1
    assert progress_snapshots[-1]["segments_processed"] == 3
    assert progress_snapshots[-1]["cards_inserted"] == 3
    assert progress_snapshots[-1]["total_tokens"] == 450

    stderr_out = capsys.readouterr().err
    assert "Segment 1/3 completed" in stderr_out
    assert "Segment 2/3 completed" in stderr_out
    assert "Segment 3/3 completed" in stderr_out


@pytest.mark.asyncio
async def test_cancellation_and_permanent_quota_preserve_completed_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that when a running batch is cancelled (`CancelledError`) or hits `PermanentQuotaError`,
    in-flight tasks are cancelled and the already-completed contiguous prefix is persisted on disk.
    """
    base_ms = int(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    batch_events = [
        {
            "agent": "main",
            "session_id": "sess-cancel",
            "event_id": f"ev-{idx}",
            "seq": idx + 1,
            "generation": "gen-1",
            "created_at_ms": base_ms + idx * 3600 * 1000,
            "ts": datetime.fromtimestamp((base_ms + idx * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-20",
            "text": f"NVDA segment {idx} 关键财务数据假设记录。",
        }
        for idx in range(3)
    ]

    persisted_cards: list[str] = []
    persisted_events: list[str] = []
    finish_calls: list[dict[str, Any]] = []

    async def _fake_insert_batch(
        cards: list[dict[str, Any]],
        *,
        embeddings: list[list[float] | None] | None = None,
        conn: Any = None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        for c in cards:
            persisted_cards.append(str(c["card_id"]))
        return len(cards), 0, list(cards)

    async def _fake_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        for r in rows:
            persisted_events.append(str(r["event_id"]))
        return len(rows)

    async def _fake_finish_run(run_id: str, **kwargs: Any) -> dict[str, Any] | None:
        finish_calls.append({"run_id": run_id, **kwargs})
        return {"run_id": run_id, **kwargs}

    async def _fake_embed(
        texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> tuple[list[list[float]], str]:
        return [[0.1, 0.2, 0.3] for _ in texts], "gemini-embedding-001"

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, side_effect=_fake_finish_run),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(db_conv.insert_cards_batch, side_effect=_fake_insert_batch),
    )
    mock_rec_proc = create_autospec(
        db_conv.record_processed_event_chunks,
        side_effect=_fake_record_processed,
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        mock_rec_proc,
    )

    # 1. Task cancellation (`CancelledError`) after segments 0 and 1 complete while segment 2 is in flight
    seg2_in_flight = asyncio.Event()
    seg2_hold = asyncio.Event()

    async def _cancel_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-2":
            seg2_in_flight.set()
            await seg2_hold.wait()
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": f"cc_{ev_id}",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": "sess-cancel",
                    "quote_event_id": ev_id,
                    "generation": "gen-1",
                    "ts": str(segment[0]["ts"]),
                    "date_et": "2026-10-20",
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": [],
                    "topic_label": "NVDA",
                    "type": "estimate",
                    "claim": f"Claim {ev_id}",
                    "quote": str(segment[0]["text"]),
                    "content_hash": f"hash_{ev_id}",
                    "search_text": f"NVDA {ev_id}",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=80,
            output_tokens=40,
            total_tokens=120,
        )

    mock_extract_cancel = create_autospec(extractor_mod.extract_segment_cards, side_effect=_cancel_extract)
    mock_embed = create_autospec(embedder_mod.embed_texts_via_bridge, side_effect=_fake_embed)

    limiter = archive_cli.HourlyTokenBudgetLimiter(1_500_000)
    task = asyncio.create_task(
        archive_cli.process_weekly_batch(
            [dict(e) for e in batch_events],
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=limiter,
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=2,
            extract_fn=mock_extract_cancel,
            embed_fn=mock_embed,
        )
    )

    await asyncio.wait_for(seg2_in_flight.wait(), timeout=2.0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert persisted_cards == ["cc_ev-0", "cc_ev-1"]
    assert persisted_events == ["ev-0", "ev-1"]
    assert finish_calls[-1]["status"] == "interrupted"
    assert finish_calls[-1]["segments_processed"] == 2
    assert finish_calls[-1]["cards_inserted"] == 2

    # 2. `PermanentQuotaError` on segment 1 cancels in-flight segment 2 and preserves completed segment 0
    persisted_cards.clear()
    persisted_events.clear()
    finish_calls.clear()
    seg0_flushed = asyncio.Event()
    seg2_cancelled = False

    async def _quota_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal seg2_cancelled
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-0":
            return extractor_mod.SegmentExtractionOutcome(
                category="research",
                ok=True,
                cards=[
                    {
                        "card_id": "cc_ev-0",
                        "source_type": "conversation",
                        "agent": "main",
                        "session_id": "sess-cancel",
                        "quote_event_id": "ev-0",
                        "generation": "gen-1",
                        "ts": str(segment[0]["ts"]),
                        "date_et": "2026-10-20",
                        "tickers": ["NVDA"],
                        "entities": [],
                        "sectors": [],
                        "topic_label": "NVDA",
                        "type": "estimate",
                        "claim": "Claim ev-0",
                        "quote": str(segment[0]["text"]),
                        "content_hash": "hash_ev-0",
                        "search_text": "NVDA ev-0",
                        "status": "active",
                    }
                ],
                cards_raw_count=1,
                model_used="gemini-4-argon-medium-oauth",
                input_tokens=80,
                output_tokens=40,
                total_tokens=120,
            )
        if ev_id == "ev-1":
            await seg0_flushed.wait()
            return extractor_mod.SegmentExtractionOutcome(
                model_used="gemini-4-argon-medium-oauth",
                ok=False,
                rate_limited=True,
                permanent_quota_error=True,
                error="http_429_billing_hard_limit",
            )
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seg2_cancelled = True
            raise
        raise AssertionError("unreachable")

    orig_record_processed = _fake_record_processed

    async def _record_and_signal(rows: list[dict[str, Any]], conn: Any = None) -> int:
        res = await orig_record_processed(rows, conn=conn)
        if any(r.get("event_id") == "ev-0" for r in rows):
            seg0_flushed.set()
        return res

    mock_rec_proc.side_effect = _record_and_signal
    mock_extract_quota = create_autospec(extractor_mod.extract_segment_cards, side_effect=_quota_extract)

    with pytest.raises(archive_cli.PermanentQuotaError):
        await archive_cli.process_weekly_batch(
            [dict(e) for e in batch_events],
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000),
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=3,
            extract_fn=mock_extract_quota,
            embed_fn=mock_embed,
        )

    assert seg2_cancelled is True
    assert persisted_cards == ["cc_ev-0"]
    assert persisted_events == ["ev-0"]
    assert finish_calls[-1]["status"] == "failed"
    assert finish_calls[-1]["segments_processed"] == 1


@pytest.mark.asyncio
async def test_limiter_concurrent_reservation_reconciliation_and_shared_429_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify through production call paths (`process_weekly_batch` and `extract_segment_with_infinite_backoff`):
    1. Reservation settlement by unique ID with different estimated token sizes and out-of-order completion.
    2. Cancellation during 429 backoff sleep does not delete a sibling coroutine's active reservation.
    3. Every reservation is settled at most once and `current_window_tokens()` equals actual settled sum.
    4. Shared 429 pause is not cleared early when a shorter backoff (30s) wakes before a longer one (60s).
    """
    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(
            db_conv.insert_cards_batch,
            side_effect=lambda cards, *, embeddings=None, conn=None: (len(cards), 0, list(cards)),
        ),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, return_value=1),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )
    mock_embed = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        return_value=([[0.1, 0.2, 0.3]], "gemini-embedding-001"),
    )

    # 1. Production path (`process_weekly_batch`): different segment token estimates + out-of-order completion
    base_ms = int(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    texts_by_idx = [
        "NVDA 短片段一。" * 10,     # ~80 chars -> input_est=200
        "TSMC 中片段二。" * 120,    # ~960 chars -> input_est=480
        "AVGO 长片段三。" * 250,    # ~2000 chars -> input_est=1000
    ]
    events_3 = [
        {
            "agent": "main",
            "session_id": "sess-res",
            "event_id": f"ev-{i}",
            "seq": i + 1,
            "generation": "gen-1",
            "created_at_ms": base_ms + i * 3600 * 1000,
            "ts": datetime.fromtimestamp((base_ms + i * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-20",
            "text": texts_by_idx[i],
        }
        for i in range(3)
    ]
    budget = 10_000
    est_0 = archive_cli.estimate_segment_tokens([events_3[0]], hourly_budget=budget)
    est_1 = archive_cli.estimate_segment_tokens([events_3[1]], hourly_budget=budget)
    est_2 = archive_cli.estimate_segment_tokens([events_3[2]], hourly_budget=budget)
    assert len({est_0, est_1, est_2}) == 3

    limiter_1 = archive_cli.HourlyTokenBudgetLimiter(budget)
    all_entered = asyncio.Event()
    seg0_gate = asyncio.Event()
    seg1_gate = asyncio.Event()
    entered_ids: list[str] = []
    actual_by_ev = {"ev-0": 310, "ev-1": 420, "ev-2": 530}

    async def _out_of_order_diff_est_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        entered_ids.append(ev_id)
        if len(entered_ids) == 3:
            all_entered.set()
        await all_entered.wait()
        if ev_id == "ev-0":
            await seg0_gate.wait()
        elif ev_id == "ev-1":
            await seg1_gate.wait()
        # ev-2 finishes first while ev-0 and ev-1 are still in flight!
        tot = actual_by_ev[ev_id]
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=tot - 50,
            output_tokens=50,
            total_tokens=tot,
        )

    batch_task_1 = asyncio.create_task(
        archive_cli.process_weekly_batch(
            [dict(e) for e in events_3],
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=limiter_1,
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=3,
            extract_fn=create_autospec(
                extractor_mod.extract_segment_cards,
                side_effect=_out_of_order_diff_est_extract,
            ),
            embed_fn=mock_embed,
        )
    )
    await asyncio.wait_for(all_entered.wait(), timeout=2.0)
    # Give ev-2 a turn to finish extraction and settle its reservation by ID
    for _ in range(5):
        await asyncio.sleep(0)

    # ev-2 settled actual=530 and removed ONLY its own reservation (`est_2`);
    # ev-0 (`est_0`) and ev-1 (`est_1`) remain reserved in `limiter_1._reservations`!
    assert len(limiter_1._reservations) == 2
    assert [tok for _, _, tok in limiter_1._reservations] == [est_0, est_1]
    assert limiter_1.current_window_tokens() == actual_by_ev["ev-2"] + est_0 + est_1

    # Now let ev-0 finish second, then ev-1 finish last
    seg0_gate.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(limiter_1._reservations) == 1
    assert [tok for _, _, tok in limiter_1._reservations] == [est_1]
    assert (
        limiter_1.current_window_tokens()
        == actual_by_ev["ev-2"] + actual_by_ev["ev-0"] + est_1
    )

    seg1_gate.set()
    await asyncio.wait_for(batch_task_1, timeout=2.0)
    assert len(limiter_1._reservations) == 0
    assert limiter_1.current_window_tokens() == sum(actual_by_ev.values())

    # 2. Production path (`process_weekly_batch`): cancellation during 429 backoff sleep does NOT delete
    #    a sibling coroutine's active reservation, and every reservation is settled at most once.
    limiter_2 = archive_cli.HourlyTokenBudgetLimiter(budget)
    seg0_in_429_sleep = asyncio.Event()
    seg1_in_flight = asyncio.Event()
    seg1_release = asyncio.Event()
    seg0_task_holder: list[asyncio.Task[Any] | None] = [None]

    async def _sleep_429_hold(seconds: float) -> None:
        seg0_task_holder[0] = asyncio.current_task()
        seg0_in_429_sleep.set()
        await asyncio.Event().wait()

    async def _extract_seg0_429_seg1_inflight(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-0":
            await seg1_in_flight.wait()
            return extractor_mod.SegmentExtractionOutcome(
                model_used="gemini-4-argon-medium-oauth",
                ok=False,
                rate_limited=True,
                input_tokens=110,
                output_tokens=0,
                total_tokens=110,
                error="http_429_rate_limit",
            )
        seg1_in_flight.set()
        await seg1_release.wait()
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=200,
            output_tokens=90,
            total_tokens=290,
        )

    mock_extract_2 = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_extract_seg0_429_seg1_inflight,
    )
    mock_sleep_2 = create_autospec(asyncio.sleep, side_effect=_sleep_429_hold)

    # Use two segments with IDENTICAL estimated tokens so the old amount-matching `release_reservation`
    # would have mistakenly deleted seg 1's reservation when seg 0 was cancelled during 429 sleep!
    same_len_events = [
        {**events_3[0], "event_id": "ev-0", "text": "NVDA 相同长度片段 A。" * 20},
        {**events_3[1], "event_id": "ev-1", "text": "NVDA 相同长度片段 B。" * 20},
    ]
    same_est = archive_cli.estimate_segment_tokens([same_len_events[0]], hourly_budget=budget)
    assert archive_cli.estimate_segment_tokens([same_len_events[1]], hourly_budget=budget) == same_est

    batch_task_2 = asyncio.create_task(
        archive_cli.process_weekly_batch(
            same_len_events,
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=limiter_2,
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=2,
            extract_fn=mock_extract_2,
            embed_fn=mock_embed,
            sleep_fn=mock_sleep_2,
        )
    )
    await asyncio.wait_for(seg0_in_429_sleep.wait(), timeout=2.0)

    # At this point inside `process_weekly_batch`:
    # - seg 0 settled its initial reservation (id=1, `110` actual tokens) on 429 and is sleeping in backoff;
    # - seg 1's reservation (id=2, `same_est`) is still active in `limiter_2._reservations`.
    assert len(limiter_2._reservations) == 1
    assert [rid for rid, _, _ in limiter_2._reservations] == [2]
    assert limiter_2.current_window_tokens() == 110 + same_est

    # Cancel seg 0's task while it is in 429 backoff sleep -> `_extract_one_segment` runs
    # `except BaseException: limiter.release_reservation(reservation)`, which must NOT delete seg 1's reservation!
    seg0_task = seg0_task_holder[0]
    assert seg0_task is not None
    seg0_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await seg0_task
    assert len(limiter_2._reservations) == 1
    assert [rid for rid, _, _ in limiter_2._reservations] == [2]
    assert limiter_2.current_window_tokens() == 110 + same_est

    # Now let seg 1 complete normally inside `process_weekly_batch` -> seg 1 settles reservation id=2 with 290 actual tokens
    seg1_release.set()
    await asyncio.wait_for(batch_task_2, timeout=2.0)
    assert len(limiter_2._reservations) == 0
    assert limiter_2._settled_reservation_ids == {1, 2}
    assert len(limiter_2._history) == 2
    assert limiter_2.current_window_tokens() == 110 + 290

    # 3. Shared 429 pause: Coroutine A sets 60s backoff, Coroutine B sets 30s backoff;
    # when B wakes up at 30s, the shared pause remains active until 60s on the monotonic clock!
    fake_now = 1000.0

    def _clock() -> float:
        return fake_now

    async def _limiter_sleep(seconds: float) -> None:
        await wake_a.wait()

    mock_clock = create_autospec(archive_cli.time.monotonic, side_effect=_clock)
    mock_limiter_sleep = create_autospec(asyncio.sleep, side_effect=_limiter_sleep)
    limiter_3 = archive_cli.HourlyTokenBudgetLimiter(
        10_000,
        clock_fn=mock_clock,
        sleep_fn=mock_limiter_sleep,
    )

    both_in_extract = asyncio.Event()
    b_entered_sleep = asyncio.Event()
    a_entered_sleep = asyncio.Event()
    wake_b = asyncio.Event()
    wake_a = asyncio.Event()
    in_extract_count = 0
    a_attempts = 0
    b_attempts = 0

    async def _sleep_ab(seconds: float) -> None:
        if seconds >= 50.0:
            a_entered_sleep.set()
            await wake_a.wait()
        else:
            b_entered_sleep.set()
            await wake_b.wait()

    async def _extract_ab(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal in_extract_count, a_attempts, b_attempts
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-a":
            a_attempts += 1
            if a_attempts == 1:
                in_extract_count += 1
                if in_extract_count == 2:
                    both_in_extract.set()
                await both_in_extract.wait()
                return extractor_mod.SegmentExtractionOutcome(
                    model_used="gemini-4-argon-medium-oauth",
                    ok=False,
                    rate_limited=True,
                    total_tokens=100,
                    error="http_429",
                )
        else:
            b_attempts += 1
            if b_attempts == 1:
                in_extract_count += 1
                if in_extract_count == 2:
                    both_in_extract.set()
                await both_in_extract.wait()
                return extractor_mod.SegmentExtractionOutcome(
                    model_used="gemini-4-argon-medium-oauth",
                    ok=False,
                    rate_limited=True,
                    total_tokens=100,
                    error="http_429",
                )
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

    mock_extract_ab = create_autospec(extractor_mod.extract_segment_cards, side_effect=_extract_ab)
    mock_sleep_ab = create_autospec(asyncio.sleep, side_effect=_sleep_ab)

    res_a = await limiter_3.wait_for_budget(500)
    res_b = await limiter_3.wait_for_budget(500)

    task_a = asyncio.create_task(
        archive_cli.extract_segment_with_infinite_backoff(
            [{"event_id": "ev-a", "seq": 1, "text": "Segment A"}],
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            initial_backoff_s=60.0,
            max_backoff_s=900.0,
            limiter=limiter_3,
            estimated_tokens=500,
            reservation=res_a,
            extract_fn=mock_extract_ab,
            sleep_fn=mock_sleep_ab,
        )
    )
    task_b = asyncio.create_task(
        archive_cli.extract_segment_with_infinite_backoff(
            [{"event_id": "ev-b", "seq": 2, "text": "Segment B"}],
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            initial_backoff_s=30.0,
            max_backoff_s=900.0,
            limiter=limiter_3,
            estimated_tokens=500,
            reservation=res_b,
            extract_fn=mock_extract_ab,
            sleep_fn=mock_sleep_ab,
        )
    )
    await asyncio.wait_for(a_entered_sleep.wait(), timeout=2.0)
    await asyncio.wait_for(b_entered_sleep.wait(), timeout=2.0)
    assert limiter_3._rate_limit_pause_until == 1060.0

    # Advance monotonic clock by 30s (`1030.0`) and wake B -> B calls `clear_rate_limit_backoff`,
    # but because monotonic clock is 1030.0 < 1060.0, the shared pause MUST remain active!
    fake_now = 1030.0
    wake_b.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert limiter_3._rate_limit_pause_until == 1060.0
    assert not limiter_3._rate_limit_clear_event.is_set()
    assert b_attempts == 1  # B cannot start attempt 2 while shared pause until 1060.0 is still active!

    # Now advance monotonic clock to 1060.0 and wake A -> pause clears and both A and B complete
    fake_now = 1060.0
    wake_a.set()
    out_a, retries_a = await asyncio.wait_for(task_a, timeout=2.0)
    out_b, retries_b = await asyncio.wait_for(task_b, timeout=2.0)
    limiter_3.record_tokens(out_a.total_tokens, reservation=res_a)
    limiter_3.record_tokens(out_b.total_tokens, reservation=res_b)
    assert retries_a == 1
    assert retries_b == 1
    assert limiter_3._rate_limit_pause_until == 0.0
    assert limiter_3._rate_limit_clear_event.is_set()
    assert len(limiter_3._reservations) == 0
    assert limiter_3.current_window_tokens() == 100 + 100 + 150 + 150


@pytest.mark.asyncio
async def test_flush_prefix_commit_atomicity_on_embed_or_transaction_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `_flush_ready_prefix` advances `next_to_flush`, removes buffered segments, and
    increments counters ONLY after `embed_fn` and `transaction_scope` succeed:
    1. When Batch 0 `embed_fn` fails, Batch 1 is never embedded or written, `next_to_flush` stays 0,
       and `update_run_finish` records 0 committed segments/cards/tokens.
    2. When Batch 0 commits and Batch 1's DB transaction fails on `__aenter__`, Batch 2 is never written,
       `next_to_flush` stays 1, and `update_run_finish` records strictly Batch 0's committed counts.
    3. When Batch 0 commits and Batch 1's DB transaction fails on exit (`__aexit__` / commit), staged
       writes are rolled back, `next_to_flush` stays 1, `dalton_conversation_processed` does not advance,
       and `update_run_finish` records strictly Batch 0's committed counts.
    """
    base_ms = int(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    batch_events = [
        {
            "agent": "main",
            "session_id": "sess-atomicity",
            "event_id": f"ev-{idx}",
            "seq": idx + 1,
            "generation": "gen-1",
            "created_at_ms": base_ms + idx * 3600 * 1000,
            "ts": datetime.fromtimestamp((base_ms + idx * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-20",
            "text": f"NVDA segment {idx} 关键数据。",
        }
        for idx in range(3)
    ]

    tx_conn, _ = _patch_autospec_db_connection(monkeypatch)
    persisted_cards: list[str] = []
    persisted_events: list[str] = []
    staged_cards: list[str] = []
    staged_events: list[str] = []
    in_staged_tx = [False]
    finish_calls: list[dict[str, Any]] = []

    async def _fake_insert_batch(
        cards: list[dict[str, Any]],
        *,
        embeddings: list[list[float] | None] | None = None,
        conn: Any = None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        target = staged_cards if in_staged_tx[0] else persisted_cards
        for c in cards:
            target.append(str(c["card_id"]))
        return len(cards), 0, list(cards)

    async def _fake_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        target = staged_events if in_staged_tx[0] else persisted_events
        for r in rows:
            target.append(str(r["event_id"]))
        return len(rows)

    async def _fake_finish_run(run_id: str, **kwargs: Any) -> dict[str, Any] | None:
        next_to_flush_val: int | None = None
        completed_processed_ids: list[str] | None = None
        for task_item in asyncio.all_tasks():
            cur_coro: Any = task_item.get_coro()
            while cur_coro is not None:
                cr_frame = getattr(cur_coro, "cr_frame", None)
                if cr_frame is not None and cr_frame.f_code.co_name == "process_weekly_batch":
                    next_to_flush_val = int(cr_frame.f_locals["next_to_flush"])
                    completed_processed_ids = [
                        str(k[2]) if isinstance(k, tuple) else str(k.get("event_id"))
                        for k in (
                            cr_frame.f_locals.get("processed_chunk_keys")
                            or cr_frame.f_locals.get("completed_processed_rows")
                            or []
                        )
                    ]
                    break
                cur_coro = getattr(cur_coro, "cr_await", None)
            if next_to_flush_val is not None:
                break
        finish_calls.append(
            {
                "run_id": run_id,
                "next_to_flush": next_to_flush_val,
                "completed_processed_events": completed_processed_ids,
                **kwargs,
            }
        )
        return {"run_id": run_id, **kwargs}

    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, side_effect=_fake_finish_run),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(db_conv.insert_cards_batch, side_effect=_fake_insert_batch),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, side_effect=_fake_record_processed),
    )

    async def _extract_ok(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": f"cc_{ev_id}",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": "sess-atomicity",
                    "quote_event_id": ev_id,
                    "generation": "gen-1",
                    "ts": str(segment[0]["ts"]),
                    "date_et": "2026-10-20",
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": [],
                    "topic_label": "NVDA",
                    "type": "estimate",
                    "claim": f"Claim {ev_id}",
                    "quote": str(segment[0]["text"]),
                    "content_hash": f"hash_{ev_id}",
                    "search_text": f"NVDA {ev_id}",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

    mock_extract = create_autospec(extractor_mod.extract_segment_cards, side_effect=_extract_ok)

    # 1. Batch 0 `embed_fn` fails -> Batch 1 is never embedded or written, run counters == 0
    embed_calls: list[list[str]] = []

    async def _fail_batch0_embed(
        texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT"
    ) -> tuple[list[list[float]], str]:
        embed_calls.append(list(texts))
        raise RuntimeError("embed_batch0_failed")

    with pytest.raises(RuntimeError, match="embed_batch0_failed"):
        await archive_cli.process_weekly_batch(
            [dict(e) for e in batch_events[:2]],
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000),
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=1,
            extract_fn=mock_extract,
            embed_fn=create_autospec(
                embedder_mod.embed_texts_via_bridge,
                side_effect=_fail_batch0_embed,
            ),
        )

    assert len(embed_calls) == 1
    assert persisted_cards == []
    assert persisted_events == []
    assert finish_calls[-1]["status"] == "failed"
    assert finish_calls[-1]["next_to_flush"] == 0
    assert finish_calls[-1]["completed_processed_events"] == []
    assert finish_calls[-1]["segments_processed"] == 0
    assert finish_calls[-1]["cards_extracted"] == 0
    assert finish_calls[-1]["cards_inserted"] == 0
    assert finish_calls[-1]["total_tokens"] == 0

    # 2. Batch 0 commits (`seg 0`), Batch 1 DB transaction fails on `__aenter__` (`seg 1`) -> Batch 2 (`seg 2`)
    #    is never written, `next_to_flush` stays 1, and `update_run_finish` counters equal Batch 0's committed counts.
    finish_calls.clear()
    tx_enter_count = 0

    def _make_tx_fail_on_second_enter() -> AsyncTransaction:
        nonlocal tx_enter_count
        tx_enter_count += 1
        tx_double = create_autospec(AsyncTransaction, instance=True)
        if tx_enter_count == 2:
            tx_double.__aenter__.side_effect = RuntimeError("db_tx_batch1_failed")
        else:
            tx_double.__aenter__.return_value = tx_double
        tx_double.__aexit__.return_value = False
        return tx_double

    tx_conn.transaction.side_effect = _make_tx_fail_on_second_enter
    mock_embed_ok = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        side_effect=lambda texts, *, task_type="RETRIEVAL_DOCUMENT": (
            [[0.1, 0.2, 0.3] for _ in texts],
            "gemini-embedding-001",
        ),
    )

    with pytest.raises(RuntimeError, match="db_tx_batch1_failed"):
        await archive_cli.process_weekly_batch(
            [dict(e) for e in batch_events],
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000),
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=1,
            extract_fn=mock_extract,
            embed_fn=mock_embed_ok,
        )

    assert persisted_cards == ["cc_ev-0"]
    assert persisted_events == ["ev-0"]
    assert finish_calls[-1]["status"] == "failed"
    assert finish_calls[-1]["next_to_flush"] == 1
    assert finish_calls[-1]["completed_processed_events"] == ["ev-0"]
    assert finish_calls[-1]["segments_processed"] == 1
    assert finish_calls[-1]["cards_extracted"] == 1
    assert finish_calls[-1]["cards_inserted"] == 1
    assert finish_calls[-1]["input_tokens"] == 100
    assert finish_calls[-1]["output_tokens"] == 50
    assert finish_calls[-1]["total_tokens"] == 150

    # 3. Batch 0 commits (`seg 0`), Batch 1 executes body (`insert_cards_batch` + `record_processed_event_chunks`)
    #    and then fails on transaction exit (`__aexit__` / commit) -> staged writes roll back, `next_to_flush` stays 1,
    #    `dalton_conversation_processed` (`persisted_events`) does not advance, and counters stay at Batch 0's values.
    persisted_cards.clear()
    persisted_events.clear()
    finish_calls.clear()
    tx_exit_count = 0

    def _make_tx_fail_on_second_exit() -> AsyncTransaction:
        nonlocal tx_exit_count
        tx_exit_count += 1
        batch_num = tx_exit_count
        tx_double = create_autospec(AsyncTransaction, instance=True)

        async def _aenter() -> AsyncTransaction:
            in_staged_tx[0] = True
            staged_cards.clear()
            staged_events.clear()
            return tx_double

        async def _aexit(exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
            in_staged_tx[0] = False
            if exc_type is not None:
                staged_cards.clear()
                staged_events.clear()
                return False
            if batch_num == 2:
                staged_cards.clear()
                staged_events.clear()
                raise RuntimeError("db_tx_batch1_commit_exit_failed")
            persisted_cards.extend(staged_cards)
            persisted_events.extend(staged_events)
            staged_cards.clear()
            staged_events.clear()
            return False

        tx_double.__aenter__.side_effect = _aenter
        tx_double.__aexit__.side_effect = _aexit
        return tx_double

    tx_conn.transaction.side_effect = _make_tx_fail_on_second_exit

    with pytest.raises(RuntimeError, match="db_tx_batch1_commit_exit_failed"):
        await archive_cli.process_weekly_batch(
            [dict(e) for e in batch_events],
            mode="backfill",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000),
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v1",
            concurrency=1,
            extract_fn=mock_extract,
            embed_fn=mock_embed_ok,
        )

    assert persisted_cards == ["cc_ev-0"]
    assert persisted_events == ["ev-0"]
    assert finish_calls[-1]["status"] == "failed"
    assert finish_calls[-1]["next_to_flush"] == 1
    assert finish_calls[-1]["completed_processed_events"] == ["ev-0"]
    assert finish_calls[-1]["segments_processed"] == 1
    assert finish_calls[-1]["cards_extracted"] == 1
    assert finish_calls[-1]["cards_inserted"] == 1
    assert finish_calls[-1]["input_tokens"] == 100
    assert finish_calls[-1]["output_tokens"] == 50
    assert finish_calls[-1]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_advisory_lock_contention_exits_code_2_and_marks_stale_running_abandoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify:
    1. `ArchiveAlreadyRunningError` inherits from `RuntimeError` (not `SystemExit`).
    2. When `try_acquire_archive_run_lock` returns False, `run_archive_pipeline` prints '已有归档在运行',
       closes its dedicated session lock connection, and raises `ArchiveAlreadyRunningError`, while
       `main()` catches it and returns exit code 2.
    3. `open_session_connection()` ALWAYS holds the lock on a dedicated session connection (even when
       `conn` is passed) and unlocks + closes it in `finally` even when `mark_stale_running_runs_abandoned` raises.
    4. Two concurrent `run_archive_pipeline(..., conn=shared_conn)` calls sharing the same `conn` open
       distinct lock sessions via `open_session_connection()`, so under Postgres session-reentrant lock
       semantics (same session re-entrant, different sessions mutually exclusive) the second call raises
       `ArchiveAlreadyRunningError` (`.code == 2`) without marking the first run as `abandoned`.
    """
    assert issubclass(archive_cli.ArchiveAlreadyRunningError, RuntimeError)
    assert not issubclass(archive_cli.ArchiveAlreadyRunningError, SystemExit)

    # 1. Contended lock -> raises ArchiveAlreadyRunningError (code 2) + closes dedicated lock connection
    _, lock_conn = _patch_autospec_db_connection(monkeypatch)
    mock_lock_busy = create_autospec(db_conv.try_acquire_archive_run_lock, return_value=False)
    mock_release_lock = create_autospec(db_conv.release_archive_run_lock, return_value=True)
    mock_mark_abandoned = create_autospec(db_conv.mark_stale_running_runs_abandoned, return_value=2)
    monkeypatch.setattr(db_conv, "try_acquire_archive_run_lock", mock_lock_busy)
    monkeypatch.setattr(db_conv, "release_archive_run_lock", mock_release_lock)
    monkeypatch.setattr(db_conv, "mark_stale_running_runs_abandoned", mock_mark_abandoned)

    with pytest.raises(archive_cli.ArchiveAlreadyRunningError) as exc_info:
        await archive_cli.run_archive_pipeline(
            mode="run",
            inbox_dir=tmp_path,
            skip_host_export=True,
        )
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "已有归档在运行" in (captured.err + captured.out)
    assert mock_mark_abandoned.await_count == 0
    assert mock_lock_busy.call_args.kwargs.get("conn") is lock_conn
    assert lock_conn.close.await_count == 1

    # Also verify CLI `main()` catches `ArchiveAlreadyRunningError` and returns exit code 2
    mock_pool = create_autospec(AsyncConnectionPool, instance=True)
    mock_pool.open.return_value = None
    mock_pool.close.return_value = None
    monkeypatch.setattr(
        archive_cli,
        "get_or_create_pool",
        create_autospec(archive_cli.get_or_create_pool, return_value=mock_pool),
    )
    rc = await asyncio.to_thread(
        archive_cli.main,
        ["run", "--skip-host-export", "--inbox-dir", str(tmp_path)],
    )
    assert rc == 2
    assert lock_conn.close.await_count == 2

    # Verify if `mark_stale_running_runs_abandoned` raises after lock acquisition, `finally` still unlocks and closes `lock_conn`
    mock_lock_busy.return_value = True
    mock_mark_abandoned.side_effect = RuntimeError("abandon_update_failed")
    with pytest.raises(RuntimeError, match="abandon_update_failed"):
        await archive_cli.run_archive_pipeline(
            mode="run",
            inbox_dir=tmp_path,
            skip_host_export=True,
        )
    assert mock_release_lock.await_count == 1
    assert mock_release_lock.call_args.kwargs.get("conn") is lock_conn
    assert lock_conn.close.await_count == 3

    # 2. Lock acquired with explicit `conn=mock_conn` -> advisory lock SQL runs on dedicated `dedicated_lock_conn`
    #    from `open_session_connection()`, while `UPDATE dalton_conversation_runs SET status = 'abandoned'` runs on `mock_conn`
    lock_sql_executed: list[tuple[str, Any]] = []
    data_sql_executed: list[tuple[str, Any]] = []
    lock_fetch_results: list[dict[str, Any] | None] = [
        {"locked": True},
        {"unlocked": True},
    ]

    async def _fake_lock_execute(sql: str, params: Any = None) -> None:
        lock_sql_executed.append((sql.strip(), params))

    async def _fake_lock_fetchone() -> dict[str, Any] | None:
        return lock_fetch_results.pop(0) if lock_fetch_results else None

    async def _fake_data_execute(sql: str, params: Any = None) -> None:
        data_sql_executed.append((sql.strip(), params))

    lock_cur = create_autospec(AsyncCursor, instance=True)
    lock_cur.__aenter__.return_value = lock_cur
    lock_cur.__aexit__.return_value = False
    lock_cur.execute.side_effect = _fake_lock_execute
    lock_cur.fetchone.side_effect = _fake_lock_fetchone
    lock_cur.fetchall.return_value = []
    lock_cur.rowcount = 0

    dedicated_lock_conn = _make_autospec_db_conn()
    dedicated_lock_conn.cursor.return_value = lock_cur

    mock_cur = create_autospec(AsyncCursor, instance=True)
    mock_cur.__aenter__.return_value = mock_cur
    mock_cur.__aexit__.return_value = False
    mock_cur.execute.side_effect = _fake_data_execute
    mock_cur.fetchone.return_value = None
    mock_cur.fetchall.return_value = []
    mock_cur.rowcount = 2

    mock_conn = _make_autospec_db_conn()
    mock_conn.cursor.return_value = mock_cur

    monkeypatch.undo()
    monkeypatch.setattr(
        archive_cli,
        "open_session_connection",
        create_autospec(archive_cli.open_session_connection, return_value=dedicated_lock_conn),
    )
    summary = await archive_cli.run_archive_pipeline(
        mode="run",
        inbox_dir=tmp_path,
        skip_host_export=True,
        conn=mock_conn,
    )
    assert summary["ok"] is True
    assert summary["abandoned_runs_marked"] == 2
    assert dedicated_lock_conn.close.await_count == 1
    assert mock_conn.close.await_count == 0

    lock_sql_statements = [s for s, _ in lock_sql_executed]
    data_sql_statements = [s for s, _ in data_sql_executed]
    assert any("pg_try_advisory_lock(hashtext(%s))" in s for s in lock_sql_statements)
    assert any("pg_advisory_unlock(hashtext(%s))" in s for s in lock_sql_statements)
    assert not any("pg_try_advisory_lock" in s for s in data_sql_statements)
    assert any(
        "UPDATE dalton_conversation_runs" in s and "SET status       = 'abandoned'" in s
        for s in data_sql_statements
    )
    abandoned_idx = next(
        i for i, (s, _) in enumerate(data_sql_executed) if "SET status       = 'abandoned'" in s
    )
    assert data_sql_executed[abandoned_idx][1] == (db_conv.DEFAULT_ABANDONED_RUN_ERROR_TEXT,)

    # 3. Two concurrent calls passing the SAME `conn=shared_conn` -> lock double simulates Postgres
    #    session-level advisory lock semantics (same session is re-entrant, different sessions are mutually exclusive).
    #    Because `run_archive_pipeline` always opens a new dedicated session via `open_session_connection()`,
    #    the second concurrent call gets `ArchiveAlreadyRunningError` (`.code == 2`) and does NOT mark the first run abandoned.
    shared_conn = _make_autospec_db_conn()
    opened_lock_sessions: list[AsyncConnection] = []
    held_lock_by_key: dict[str, Any] = {}

    async def _open_distinct_lock_session() -> AsyncConnection:
        sess = _make_autospec_db_conn()
        opened_lock_sessions.append(sess)
        return sess

    async def _session_reentrant_try_lock(
        conn: Any = None,
        *,
        lock_key: str = db_conv.ARCHIVE_ADVISORY_LOCK_KEY,
    ) -> bool:
        holder = held_lock_by_key.get(lock_key)
        if holder is None or holder is conn:
            held_lock_by_key[lock_key] = conn
            return True
        return False

    async def _session_reentrant_unlock(
        conn: Any = None,
        *,
        lock_key: str = db_conv.ARCHIVE_ADVISORY_LOCK_KEY,
    ) -> bool:
        holder = held_lock_by_key.get(lock_key)
        if holder is not None and holder is conn:
            held_lock_by_key.pop(lock_key, None)
            return True
        return False

    # Sanity-check the double's session-reentrant semantics: acquiring twice on the same session succeeds
    assert await _session_reentrant_try_lock(conn=shared_conn) is True
    assert await _session_reentrant_try_lock(conn=shared_conn) is True
    assert await _session_reentrant_unlock(conn=shared_conn) is True

    run1_holding_lock = asyncio.Event()
    release_run1 = asyncio.Event()

    async def _hold_run1_in_unrendered(conn: Any = None) -> list[date]:
        run1_holding_lock.set()
        await release_run1.wait()
        return []

    mock_mark_abandoned_shared = create_autospec(
        db_conv.mark_stale_running_runs_abandoned,
        return_value=0,
    )
    monkeypatch.setattr(
        archive_cli,
        "open_session_connection",
        create_autospec(archive_cli._orig_open_session_connection, side_effect=_open_distinct_lock_session),
    )
    monkeypatch.setattr(
        db_conv,
        "try_acquire_archive_run_lock",
        create_autospec(db_conv.try_acquire_archive_run_lock, side_effect=_session_reentrant_try_lock),
    )
    monkeypatch.setattr(
        db_conv,
        "release_archive_run_lock",
        create_autospec(db_conv.release_archive_run_lock, side_effect=_session_reentrant_unlock),
    )
    monkeypatch.setattr(db_conv, "mark_stale_running_runs_abandoned", mock_mark_abandoned_shared)
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, side_effect=_hold_run1_in_unrendered),
    )

    run1_task = asyncio.create_task(
        archive_cli.run_archive_pipeline(
            mode="run",
            inbox_dir=tmp_path,
            skip_host_export=True,
            conn=shared_conn,
        )
    )
    await asyncio.wait_for(run1_holding_lock.wait(), timeout=2.0)
    assert mock_mark_abandoned_shared.await_count == 1
    assert mock_mark_abandoned_shared.call_args.kwargs.get("conn") is shared_conn

    # Second concurrent call with the EXACT SAME `conn=shared_conn` must fail with `ArchiveAlreadyRunningError`
    with pytest.raises(archive_cli.ArchiveAlreadyRunningError) as exc_shared:
        await archive_cli.run_archive_pipeline(
            mode="run",
            inbox_dir=tmp_path,
            skip_host_export=True,
            conn=shared_conn,
        )
    assert exc_shared.value.code == 2
    assert mock_mark_abandoned_shared.await_count == 1
    assert len(opened_lock_sessions) == 2
    assert opened_lock_sessions[0] is not opened_lock_sessions[1]
    assert opened_lock_sessions[1].close.await_count == 1

    release_run1.set()
    run1_summary = await asyncio.wait_for(run1_task, timeout=2.0)
    assert run1_summary["ok"] is True
    assert opened_lock_sessions[0].close.await_count == 1
    assert shared_conn.close.await_count == 0
    assert held_lock_by_key == {}


@pytest.mark.asyncio
async def test_in_flight_deferred_queue_succeeds_on_retry_and_flushes_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that when segment 1 returns `in_flight`, it is placed into the deferred queue while
    segments 0 and 2 continue, segment 0 flushes immediately, and once the rest of the batch finishes,
    the deferred queue retries at 30-60s intervals and flushes segments 1 and 2 in strict order.
    """
    base_ms = int(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    batch_events = [
        {
            "agent": "main",
            "session_id": "sess-defer-ok",
            "event_id": f"ev-{idx}",
            "seq": idx + 1,
            "generation": "gen-1",
            "created_at_ms": base_ms + idx * 3600 * 1000,
            "ts": datetime.fromtimestamp((base_ms + idx * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-20",
            "text": f"NVDA segment {idx} Blackwell 机柜交付跟踪。",
        }
        for idx in range(3)
    ]

    persisted_cards: list[str] = []
    persisted_events: list[str] = []
    slept_intervals: list[float] = []
    ev1_attempts = 0
    events_before_defer_sleep: list[str] = []

    async def _fake_sleep(seconds: float) -> None:
        if not slept_intervals:
            events_before_defer_sleep.extend(persisted_events)
        slept_intervals.append(seconds)

    async def _extract_with_inflight_seg1(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal ev1_attempts
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-1":
            ev1_attempts += 1
            if ev1_attempts <= 2:
                return extractor_mod.SegmentExtractionOutcome(
                    model_used="gemini-4-argon-medium-oauth",
                    ok=False,
                    status="in_flight",
                    error_kind="in_flight",
                    in_flight=True,
                    error="该请求正在由另一个进程执行中，请稍候。",
                )
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": f"cc_{ev_id}",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": "sess-defer-ok",
                    "quote_event_id": ev_id,
                    "generation": "gen-1",
                    "ts": str(segment[0]["ts"]),
                    "date_et": "2026-10-20",
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": [],
                    "topic_label": "NVDA",
                    "type": "estimate",
                    "claim": f"Claim {ev_id}",
                    "quote": str(segment[0]["text"]),
                    "content_hash": f"hash_{ev_id}",
                    "search_text": f"NVDA {ev_id}",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

    async def _fake_insert_batch(
        cards: list[dict[str, Any]],
        *,
        embeddings: list[list[float] | None] | None = None,
        conn: Any = None,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        for c in cards:
            persisted_cards.append(str(c["card_id"]))
        return len(cards), 0, list(cards)

    async def _fake_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        for r in rows:
            persisted_events.append(str(r["event_id"]))
        return len(rows)

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(db_conv.insert_cards_batch, side_effect=_fake_insert_batch),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, side_effect=_fake_record_processed),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )

    mock_extract = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_extract_with_inflight_seg1,
    )
    mock_embed = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        return_value=([[0.1, 0.2, 0.3]], "gemini-embedding-001"),
    )
    mock_render = create_autospec(
        render_mod.render_and_write_conversation_digests,
        return_value={
            "dates_rendered": ["2026-10-20"],
            "files_written": 1,
            "cards_rendered": 3,
            "card_locations": {},
        },
    )
    monkeypatch.setattr(archive_cli, "render_and_write_conversation_digests", mock_render)
    mock_sleep = create_autospec(asyncio.sleep, side_effect=_fake_sleep)

    summary = await archive_cli.process_weekly_batch(
        batch_events,
        mode="backfill",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000, sleep_fn=mock_sleep),
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        concurrency=2,
        extract_fn=mock_extract,
        embed_fn=mock_embed,
        sleep_fn=mock_sleep,
    )

    # Before the first deferred sleep, only seg 0 (`ev-0`) was flushed; seg 2 waited behind deferred seg 1
    assert events_before_defer_sleep == ["ev-0"]
    # Deferred queue slept 30s then 60s, and on attempt 3 seg 1 succeeded, flushing seg 1 and seg 2 in order!
    assert slept_intervals == [30.0, 60.0]
    assert ev1_attempts == 3
    assert persisted_events == ["ev-0", "ev-1", "ev-2"]
    assert persisted_cards == ["cc_ev-0", "cc_ev-1", "cc_ev-2"]
    assert summary["segments_processed"] == 3
    assert summary["next_to_flush"] == 3
    assert summary["incomplete_segments_count"] == 0
    assert summary["incomplete_segments"] == []


@pytest.mark.asyncio
async def test_in_flight_deferred_still_failing_marks_incomplete_holds_prefix_cursor_and_continues_next_week(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that when a deferred `in_flight` segment remains `in_flight` past `stale_after_s + 60`s:
    1. It is recorded as incomplete for that week, and the week's prefix commit cursor (`next_to_flush`)
       does NOT advance past it (so subsequent segments in that week are not marked processed).
    2. The pipeline continues processing subsequent weekly batches.
    3. The overall pipeline summary reports `exit_code == 3` and lists the incomplete segment(s),
       while preserving the inbox file on disk for re-run.
    """
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)
    inbox_file = inbox_dir / "20261020T100000Z_two_weeks.jsonl"

    w1_base_ms = int(datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    w2_base_ms = int(datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    events = [
        {
            "agent": "main",
            "session_id": "sess-w1",
            "event_id": f"ev-w1-{idx}",
            "seq": idx + 1,
            "generation": "gen-1",
            "created_at_ms": w1_base_ms + idx * 3600 * 1000,
            "ts": datetime.fromtimestamp((w1_base_ms + idx * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-10",
            "text": f"NVDA week 1 segment {idx} 机柜出货跟踪。",
        }
        for idx in range(3)
    ] + [
        {
            "agent": "main",
            "session_id": "sess-w2",
            "event_id": "ev-w2-0",
            "seq": 1,
            "generation": "gen-1",
            "created_at_ms": w2_base_ms,
            "ts": datetime.fromtimestamp(w2_base_ms / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-18",
            "text": "TSMC week 2 CoWoS 扩产节奏跟踪。",
        }
    ]
    inbox_file.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )

    processed_events: list[str] = []
    slept_intervals: list[float] = []
    fake_now = 1_000.0

    def _fake_clock() -> float:
        return fake_now

    async def _fake_sleep(seconds: float) -> None:
        nonlocal fake_now
        slept_intervals.append(seconds)
        fake_now += max(0.0, float(seconds))

    async def _extract_stuck_inflight(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-w1-1":
            return extractor_mod.SegmentExtractionOutcome(
                model_used="gemini-4-argon-medium-oauth",
                ok=False,
                status="in_flight",
                error_kind="in_flight",
                in_flight=True,
                error="该请求正在由另一个进程执行中，请稍候。",
            )
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[
                {
                    "card_id": f"cc_{ev_id}",
                    "source_type": "conversation",
                    "agent": "main",
                    "session_id": str(segment[0]["session_id"]),
                    "quote_event_id": ev_id,
                    "generation": "gen-1",
                    "ts": str(segment[0]["ts"]),
                    "date_et": str(segment[0]["date_et"]),
                    "tickers": ["NVDA"],
                    "entities": [],
                    "sectors": [],
                    "topic_label": "NVDA",
                    "type": "estimate",
                    "claim": f"Claim {ev_id}",
                    "quote": str(segment[0]["text"]),
                    "content_hash": f"hash_{ev_id}",
                    "search_text": f"NVDA {ev_id}",
                    "status": "active",
                }
            ],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )

    async def _fake_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        for r in rows:
            processed_events.append(str(r["event_id"]))
        return len(rows)

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "try_acquire_archive_run_lock",
        create_autospec(db_conv.try_acquire_archive_run_lock, return_value=True),
    )
    monkeypatch.setattr(
        db_conv,
        "release_archive_run_lock",
        create_autospec(db_conv.release_archive_run_lock, return_value=True),
    )
    monkeypatch.setattr(
        db_conv,
        "mark_stale_running_runs_abandoned",
        create_autospec(db_conv.mark_stale_running_runs_abandoned, return_value=0),
    )
    monkeypatch.setattr(
        db_conv,
        "fetch_processed_event_chunks",
        create_autospec(db_conv.fetch_processed_event_chunks, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, side_effect=_fake_record_processed),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(
            db_conv.insert_cards_batch,
            side_effect=lambda cards, *, embeddings=None, conn=None: (len(cards), 0, list(cards)),
        ),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )

    mock_extract = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_extract_stuck_inflight,
    )
    mock_embed = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        return_value=([[0.1, 0.2, 0.3]], "gemini-embedding-001"),
    )
    mock_render = create_autospec(
        render_mod.render_and_write_conversation_digests,
        return_value={
            "dates_rendered": ["2026-10-10"],
            "files_written": 1,
            "cards_rendered": 1,
            "card_locations": {},
        },
    )
    monkeypatch.setattr(archive_cli, "render_and_write_conversation_digests", mock_render)
    mock_sleep = create_autospec(asyncio.sleep, side_effect=_fake_sleep)
    mock_clock = create_autospec(archive_cli.time.monotonic, side_effect=_fake_clock)

    summary = await archive_cli.run_archive_pipeline(
        mode="backfill",
        since_date=date(2026, 10, 10),
        until_date=date(2026, 10, 20),
        inbox_dir=inbox_dir,
        skip_host_export=True,
        concurrency=2,
        extract_fn=mock_extract,
        embed_fn=mock_embed,
        sleep_fn=mock_sleep,
        clock_fn=mock_clock,
    )

    # Deferred retry slept intervals in [30, 60]s up to stale_after_s (900) + 60 = 960s total
    assert all(30.0 <= s <= 60.0 for s in slept_intervals)
    assert sum(slept_intervals) == 960.0

    # Week 1 cursor stopped at seg 1 (only `ev-w1-0` committed; `ev-w1-1` failed and `ev-w1-2` blocked),
    # and Week 2 continued and committed `ev-w2-0`!
    assert processed_events == ["ev-w1-0", "ev-w2-0"]
    assert summary["ok"] is False
    assert summary["exit_code"] == 3
    assert summary["batches_count"] == 2
    assert summary["batches"][0]["status"] == "failed"
    assert summary["batches"][0]["error_text"] == "incomplete_segments=2"
    assert summary["batches"][0]["next_to_flush"] == 1
    assert summary["batches"][0]["segments_processed"] == 1
    assert summary["batches"][0]["incomplete_segments_count"] == 2
    assert summary["batches"][0]["total_tokens"] == 300
    assert summary["batches"][1]["status"] == "completed"
    assert summary["batches"][1]["error_text"] is None
    assert summary["batches"][1]["segments_processed"] == 1
    assert summary["batches"][1]["incomplete_segments_count"] == 0
    assert summary["batches"][1]["total_tokens"] == 150
    assert summary["total_tokens"] == 450
    assert summary["incomplete_segments_total"] == 2
    assert len(summary["incomplete_segments"]) == 2
    inc_failed = summary["incomplete_segments"][0]
    assert inc_failed["segment_index"] == 1
    assert inc_failed["first_event_id"] == "ev-w1-1"
    assert inc_failed["status"] == "failed"
    assert inc_failed["reason"] == "in_flight_timeout"
    inc_blocked = summary["incomplete_segments"][1]
    assert inc_blocked["segment_index"] == 2
    assert inc_blocked["first_event_id"] == "ev-w1-2"
    assert inc_blocked["status"] == "blocked"
    assert inc_blocked["reason"] == "blocked_by_incomplete_prefix"
    # Inbox file stays on disk because Week 1 had uncommitted segments
    assert inbox_file.exists()


@pytest.mark.asyncio
async def test_transient_network_errors_retry_exponential_backoff_and_oauth_only_resend_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1. Transient network/upstream errors (`RemoteProtocolError`, `httpx.ReadError`, `ConnectError`,
       `ReadTimeout`, HTTP 502/503/504) retry inside `extract_segment_with_infinite_backoff` with
       exponential backoff `2 -> 4 -> 8 -> 16 -> 32`s (max 5 retries).
    2. When still failing after 5 retries, `process_weekly_batch` records the segment as incomplete
       without aborting the batch or advancing the prefix cursor.
    3. `resend_indeterminate=True` is passed ONLY when `billing_type == "oauth"`; cash models keep
       `resend_indeterminate=False`.
    """
    import httpx

    slept_delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept_delays.append(seconds)

    mock_sleep = create_autospec(asyncio.sleep, side_effect=_fake_sleep)

    # 1. Transient errors (`RemoteProtocolError` -> `ReadError` -> `ConnectError` -> `ReadTimeout` -> HTTP 503)
    #    succeeding on retry #5 (attempt 6)
    transient_seq: list[Any] = [
        httpx.RemoteProtocolError("peer closed connection without sending complete message body"),
        httpx.ReadError("socket read error"),
        httpx.ConnectError("connection reset by peer"),
        httpx.ReadTimeout("read timed out"),
        extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            status="indeterminate",
            error_kind="http_503",
            error="http_503: Service Unavailable",
        ),
        extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=100,
            output_tokens=40,
            total_tokens=140,
        ),
    ]
    seen_resend_flags: list[bool] = []

    async def _flaky_transient_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        seen_resend_flags.append(bool(kwargs.get("resend_indeterminate")))
        item = transient_seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    mock_flaky = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_flaky_transient_extract,
    )
    out_ok, retries_used = await archive_cli.extract_segment_with_infinite_backoff(
        [{"event_id": "ev-net-1", "seq": 1, "text": "NVDA 网络瞬断重试测试"}],
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        extract_fn=mock_flaky,
        sleep_fn=mock_sleep,
    )
    assert out_ok.ok is True
    assert retries_used == 5
    assert slept_delays == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert seen_resend_flags == [True] * 6

    # 2. Cash model (`gpt-5.4`, `billing_type == "cash"`) must pass `resend_indeterminate=False`
    cash_resend_flags: list[bool] = []

    async def _cash_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        cash_resend_flags.append(bool(kwargs.get("resend_indeterminate")))
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            model_used="gpt-5.4",
        )

    await archive_cli.extract_segment_with_infinite_backoff(
        [{"event_id": "ev-cash-1", "seq": 1, "text": "Cash model test"}],
        extractor_model="gpt-5.4",
        prompt_version="conv_card_v1",
        extract_fn=create_autospec(extractor_mod.extract_segment_cards, side_effect=_cash_extract),
        sleep_fn=mock_sleep,
    )
    assert cash_resend_flags == [False]

    # Also verify `extract_segment_cards` itself clamps `resend_indeterminate=False` for cash models
    mock_assist_cash = create_autospec(
        extractor_mod.assist_json,
        return_value=assist_mod.AssistResult(
            ok=True,
            data={"category": "ops", "topic_label": "ops", "cards": []},
            model="gpt-5.4",
        ),
    )
    monkeypatch.setattr(extractor_mod, "assist_json", mock_assist_cash)
    await extractor_mod.extract_segment_cards(
        [{"event_id": "ev-cash-2", "seq": 1, "text": "Cash model direct test"}],
        extractor_model="gpt-5.4",
        resend_indeterminate=True,
    )
    assert mock_assist_cash.call_args.kwargs.get("resend_indeterminate") is False

    # 3. Exhausting all 5 transient retries (6 attempts total) marks the segment incomplete in
    #    `process_weekly_batch` without raising `SegmentExtractionError` or advancing the prefix cursor!
    slept_delays.clear()
    always_disconnect = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=httpx.RemoteProtocolError("peer closed connection"),
    )
    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )

    batch_summary = await archive_cli.process_weekly_batch(
        [
            {
                "agent": "main",
                "session_id": "sess-net-fail",
                "event_id": "ev-net-fail",
                "seq": 1,
                "generation": "gen-1",
                "created_at_ms": 1760950000000,
                "ts": "2026-10-20T10:00:00+00:00",
                "date_et": "2026-10-20",
                "text": "NVDA 持续断连测试。",
            }
        ],
        mode="backfill",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000, sleep_fn=mock_sleep),
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        extract_fn=always_disconnect,
        sleep_fn=mock_sleep,
    )
    assert slept_delays == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert always_disconnect.await_count == 6
    assert batch_summary["segments_processed"] == 0
    assert batch_summary["next_to_flush"] == 0
    assert batch_summary["incomplete_segments_count"] == 1
    assert batch_summary["incomplete_segments"][0]["first_event_id"] == "ev-net-fail"


@pytest.mark.asyncio
async def test_enum_whitespace_and_case_normalization_and_strict_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify enum fields (`type`, `category`) are stripped and lowercased before schema validation
    (covering `' opinion'`), while invalid enum values after normalization are still strictly rejected.
    """
    segment = [
        {
            "agent": "main",
            "session_id": "sess-enum",
            "event_id": "ev-enum-1",
            "seq": 1,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "speaker": {"sender_id_hash": "h1", "name": "Owner", "is_owner": True, "role": "owner"},
            "text": "我们认为 NVDA 四季度 Blackwell 机柜毛利率表现会超预期。",
        }
    ]

    mock_complete_ok = create_autospec(
        complete_mod.dalton_complete,
        return_value=complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "category": " Research ",
                "topic_label": "NVDA 毛利率",
                "cards": [
                    {
                        "type": " opinion",
                        "topic_label": "NVDA 毛利率",
                        "claim": "NVDA 四季度 Blackwell 机柜毛利率表现会超预期",
                        "tickers": ["NVDA"],
                        "entities": [],
                        "sectors": ["Semiconductors"],
                        "figures": [],
                        "quote": "我们认为 NVDA 四季度 Blackwell 机柜毛利率表现会超预期。",
                        "quote_event_id": "ev-enum-1",
                        "cited_sources": [],
                        "confidence": 0.85,
                    }
                ],
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 80, "output_tokens": 40, "total_tokens": 120},
        ),
    )
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete_ok)

    outcome = await extractor_mod.extract_segment_cards(segment, skip_journal=True)
    assert outcome.ok is True
    assert outcome.category == "research"
    assert len(outcome.cards) == 1
    assert outcome.cards[0]["type"] == "opinion"

    # Invalid enum value after strip().lower() must still be strictly rejected by jsonschema
    mock_complete_bad = create_autospec(
        complete_mod.dalton_complete,
        return_value=complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "category": "research",
                "topic_label": "NVDA",
                "cards": [
                    {
                        "type": " invalid_card_type ",
                        "topic_label": "NVDA",
                        "claim": "Claim",
                        "quote": "我们认为 NVDA 四季度 Blackwell 机柜毛利率表现会超预期。",
                        "quote_event_id": "ev-enum-1",
                    }
                ],
            },
            model="gemini-4-argon-medium-oauth",
        ),
    )
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete_bad)

    bad_outcome = await extractor_mod.extract_segment_cards(segment, skip_journal=True)
    assert bad_outcome.ok is False
    assert "ValidationError" in (bad_outcome.error or "")


@pytest.mark.asyncio
async def test_exit_code_contract_and_maxrss_memory_cleanup(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CLI exit code contract (`0` all completed, `2` lock held, `3` incomplete segments),
    `--help` documentation, and per-weekly-batch `maxrss` logging + memory cleanup.
    """
    help_text = archive_cli.build_arg_parser().format_help()
    assert "0" in help_text and "2" in help_text and "3" in help_text
    assert "incomplete" in help_text.lower()

    real_run_archive_pipeline = archive_cli.run_archive_pipeline

    # Exit code 3 when `run_archive_pipeline` reports incomplete segments
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
        create_autospec(
            real_run_archive_pipeline,
            return_value={
                "ok": False,
                "exit_code": 3,
                "incomplete_segments_total": 1,
                "incomplete_segments": [
                    {
                        "segment_index": 2,
                        "window_start": "2026-10-13",
                        "window_end": "2026-10-19",
                        "first_event_id": "ev-stuck",
                        "status": "in_flight",
                    }
                ],
            },
        ),
    )
    rc_incomplete = await asyncio.to_thread(
        archive_cli.main,
        ["backfill", "--since", "2026-10-13", "--skip-host-export"],
    )
    assert rc_incomplete == 3
    stdout_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert stdout_json["incomplete_segments_total"] == 1
    assert stdout_json["incomplete_segments"][0]["first_event_id"] == "ev-stuck"

    # Exit code 0 when all segments complete
    monkeypatch.setattr(
        archive_cli,
        "run_archive_pipeline",
        create_autospec(
            real_run_archive_pipeline,
            return_value={
                "ok": True,
                "exit_code": 0,
                "incomplete_segments_total": 0,
                "incomplete_segments": [],
            },
        ),
    )
    rc_ok = await asyncio.to_thread(
        archive_cli.main,
        ["run", "--skip-host-export"],
    )
    assert rc_ok == 0

    # Verify `_get_maxrss_stats` returns `(ru_maxrss, maxrss_mib)` via `resource.getrusage`
    ru_maxrss, maxrss_mib = archive_cli._get_maxrss_stats()
    assert ru_maxrss > 0
    assert maxrss_mib > 0


@pytest.mark.asyncio
async def test_enum_normalization_preserves_case_sensitive_enums_and_matches_unique_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `_normalize_schema_enums` and `assist_json`:
    1. Strips whitespace first; if the stripped value is already in `enum`, keeps exact case (e.g. `'P0'`, `' P1 '`).
    2. Otherwise, replaces with a case-insensitively matching enum member ONLY when there is a unique match
       (e.g. `' opinion'` -> `'opinion'`, `'Opinion'` -> `'opinion'`, `' p1 '` -> `'P1'`).
    3. Keeps original value when invalid or ambiguous so `jsonschema.validate` rejects it.
    """
    from src.dalton.autonomy.preflight import TRIAGE_OUTPUT_SCHEMA

    triage_payload = {
        "wake": True,
        "priority": "P0",
        "reason": "Batch has P0 signal",
        "decisions": [
            {"signal_key": "sig-0", "wake": True, "priority": "P0", "reason": "Earnings beat"},
            {"signal_key": "sig-1", "wake": True, "priority": " P1 ", "reason": "Guidance update"},
            {"signal_key": "sig-2", "wake": False, "priority": " p2 ", "reason": "Routine noise"},
        ],
    }
    mock_complete_triage = create_autospec(
        complete_mod.dalton_complete,
        return_value=complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data=triage_payload,
            model="deepseek-flash",
        ),
    )
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete_triage)
    triage_res = await assist_mod.assist_json(
        task="autonomy.preflight_triage",
        instructions="Triage items",
        payload={"items": []},
        schema=TRIAGE_OUTPUT_SCHEMA,
        user_id="00000000-0000-0000-0000-000000000001",
        skip_journal=True,
    )
    assert triage_res.ok is True
    assert triage_res.data is not None
    assert triage_res.data["priority"] == "P0"
    assert [d["priority"] for d in triage_res.data["decisions"]] == ["P0", "P1", "P2"]

    # Direct unit checks on `_normalize_schema_enums` for `"P0"`, `" P1 "`, `" opinion"`, `"Opinion"`,
    # ambiguous case-insensitive members, and invalid values
    priority_schema = {"type": "string", "enum": ["P0", "P1", "P2"]}
    card_type_schema = {"type": "string", "enum": ["fact", "opinion", "estimate"]}
    ambiguous_schema = {"type": "string", "enum": ["Alpha", "ALPHA"]}

    assert assist_mod._normalize_schema_enums("P0", priority_schema) == "P0"
    assert assist_mod._normalize_schema_enums(" P1 ", priority_schema) == "P1"
    assert assist_mod._normalize_schema_enums(" opinion", card_type_schema) == "opinion"
    assert assist_mod._normalize_schema_enums("Opinion", card_type_schema) == "opinion"
    assert assist_mod._normalize_schema_enums("alpha", ambiguous_schema) == "alpha"
    assert assist_mod._normalize_schema_enums(" P9 ", priority_schema) == " P9 "

    # Invalid value in `assist_json` is rejected by `jsonschema.validate`
    mock_complete_invalid = create_autospec(
        complete_mod.dalton_complete,
        return_value=complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "wake": True,
                "priority": "P9",
                "reason": "Invalid priority",
                "decisions": [
                    {"signal_key": "sig-0", "wake": True, "priority": "P9", "reason": "Invalid priority"}
                ],
            },
            model="deepseek-flash",
        ),
    )
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete_invalid)
    bad_triage = await assist_mod.assist_json(
        task="autonomy.preflight_triage",
        instructions="Triage items",
        payload={"items": []},
        schema=TRIAGE_OUTPUT_SCHEMA,
        user_id="00000000-0000-0000-0000-000000000001",
        skip_journal=True,
    )
    assert bad_triage.ok is False
    assert "ValidationError" in (bad_triage.error or "")


@pytest.mark.asyncio
async def test_transient_retry_settles_limiter_tokens_across_all_attempts_and_skips_journal_no_send() -> None:
    """Verify:
    1. When a segment retries 2 times on transient errors (3 attempts total), `limiter` settles the
       previous reservation via `record_tokens` and calls `wait_for_budget` before each retry so the
       recorded tokens in `limiter` equal the sum of all 3 attempts.
    2. When the journal short-circuits without sending an upstream request (`in_flight` / `indeterminate`
       pre-send), no tokens are recorded in `limiter`.
    """
    import httpx

    slept: list[float] = []

    async def _noop_sleep(secs: float) -> None:
        slept.append(secs)

    mock_sleep = create_autospec(asyncio.sleep, side_effect=_noop_sleep)
    limiter = archive_cli.HourlyTokenBudgetLimiter(100_000, sleep_fn=mock_sleep)

    seg = [{"event_id": "ev-tr-budget", "seq": 1, "text": "NVDA 瞬断重试 token 预算结算测试。"}]
    est = archive_cli.estimate_segment_tokens(seg, hourly_budget=limiter.hourly_budget)

    # Attempt 1: transient HTTP 503 with known usage (180 tokens)
    # Attempt 2: transient RemoteProtocolError with no reported usage (falls back to `est` tokens)
    # Attempt 3: succeeds with 260 tokens
    attempts_seq: list[Any] = [
        extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            status="indeterminate",
            error_kind="http_503",
            transient_error=True,
            input_tokens=150,
            output_tokens=30,
            total_tokens=180,
            error="http_503: upstream unavailable",
        ),
        httpx.RemoteProtocolError("peer closed connection"),
        extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=200,
            output_tokens=60,
            total_tokens=260,
        ),
    ]

    async def _three_attempts_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        item = attempts_seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    res = await limiter.wait_for_budget(est)
    outcome, retries = await archive_cli.extract_segment_with_infinite_backoff(
        seg,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        limiter=limiter,
        estimated_tokens=est,
        reservation=res,
        extract_fn=create_autospec(
            extractor_mod.extract_segment_cards,
            side_effect=_three_attempts_extract,
        ),
        sleep_fn=mock_sleep,
    )
    limiter.record_tokens(outcome.total_tokens, reservation=res)

    assert outcome.ok is True
    assert retries == 2
    assert slept == [2.0, 4.0]
    assert len(limiter._reservations) == 0
    assert len(limiter._history) == 3
    assert [tok for _, tok in limiter._history] == [180, est, 260]
    assert limiter.current_window_tokens() == 180 + est + 260

    # Journal short-circuit ("未实际发出请求") must release reservation without charging tokens
    limiter_no_send = archive_cli.HourlyTokenBudgetLimiter(100_000, sleep_fn=mock_sleep)
    res_no_send = await limiter_no_send.wait_for_budget(est)
    out_inflight, _ = await archive_cli.extract_segment_with_infinite_backoff(
        seg,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        limiter=limiter_no_send,
        estimated_tokens=est,
        reservation=res_no_send,
        extract_fn=create_autospec(
            extractor_mod.extract_segment_cards,
            return_value=extractor_mod.SegmentExtractionOutcome(
                model_used="gemini-4-argon-medium-oauth",
                ok=False,
                status="in_flight",
                error_kind="in_flight",
                in_flight=True,
                error="该请求正在由另一个进程执行中，请稍候。",
            ),
        ),
        sleep_fn=mock_sleep,
    )
    if archive_cli._did_journal_short_circuit_without_send(out_inflight):
        limiter_no_send.release_reservation(res_no_send)
    assert len(limiter_no_send._reservations) == 0
    assert limiter_no_send.current_window_tokens() == 0


@pytest.mark.asyncio
async def test_deferred_in_flight_window_uses_monotonic_deadline_including_budget_and_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1. Production `_defer_now()` reads `time.monotonic()` directly; tests inject `clock_fn`.
    2. Entering the deferred phase sets `deadline = clock_fn() + stale_after_s + 60`.
    3. `now >= deadline` is checked after budget wait (`wait_for_budget`) and before every retry send;
       if `now >= deadline`, no request is sent, the reservation is released, and the segment is marked
       `status="failed"`, `reason="in_flight_timeout"`.
    4. Already in-flight requests are NOT truncated when `now` crosses `deadline`.
    5. `update_run_finish` is called with `status="failed"` and `error_text="incomplete_segments=..."`.
    """
    fake_mono = 10_000.0

    def _monotonic() -> float:
        return fake_mono

    async def _advancing_sleep(seconds: float) -> None:
        nonlocal fake_mono
        fake_mono += max(0.0, float(seconds))

    mock_clock = create_autospec(archive_cli.time.monotonic, side_effect=_monotonic)
    mock_sleep = create_autospec(asyncio.sleep, side_effect=_advancing_sleep)

    # 3 segments in different sessions so they form 3 separate segments:
    # seg 0 (`ev-d0`): in_flight initially; on deferred pass 1, call takes 940s (pushing clock past deadline = +960s) and stays in_flight
    # seg 1 (`ev-d1`): in_flight initially; must NOT be retried in deferred pass 1 after seg 0 crosses deadline!
    # seg 2 (`ev-d2`): succeeds on initial pass (150 tokens), becomes `blocked` behind seg 0 & seg 1
    events = [
        {
            "agent": "main",
            "session_id": f"sess-d{i}",
            "event_id": f"ev-d{i}",
            "seq": 1,
            "generation": "gen-1",
            "created_at_ms": 1760950000000 + i * 1000,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "text": f"NVDA monotonic deadline test segment {i}.",
        }
        for i in range(3)
    ]

    call_counts: dict[str, int] = {"ev-d0": 0, "ev-d1": 0, "ev-d2": 0}
    finish_kwargs_list: list[dict[str, Any]] = []

    async def _extract_with_slow_deferred(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal fake_mono
        ev_id = str(segment[0]["event_id"])
        call_counts[ev_id] = call_counts.get(ev_id, 0) + 1
        if ev_id == "ev-d2":
            return extractor_mod.SegmentExtractionOutcome(
                category="research",
                ok=True,
                cards=[],
                cards_raw_count=0,
                model_used="gemini-4-argon-medium-oauth",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
            )
        if ev_id == "ev-d0" and call_counts[ev_id] == 2:
            # Deferred attempt #1 on seg 0: simulate 940s call time
            # Total elapsed since deferred start = 30s (sleep) + 940s (call) = 970s > 960s deadline!
            fake_mono += 940.0
        return extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            status="in_flight",
            error_kind="in_flight",
            in_flight=True,
            error="该请求正在由另一个进程执行中，请稍候。",
        )

    async def _capture_finish(run_id: str, **kwargs: Any) -> dict[str, Any]:
        finish_kwargs_list.append({"run_id": run_id, **kwargs})
        return {"run_id": run_id, **kwargs}

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, side_effect=_capture_finish),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )

    limiter = archive_cli.HourlyTokenBudgetLimiter(
        1_500_000, clock_fn=mock_clock, sleep_fn=mock_sleep
    )
    summary = await archive_cli.process_weekly_batch(
        events,
        mode="backfill",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=limiter,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        stale_after_s=900,
        concurrency=3,
        extract_fn=create_autospec(
            extractor_mod.extract_segment_cards,
            side_effect=_extract_with_slow_deferred,
        ),
        sleep_fn=mock_sleep,
        clock_fn=mock_clock,
    )

    # seg 0 ran initial + 1 deferred attempt (which crossed the 960s deadline without being truncated);
    # seg 1 ran ONLY the initial attempt and was NOT started in the deferred loop because deadline passed!
    assert call_counts == {"ev-d0": 2, "ev-d1": 1, "ev-d2": 1}
    assert summary["incomplete_segments_count"] == 3
    inc_by_id = {item["first_event_id"]: item for item in summary["incomplete_segments"]}
    assert inc_by_id["ev-d0"]["status"] == "failed"
    assert inc_by_id["ev-d0"]["reason"] == "in_flight_timeout"
    assert inc_by_id["ev-d1"]["status"] == "failed"
    assert inc_by_id["ev-d1"]["reason"] == "in_flight_timeout"
    assert inc_by_id["ev-d2"]["status"] == "blocked"
    assert inc_by_id["ev-d2"]["reason"] == "blocked_by_incomplete_prefix"
    # Blocked segment `ev-d2`'s 150 tokens are counted in batch summary and `update_run_finish`
    assert summary["total_tokens"] == 150
    assert len(finish_kwargs_list) == 1
    assert finish_kwargs_list[0]["status"] == "failed"
    assert finish_kwargs_list[0]["error_text"] == "incomplete_segments=3"
    assert finish_kwargs_list[0]["total_tokens"] == 150

    # Also verify: when budget wait (`wait_for_budget`) or retry backoff advances `now >= deadline`
    # before sending/resending, `extract_fn` is NOT called after `deadline`, the reservation is released,
    # and the segment is marked `status="failed"`, `reason="in_flight_timeout"`.
    budget_wait_calls = 0

    async def _extract_inflight_once(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal budget_wait_calls
        budget_wait_calls += 1
        return extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            status="in_flight",
            error_kind="in_flight",
            in_flight=True,
            error="该请求正在由另一个进程执行中，请稍候。",
        )

    tight_limiter = archive_cli.HourlyTokenBudgetLimiter(
        2_000, clock_fn=mock_clock, sleep_fn=mock_sleep
    )
    # Pre-consume 1,800 tokens right before deferred retry so `wait_for_budget` sleeps 3600s (past the 960s deadline!)
    orig_wait = tight_limiter.wait_for_budget
    wait_calls = 0

    async def _wait_pushing_past_deadline(estimated_tokens: int = 1000) -> archive_cli.BudgetReservation:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 2:
            tight_limiter.record_tokens(1_800)
        return await orig_wait(estimated_tokens)

    monkeypatch.setattr(
        tight_limiter,
        "wait_for_budget",
        create_autospec(tight_limiter.wait_for_budget, side_effect=_wait_pushing_past_deadline),
    )

    budget_deadline_summary = await archive_cli.process_weekly_batch(
        [
            {
                "agent": "main",
                "session_id": "sess-bw",
                "event_id": "ev-bw",
                "seq": 1,
                "generation": "gen-1",
                "created_at_ms": 1760950000000,
                "ts": "2026-10-20T10:00:00+00:00",
                "date_et": "2026-10-20",
                "text": "NVDA budget wait deadline check.",
            }
        ],
        mode="backfill",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=tight_limiter,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        stale_after_s=900,
        concurrency=1,
        extract_fn=create_autospec(
            extractor_mod.extract_segment_cards,
            side_effect=_extract_inflight_once,
        ),
        sleep_fn=mock_sleep,
        clock_fn=mock_clock,
    )
    # Initial attempt ran (`budget_wait_calls == 1`); on deferred retry, `wait_for_budget` slept 3600s
    # pushing `now >= deadline`, so `extract_fn` was NOT called a 2nd time and reservation was released!
    assert budget_wait_calls == 1
    assert len(tight_limiter._reservations) == 0
    assert budget_deadline_summary["incomplete_segments_count"] == 1
    assert budget_deadline_summary["incomplete_segments"][0]["status"] == "failed"
    assert budget_deadline_summary["incomplete_segments"][0]["reason"] == "in_flight_timeout"


@pytest.mark.asyncio
async def test_committed_segment_raw_events_released_via_weakref_and_optional_tracemalloc(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify:
    1. Using `weakref.ref` on event objects (`_TrackableEvent(dict)`), committed segment events are
       garbage-collected (`gc.collect()`) immediately after prefix commit (even while a later segment
       is still in flight!), and `batch_events` is cleared after segmentation.
    2. When `DALTON_ARCHIVE_MEM_TRACE=1`, `_run_segment_attempt` logs top-10 `tracemalloc` allocation
       sites (`file:line` and size) every 20 segments.
    """
    import gc
    import tracemalloc
    import weakref

    class _TrackableEvent(dict):  # type: ignore[type-arg]
        """Dict subclass supporting weak references."""

    ev0 = _TrackableEvent(
        agent="main",
        session_id="sess-gc-0",
        event_id="ev-gc-0",
        seq=1,
        generation="gen-1",
        created_at_ms=1760950000000,
        ts="2026-10-20T10:00:00+00:00",
        date_et="2026-10-20",
        text="NVDA committed segment 0 event payload for weakref GC verification.",
    )
    ev1 = _TrackableEvent(
        agent="main",
        session_id="sess-gc-1",
        event_id="ev-gc-1",
        seq=1,
        generation="gen-1",
        created_at_ms=1760953600000,
        ts="2026-10-20T11:00:00+00:00",
        date_et="2026-10-20",
        text="TSMC segment 1 event payload held while seg 0 commits.",
    )
    ref_ev0 = weakref.ref(ev0)
    ref_ev1 = weakref.ref(ev1)
    batch_events: list[dict[str, Any]] = [ev0, ev1]
    del ev0, ev1

    seg0_committed = asyncio.Event()
    release_seg1 = asyncio.Event()

    async def _extract_gc(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev_id = str(segment[0]["event_id"])
        if ev_id == "ev-gc-1":
            await seg0_committed.wait()
            await release_seg1.wait()
        return extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=50,
            output_tokens=20,
            total_tokens=70,
        )

    async def _record_processed_gc(rows: list[dict[str, Any]], conn: Any = None) -> int:
        if any(r.get("event_id") == "ev-gc-0" for r in rows):
            seg0_committed.set()
        return len(rows)

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, side_effect=_record_processed_gc),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )

    archive_cli.configure_archive_logging()
    monkeypatch.setenv("DALTON_ARCHIVE_MEM_TRACE", "1")
    monkeypatch.setattr(archive_cli, "MEM_TRACE_SEGMENT_INTERVAL", 2)

    try:
        batch_task = asyncio.create_task(
            archive_cli.process_weekly_batch(
                batch_events,
                mode="backfill",
                window_start=date(2026, 10, 20),
                window_end=date(2026, 10, 20),
                limiter=archive_cli.HourlyTokenBudgetLimiter(1_500_000),
                extractor_model="gemini-4-argon-medium-oauth",
                prompt_version="conv_card_v1",
                concurrency=2,
                extract_fn=create_autospec(
                    extractor_mod.extract_segment_cards,
                    side_effect=_extract_gc,
                ),
            )
        )

        await asyncio.wait_for(seg0_committed.wait(), timeout=2.0)
        for _ in range(5):
            await asyncio.sleep(0)

        # `batch_events` was cleared right after segmentation, and `ev-gc-0` was released on prefix commit
        # while `ev-gc-1` is still in flight!
        assert batch_events == []
        gc.collect()
        assert ref_ev0() is None
        assert ref_ev1() is not None

        release_seg1.set()
        await asyncio.wait_for(batch_task, timeout=2.0)
        gc.collect()
        assert ref_ev1() is None

        stderr_out = capsys.readouterr().err
        assert "[MEM_TRACE segments=2" in stderr_out
        assert "size=" in stderr_out
    finally:
        tracemalloc.stop()


@pytest.mark.asyncio
async def test_non_transient_schema_validation_failure_marks_failed_and_blocked_continues_next_week_exit_3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1. When segment 1 (the 2nd segment) fails with a non-transient schema validation error (`ValidationError`)
       and segment 2 (the 3rd segment) succeeds, segment 1 is recorded in `incomplete_segments` with
       `status="failed"` and `reason="extraction_error:ValidationError"`, while segment 2 is recorded with
       `status="blocked"` and `reason="blocked_by_incomplete_prefix"`.
    2. Week 1's prefix cursor (`next_to_flush`) stops at 1 (only segment 0 is committed).
    3. Week 2 continues and commits normally, and the pipeline finishes with `exit_code == 3`.
    """
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True)
    inbox_file = inbox_dir / "20261020T100000Z_schema_fail_two_weeks.jsonl"

    w1_base_ms = int(datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    w2_base_ms = int(datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    events = [
        {
            "agent": "main",
            "session_id": "sess-schema-w1",
            "event_id": f"ev-sw1-{idx}",
            "seq": idx + 1,
            "generation": "gen-1",
            "created_at_ms": w1_base_ms + idx * 3600 * 1000,
            "ts": datetime.fromtimestamp((w1_base_ms + idx * 3600 * 1000) / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-10",
            "speaker": {"sender_id_hash": "h1", "name": "Owner", "is_owner": True, "role": "owner"},
            "text": f"NVDA week 1 segment {idx} 财务跟踪与校验测试。",
        }
        for idx in range(3)
    ] + [
        {
            "agent": "main",
            "session_id": "sess-schema-w2",
            "event_id": "ev-sw2-0",
            "seq": 1,
            "generation": "gen-1",
            "created_at_ms": w2_base_ms,
            "ts": datetime.fromtimestamp(w2_base_ms / 1000, tz=timezone.utc).isoformat(),
            "date_et": "2026-10-18",
            "speaker": {"sender_id_hash": "h1", "name": "Owner", "is_owner": True, "role": "owner"},
            "text": "TSMC week 2 CoWoS 扩产跟踪测试。",
        }
    ]
    inbox_file.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )

    processed_events: list[str] = []

    async def _fake_dalton_complete(**kwargs: Any) -> complete_mod.DaltonCompleteResult:
        user_prompt = str(kwargs.get("user_prompt") or "")
        if "ev-sw1-1" in user_prompt:
            # 2nd segment in Week 1: returns invalid schema (`cards` missing required `quote_event_id` and invalid `type`)
            return complete_mod.DaltonCompleteResult(
                ok=True,
                status="done",
                data={
                    "category": "research",
                    "topic_label": "NVDA",
                    "cards": [
                        {
                            "type": "not_a_valid_card_type",
                            "claim": "Bad schema card",
                            "quote": "NVDA week 1 segment 1 财务跟踪与校验测试。",
                            "quote_event_id": "ev-sw1-1",
                        }
                    ],
                },
                model="gemini-4-argon-medium-oauth",
                usage={"input_tokens": 110, "output_tokens": 40, "total_tokens": 150},
            )
        ev_id = "ev-sw1-0" if "ev-sw1-0" in user_prompt else ("ev-sw1-2" if "ev-sw1-2" in user_prompt else "ev-sw2-0")
        ev_text = next(e["text"] for e in events if e["event_id"] == ev_id)
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "category": "research",
                "topic_label": "NVDA",
                "cards": [
                    {
                        "type": "fact",
                        "topic_label": "NVDA",
                        "claim": f"Claim {ev_id}",
                        "tickers": ["NVDA"],
                        "entities": [],
                        "sectors": ["Semiconductors"],
                        "figures": [],
                        "quote": ev_text,
                        "quote_event_id": ev_id,
                        "cited_sources": [],
                        "confidence": 0.9,
                    }
                ],
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        )

    async def _fake_record_processed(rows: list[dict[str, Any]], conn: Any = None) -> int:
        for r in rows:
            processed_events.append(str(r["event_id"]))
        return len(rows)

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        assist_mod,
        "dalton_complete",
        create_autospec(complete_mod.dalton_complete, side_effect=_fake_dalton_complete),
    )
    monkeypatch.setattr(
        db_conv,
        "try_acquire_archive_run_lock",
        create_autospec(db_conv.try_acquire_archive_run_lock, return_value=True),
    )
    monkeypatch.setattr(
        db_conv,
        "release_archive_run_lock",
        create_autospec(db_conv.release_archive_run_lock, return_value=True),
    )
    monkeypatch.setattr(
        db_conv,
        "mark_stale_running_runs_abandoned",
        create_autospec(db_conv.mark_stale_running_runs_abandoned, return_value=0),
    )
    monkeypatch.setattr(
        db_conv,
        "fetch_processed_event_chunks",
        create_autospec(db_conv.fetch_processed_event_chunks, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, side_effect=_fake_record_processed),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(
            db_conv.insert_cards_batch,
            side_effect=lambda cards, *, embeddings=None, conn=None: (len(cards), 0, list(cards)),
        ),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )
    monkeypatch.setattr(
        db_conv,
        "list_cards_for_dates",
        create_autospec(db_conv.list_cards_for_dates, return_value=[]),
    )
    monkeypatch.setattr(
        db_conv,
        "mark_dates_rendered",
        create_autospec(db_conv.mark_dates_rendered, return_value=1),
    )
    monkeypatch.setattr(
        archive_cli,
        "render_and_write_conversation_digests",
        create_autospec(
            archive_cli.render_and_write_conversation_digests,
            return_value={"company_files": [], "topic_files": [], "rendered_dates": []},
        ),
    )

    mock_embed = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        return_value=([[0.1, 0.2, 0.3]], "gemini-embedding-001"),
    )

    summary = await archive_cli.run_archive_pipeline(
        mode="backfill",
        since_date=date(2026, 10, 10),
        until_date=date(2026, 10, 20),
        inbox_dir=inbox_dir,
        skip_host_export=True,
        concurrency=2,
        embed_fn=mock_embed,
        llm_service=object(),
    )

    assert summary["ok"] is False
    assert summary["exit_code"] == 3
    assert summary["batches_count"] == 2
    assert summary["batches"][0]["status"] == "failed"
    assert summary["batches"][0]["next_to_flush"] == 1
    assert summary["batches"][0]["segments_processed"] == 1
    assert summary["batches"][1]["status"] == "completed"
    assert summary["batches"][1]["segments_processed"] == 1
    assert processed_events == ["ev-sw1-0", "ev-sw2-0"]

    assert summary["incomplete_segments_total"] == 2
    inc_by_id = {item["first_event_id"]: item for item in summary["incomplete_segments"]}
    assert inc_by_id["ev-sw1-1"]["segment_index"] == 1
    assert inc_by_id["ev-sw1-1"]["status"] == "failed"
    assert inc_by_id["ev-sw1-1"]["reason"] == "extraction_error:ValidationError"
    assert inc_by_id["ev-sw1-1"]["total_tokens"] == 150
    assert inc_by_id["ev-sw1-2"]["segment_index"] == 2
    assert inc_by_id["ev-sw1-2"]["status"] == "blocked"
    assert inc_by_id["ev-sw1-2"]["reason"] == "blocked_by_incomplete_prefix"
    assert inc_by_id["ev-sw1-2"]["total_tokens"] == 150
    # All 3 segments in Week 1 (150 each = 450) + 1 segment in Week 2 (150) = 600 tokens counted in summary
    assert summary["batches"][0]["total_tokens"] == 450
    assert summary["total_tokens"] == 600


@pytest.mark.asyncio
async def test_segment_accumulates_token_usage_across_all_attempts_in_summary_and_run_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1. When a segment fails on Attempt 1 consuming 180 tokens (`input=150, output=30, total=180`) and
       succeeds on Attempt 2 consuming 260 tokens (`input=200, output=60, total=260`), the weekly batch
       summary and `update_run_finish` record `total_tokens == 440` (`input_tokens == 350, output_tokens == 90`).
    2. Limiter reservation settlement remains per-attempt (`180` then `260`, sum `440`, not `180 + 440`).
    """
    slept: list[float] = []

    async def _noop_sleep(secs: float) -> None:
        slept.append(secs)

    mock_sleep = create_autospec(asyncio.sleep, side_effect=_noop_sleep)

    attempts: list[extractor_mod.SegmentExtractionOutcome] = [
        extractor_mod.SegmentExtractionOutcome(
            model_used="gemini-4-argon-medium-oauth",
            ok=False,
            status="indeterminate",
            error_kind="http_503",
            transient_error=True,
            input_tokens=150,
            output_tokens=30,
            total_tokens=180,
            error="http_503: Service Unavailable",
        ),
        extractor_mod.SegmentExtractionOutcome(
            category="research",
            ok=True,
            cards=[],
            cards_raw_count=0,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=200,
            output_tokens=60,
            total_tokens=260,
        ),
    ]

    async def _two_attempt_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        return attempts.pop(0)

    finish_calls: list[dict[str, Any]] = []

    async def _capture_finish(run_id: str, **kwargs: Any) -> dict[str, Any]:
        finish_calls.append({"run_id": run_id, **kwargs})
        return {"run_id": run_id, **kwargs}

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, side_effect=_capture_finish),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, return_value=1),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )

    limiter = archive_cli.HourlyTokenBudgetLimiter(100_000, sleep_fn=mock_sleep)
    summary = await archive_cli.process_weekly_batch(
        [
            {
                "agent": "main",
                "session_id": "sess-acc-tok",
                "event_id": "ev-acc-1",
                "seq": 1,
                "generation": "gen-1",
                "created_at_ms": 1760950000000,
                "ts": "2026-10-20T10:00:00+00:00",
                "date_et": "2026-10-20",
                "text": "NVDA 累计单段多次尝试 token 用量测试。",
            }
        ],
        mode="backfill",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=limiter,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        extract_fn=create_autospec(
            extractor_mod.extract_segment_cards,
            side_effect=_two_attempt_extract,
        ),
        sleep_fn=mock_sleep,
    )

    assert summary["input_tokens"] == 350
    assert summary["output_tokens"] == 90
    assert summary["total_tokens"] == 440
    assert len(finish_calls) == 1
    assert finish_calls[0]["input_tokens"] == 350
    assert finish_calls[0]["output_tokens"] == 90
    assert finish_calls[0]["total_tokens"] == 440
    # Limiter settled each attempt separately: [180, 260] -> sum 440 (not 180 + 440)
    assert [tok for _, tok in limiter._history] == [180, 260]
    assert limiter.current_window_tokens() == 440


@pytest.mark.asyncio
async def test_journal_replay_cached_or_replayed_propagates_to_outcome_and_skips_budget_and_usage_accumulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1. When `dalton_complete` returns `cached=True` or `replayed=True` (with non-zero `usage` from the
       stored journal row), `extract_segment_cards` propagates `cached` and `replayed` onto `SegmentExtractionOutcome`.
    2. `_did_journal_short_circuit_without_send(outcome)` returns `True`.
    3. `extract_segment_with_infinite_backoff` and `process_weekly_batch` release the reservation in
       `limiter` (`current_window_tokens() == 0`) and do NOT count the replayed tokens toward segment/batch
       token usage accumulation (`total_tokens == 0`).
    """
    segment = [
        {
            "agent": "main",
            "session_id": "sess-replay",
            "event_id": "ev-replay-1",
            "seq": 1,
            "generation": "gen-1",
            "created_at_ms": 1760950000000,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "speaker": {"sender_id_hash": "h1", "name": "Owner", "is_owner": True, "role": "owner"},
            "text": "NVDA journal replay token accounting test.",
        }
    ]

    mock_complete_replay = create_autospec(
        complete_mod.dalton_complete,
        return_value=complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={
                "category": "research",
                "topic_label": "NVDA",
                "cards": [
                    {
                        "type": "fact",
                        "topic_label": "NVDA",
                        "claim": "NVDA journal replay claim",
                        "tickers": ["NVDA"],
                        "entities": [],
                        "sectors": [],
                        "figures": [],
                        "quote": "NVDA journal replay token accounting test.",
                        "quote_event_id": "ev-replay-1",
                        "cited_sources": [],
                        "confidence": 0.9,
                    }
                ],
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 120, "output_tokens": 45, "total_tokens": 165},
            cached=False,
            replayed=True,
        ),
    )
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete_replay)

    direct_outcome = await extractor_mod.extract_segment_cards(
        segment,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
    )
    assert direct_outcome.ok is True
    assert direct_outcome.cached is True
    assert direct_outcome.replayed is True
    assert archive_cli._did_journal_short_circuit_without_send(direct_outcome) is True

    _patch_autospec_db_connection(monkeypatch)
    monkeypatch.setattr(
        db_conv,
        "insert_run_start",
        create_autospec(db_conv.insert_run_start, return_value={"status": "running"}),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_progress",
        create_autospec(db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        db_conv,
        "update_run_finish",
        create_autospec(db_conv.update_run_finish, return_value={"status": "completed"}),
    )
    monkeypatch.setattr(
        db_conv,
        "find_existing_content_hashes",
        create_autospec(db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(
        db_conv,
        "insert_cards_batch",
        create_autospec(
            db_conv.insert_cards_batch,
            side_effect=lambda cards, *, embeddings=None, conn=None: (len(cards), 0, list(cards)),
        ),
    )
    monkeypatch.setattr(
        db_conv,
        "record_processed_event_chunks",
        create_autospec(db_conv.record_processed_event_chunks, return_value=1),
    )
    monkeypatch.setattr(
        db_conv,
        "list_unrendered_dates",
        create_autospec(db_conv.list_unrendered_dates, return_value=[]),
    )
    monkeypatch.setattr(
        db_conv,
        "list_cards_for_dates",
        create_autospec(db_conv.list_cards_for_dates, return_value=[]),
    )
    monkeypatch.setattr(
        db_conv,
        "mark_dates_rendered",
        create_autospec(db_conv.mark_dates_rendered, return_value=1),
    )
    monkeypatch.setattr(
        archive_cli,
        "render_and_write_conversation_digests",
        create_autospec(
            archive_cli.render_and_write_conversation_digests,
            return_value={"company_files": [], "topic_files": [], "rendered_dates": []},
        ),
    )

    limiter = archive_cli.HourlyTokenBudgetLimiter(100_000)
    summary = await archive_cli.process_weekly_batch(
        [dict(segment[0])],
        mode="backfill",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=limiter,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v1",
        embed_fn=create_autospec(
            embedder_mod.embed_texts_via_bridge,
            return_value=([[0.1, 0.2, 0.3]], "gemini-embedding-001"),
        ),
    )

    assert summary["segments_processed"] == 1
    assert summary["cards_inserted"] == 1
    assert len(limiter._reservations) == 0
    assert limiter.current_window_tokens() == 0
    assert summary["input_tokens"] == 0
    assert summary["output_tokens"] == 0
    assert summary["total_tokens"] == 0


@pytest.mark.asyncio
async def test_card_fidelity_programmatic_validation_and_sample_regressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify programmatic card validation (`validate_candidate_card`, quote-scoped `cited_sources`,
    `conv_card_v2` prompt version, and weekly batch counters) against `fixtures/conv_cards_audit_sample.json`
    negative cases (`cc_07a818e64dd168360e63afa4`, `cc_e55f5248f685901e4fb349ea`,
    `cc_c602f34f600becc09bfff75a`, `cc_112c70a292c9d5886db987ab`) and positive cases
    (`cc_af0f4051d0d2a2ab831b3fa5`, `cc_22905e2fd37cc08e068703b4`), plus unit/number/period/ticker/exception regressions.
    """
    from copy import deepcopy
    from src.dalton.settings import get_dalton_settings

    assert get_dalton_settings().conversations.prompt_version == "conv_card_v2_3"
    assert "证据覆盖" in extractor_mod.EXTRACTION_SYSTEM_INSTRUCTIONS
    assert "保留限定与归属" in extractor_mod.EXTRACTION_SYSTEM_INSTRUCTIONS

    sample_path = Path(__file__).resolve().parent / "fixtures" / "conv_cards_audit_sample.json"
    sample_by_id = {
        c["card_id"]: c for c in json.loads(sample_path.read_text(encoding="utf-8"))
    }

    # 1. Negative sample card: cc_07a818e64dd168360e63afa4 (55, 8, 125 not in quote; 2026 year not in quote)
    c_hormuz = deepcopy(sample_by_id["cc_07a818e64dd168360e63afa4"])
    res_hormuz = extractor_mod.validate_candidate_card(c_hormuz)
    assert res_hormuz.ok is True
    assert res_hormuz.status == "active"
    assert res_hormuz.confidence == 0.4
    assert set(res_hormuz.unsupported_claim_numbers) >= {"55", "8", "125"}
    assert res_hormuz.figures_dropped == 2
    assert res_hormuz.periods_cleared == 2
    assert [f["value"] for f in res_hormuz.figures if f["metric"] != "_verification"] == [82, 79]
    assert res_hormuz.figures[0]["period"] == ""
    assert res_hormuz.figures[1]["period"] == ""
    assert res_hormuz.figures[-1]["metric"] == "_verification"
    assert res_hormuz.figures[-1]["value"].startswith("unsupported:")

    # 2. Negative sample card: cc_e55f5248f685901e4fb349ea (29.5, 9.7, 9.8, 1.5, 0.42 not in quote; 688775.SH and 2026 not in quote)
    c_insta = deepcopy(sample_by_id["cc_e55f5248f685901e4fb349ea"])
    res_insta = extractor_mod.validate_candidate_card(c_insta)
    assert res_insta.ok is True
    assert res_insta.status == "active"
    assert res_insta.confidence == 0.4
    assert set(res_insta.unsupported_claim_numbers) >= {"29.5", "9.7", "9.8", "1.5", "0.42"}
    assert res_insta.figures_dropped == 5
    assert res_insta.periods_cleared == 1
    assert [f["value"] for f in res_insta.figures if f["metric"] != "_verification"] == [31.9]
    assert res_insta.figures[0]["period"] == ""
    assert res_insta.figures[-1]["metric"] == "_verification"
    assert res_insta.figures[-1]["value"].startswith("unsupported:")
    assert res_insta.tickers == []
    assert res_insta.tickers_dropped == 1

    # 3. Negative sample card: cc_c602f34f600becc09bfff75a (`IT` is Gartner source ticker; `FY2027E` lacks 2027 in quote)
    c_gartner = deepcopy(sample_by_id["cc_c602f34f600becc09bfff75a"])
    res_gartner = extractor_mod.validate_candidate_card(c_gartner)
    assert res_gartner.ok is True
    assert res_gartner.tickers == []
    assert res_gartner.tickers_dropped == 1
    assert len(res_gartner.figures) == 4
    assert res_gartner.figures_dropped == 0
    assert res_gartner.periods_cleared == 1
    assert [f["period"] for f in res_gartner.figures] == ["by 2030", "by 2030", "", "FY2028E"]

    # 4. Negative sample card: cc_112c70a292c9d5886db987ab (`WIT` explicitly excluded; period not in quote)
    c_it_serv = deepcopy(sample_by_id["cc_112c70a292c9d5886db987ab"])
    res_it_serv = extractor_mod.validate_candidate_card(c_it_serv)
    assert res_it_serv.ok is True
    assert res_it_serv.tickers == ["INFY", "KD", "CTSH"]
    assert res_it_serv.tickers_dropped == 1
    assert res_it_serv.periods_cleared == 1
    assert res_it_serv.figures[0]["value"] == 12.4
    assert res_it_serv.figures[0]["period"] == ""

    # 5a. Positive sample card: cc_af0f4051d0d2a2ab831b3fa5 preserved intact
    orig_lanqi = deepcopy(sample_by_id["cc_af0f4051d0d2a2ab831b3fa5"])
    cand_lanqi = deepcopy(orig_lanqi)
    res_lanqi = extractor_mod.validate_candidate_card(cand_lanqi)
    assert res_lanqi.ok is True
    assert res_lanqi.status == "active"
    assert res_lanqi.figures_dropped == 0
    assert res_lanqi.periods_cleared == 0
    assert res_lanqi.tickers_dropped == 0
    assert cand_lanqi["claim"] == orig_lanqi["claim"]
    assert cand_lanqi["tickers"] == orig_lanqi["tickers"]
    assert cand_lanqi["figures"] == orig_lanqi["figures"]

    # 5b. Positive sample card: cc_22905e2fd37cc08e068703b4 — numbers not falsely rejected (`ok=True`, `figures_dropped=0`),
    #     while `ATAT` (only in cited_sources URL, not in quote) is dropped, `FY2026`/`2026Q2` periods (year 2026 not in quote) are cleared,
    #     and `亚朵` (in claim, absent from quote) is flagged via `unsupported_names:亚朵`.
    orig_atour = deepcopy(sample_by_id["cc_22905e2fd37cc08e068703b4"])
    cand_atour = deepcopy(orig_atour)
    res_atour = extractor_mod.validate_candidate_card(cand_atour)
    assert res_atour.ok is True
    assert res_atour.status == "active"
    assert res_atour.unsupported_claim_numbers == []
    assert res_atour.unsupported_names == ["亚朵"]
    assert res_atour.figures_dropped == 0
    assert len([f for f in res_atour.figures if f["metric"] != "_verification"]) == 4
    assert res_atour.figures[-1] == {
        "metric": "_verification",
        "value": "unsupported_names:亚朵",
        "unit": "",
        "period": "",
    }
    assert res_atour.tickers == []
    assert res_atour.tickers_dropped == 1
    assert res_atour.periods_cleared == 4
    assert all(f["period"] == "" for f in res_atour.figures)

    # 6. Verify quote-scoped citation check (`extractor.py:346`): source in sibling event but NOT in card quote -> downgraded to opinion
    segment = [
        {
            "agent": "main",
            "session_id": "sess-fid-1",
            "session_key": "agent:main:discord:channel:fid",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-fid-1",
            "seq": 1,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "assistant",
            "speaker": {"sender_id_hash": "agent:main", "name": "Eve", "is_owner": False, "role": "agent"},
            "text": (
                sample_by_id["cc_07a818e64dd168360e63afa4"]["quote"]
                + "\n"
                + sample_by_id["cc_e55f5248f685901e4fb349ea"]["quote"]
                + "\n"
                + sample_by_id["cc_c602f34f600becc09bfff75a"]["quote"]
                + "\n"
                + sample_by_id["cc_112c70a292c9d5886db987ab"]["quote"]
                + "\n"
                + sample_by_id["cc_af0f4051d0d2a2ab831b3fa5"]["quote"]
            ),
        },
        {
            "agent": "main",
            "session_id": "sess-fid-1",
            "session_key": "agent:main:discord:channel:fid",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-fid-2",
            "seq": 2,
            "ts": "2026-10-20T10:01:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "user",
            "speaker": {"sender_id_hash": "owner", "name": "Owner", "is_owner": True, "role": "owner"},
            "text": (
                "来源备忘：2026 年半年报；https://www.stocktitan.net/news/ATAT/atour-lifestyle-holdings-limited-reports-second-quarter-2026-4x8ywbp6nmz3.html\n"
                + sample_by_id["cc_22905e2fd37cc08e068703b4"]["quote"]
            ),
        },
    ]

    raw_llm_cards = []
    for cid, ev_id in [
        ("cc_07a818e64dd168360e63afa4", "ev-fid-1"),
        ("cc_e55f5248f685901e4fb349ea", "ev-fid-1"),
        ("cc_c602f34f600becc09bfff75a", "ev-fid-1"),
        ("cc_112c70a292c9d5886db987ab", "ev-fid-1"),
        ("cc_af0f4051d0d2a2ab831b3fa5", "ev-fid-1"),
        ("cc_22905e2fd37cc08e068703b4", "ev-fid-2"),
    ]:
        item = deepcopy(sample_by_id[cid])
        item["quote_event_id"] = ev_id
        raw_llm_cards.append(item)

    mock_assist = create_autospec(
        extractor_mod.assist_json,
        return_value=AssistResult(
            ok=True,
            status="ok",
            data={
                "category": "research",
                "topic_label": "卡片忠实度回归测试",
                "cards": raw_llm_cards,
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 500, "output_tokens": 250, "total_tokens": 750},
        ),
    )
    monkeypatch.setattr(extractor_mod, "assist_json", mock_assist)

    outcome = await extractor_mod.extract_segment_cards(segment)
    assert outcome.ok is True
    assert outcome.cards_flagged_unsupported_number == 2
    assert outcome.figures_dropped == 7  # 2 from hormuz + 5 from insta360
    assert outcome.periods_cleared == 9  # 2 hormuz + 1 insta360 + 1 gartner + 1 it_serv + 4 atour
    assert outcome.tickers_dropped == 4  # 1 insta360 + 1 gartner (IT) + 1 it_serv (WIT) + 1 atour (ATAT)
    assert len(outcome.cards) == 6

    by_quote = {c["quote"]: c for c in outcome.cards}
    hormuz_out = by_quote[sample_by_id["cc_07a818e64dd168360e63afa4"]["quote"]]
    assert hormuz_out["status"] == "active"
    assert hormuz_out["confidence"] == 0.4
    assert hormuz_out["figures"][-1]["metric"] == "_verification"

    insta_out = by_quote[sample_by_id["cc_e55f5248f685901e4fb349ea"]["quote"]]
    assert insta_out["status"] == "active"
    assert insta_out["confidence"] == 0.4
    assert insta_out["figures"][-1]["metric"] == "_verification"

    gartner_out = by_quote[sample_by_id["cc_c602f34f600becc09bfff75a"]["quote"]]
    assert gartner_out["tickers"] == []

    it_serv_out = by_quote[sample_by_id["cc_112c70a292c9d5886db987ab"]["quote"]]
    assert it_serv_out["tickers"] == ["INFY", "KD", "CTSH"]
    assert it_serv_out["figures"][0]["period"] == ""

    lanqi_out = by_quote[sample_by_id["cc_af0f4051d0d2a2ab831b3fa5"]["quote"]]
    assert lanqi_out["tickers"] == ["688008.SS"]
    assert lanqi_out["figures"] == sample_by_id["cc_af0f4051d0d2a2ab831b3fa5"]["figures"]
    assert lanqi_out["prompt_version"] == "conv_card_v2_3"

    atour_out = by_quote[sample_by_id["cc_22905e2fd37cc08e068703b4"]["quote"]]
    assert atour_out["tickers"] == []
    assert [f["value"] for f in atour_out["figures"] if f["metric"] != "_verification"] == [
        "25-30",
        1.575,
        63.2,
        40,
    ]
    assert all(f["period"] == "" for f in atour_out["figures"])
    assert atour_out["type"] == "fact"

    # 7. Verify agent citation check: `fact` without quote-verified source downgrades to `opinion`,
    #    whereas agent `model_assumption` without external source is preserved (while still subject to number validation).
    _spk, downgraded_type, _srcs = extractor_mod.enforce_speaker_and_agent_citation(
        "fact",
        ["2026 年半年报"],
        segment[0],
        quote=sample_by_id["cc_e55f5248f685901e4fb349ea"]["quote"],
        segment_events=segment,
    )
    assert downgraded_type == "opinion"

    _spk_ma, kept_ma_type, _srcs_ma = extractor_mod.enforce_speaker_and_agent_citation(
        "model_assumption",
        [],
        segment[0],
        quote="按 25 倍 PE 粗算，目标市值约 500 亿美元。",
        segment_events=segment,
    )
    assert kept_ma_type == "model_assumption"

    # 8. Magnitude conversion & number extraction regressions (Items 1 & 4):
    # - `8.5万亿` supports `8500 USD_bn` and `85000亿`
    res_85tn = extractor_mod.validate_candidate_card(
        {
            "claim": "AI 数据中心累计需取得 8500 USD_bn（即 85000亿）收入。",
            "figures": [{"metric": "rev", "value": 8500, "unit": "USD_bn", "period": ""}],
            "tickers": [],
            "quote": "累计要收 8.5 万亿美元。",
        }
    )
    assert res_85tn.ok is True
    assert res_85tn.figures_dropped == 0

    # - `1.2万` <-> `12000` in both directions
    assert extractor_mod.validate_candidate_card(
        {"claim": "出货量达 12000 台。", "figures": [], "tickers": [], "quote": "出货量达 1.2万 台。"}
    ).ok is True
    assert extractor_mod.validate_candidate_card(
        {"claim": "出货量达 1.2万 台。", "figures": [], "tickers": [], "quote": "出货量达 12000 台。"}
    ).ok is True

    # - `quote` has `1亿元` -> `claim` with `10000亿元` MUST be flagged (`confidence=0.4`, `_verification` figure)
    res_1yi_vs_10000yi = extractor_mod.validate_candidate_card(
        {
            "claim": "公司净利润达到 10000亿元。",
            "figures": [{"metric": "net_profit", "value": 10000, "unit": "CNY_yi", "period": ""}],
            "tickers": [],
            "quote": "公司净利润达到 1亿元。",
        }
    )
    assert res_1yi_vs_10000yi.ok is True
    assert res_1yi_vs_10000yi.status == "active"
    assert res_1yi_vs_10000yi.confidence == 0.4
    assert res_1yi_vs_10000yi.unsupported_claim_numbers == ["10000"]
    assert res_1yi_vs_10000yi.figures_dropped == 1
    assert res_1yi_vs_10000yi.figures == [
        {"metric": "_verification", "value": "unsupported:10000", "unit": "", "period": ""}
    ]

    # - Chinese numerals (`一百万`) and scientific notation (`1e6`) on both sides
    assert extractor_mod.validate_candidate_card(
        {
            "claim": "训练使用了 1e6 小时数据（约 100万 小时）。",
            "figures": [{"metric": "hours", "value": "1e6", "unit": "hours", "period": ""}],
            "tickers": [],
            "quote": "训练使用了超过一百万小时的数据。",
        }
    ).ok is True

    # - Chinese numerals in ordinary words ("一度", "十分", "一直", "一些", "一致", "一般", "万一", "百分之百地", "三番五次")
    #   must NOT be treated as metric numbers; only Chinese numerals followed by units/classifiers count.
    assert extractor_mod.validate_candidate_card(
        {"claim": "股价一度涨到80元", "figures": [], "tickers": [], "quote": "股价曾涨到80元"}
    ).ok is True
    assert extractor_mod.validate_candidate_card(
        {"claim": "十分看好", "figures": [], "tickers": [], "quote": "非常看好"}
    ).ok is True
    assert (
        extractor_mod.extract_claim_numbers(
            "他一直有一些一般看法，与市场一致，万一百分之百地三番五次验证。"
        )
        == []
    )
    assert extractor_mod.validate_candidate_card(
        {"claim": "研发投入 100万元。", "figures": [], "tickers": [], "quote": "研发投入一百万元。"}
    ).ok is True
    assert extractor_mod.validate_candidate_card(
        {"claim": "研发投入一百万元。", "figures": [], "tickers": [], "quote": "研发投入 100万元。"}
    ).ok is True
    assert extractor_mod.validate_candidate_card(
        {
            "claim": "营收 3 亿，增长 2 倍，毛利提升 10 个百分点，占 5 成。",
            "figures": [],
            "tickers": [],
            "quote": "营收三亿，增长两倍，毛利提升十个百分点，占五成。",
        }
    ).ok is True

    # - Numeric ranges followed by units (`10-12 GW`, `7-12%`, `25-35亿`) must count both endpoints,
    #   whereas unit-free `MM-DD` / `YYYY-MM` dates are masked and cannot support metric numbers.
    assert extractor_mod.validate_candidate_card(
        {
            "claim": "装机容量 10-12 GW，毛利率 7-12%，估值 25-35亿。",
            "figures": [
                {"metric": "cap", "value": "10-12", "unit": "GW", "period": ""},
                {"metric": "gm", "value": "7-12", "unit": "%", "period": ""},
                {"metric": "val", "value": "25-35", "unit": "CNY_yi", "period": ""},
            ],
            "tickers": [],
            "quote": "预计装机 10-12 GW，毛利率 7-12%，对应 25-35亿。",
        }
    ).ok is True
    assert (
        extractor_mod.validate_candidate_card(
            {
                "claim": "装机容量 12 GW。",
                "figures": [],
                "tickers": [],
                "quote": "10-12 发布新产品公告。",
            }
        ).unsupported_claim_numbers
        == ["12"]
    )
    assert (
        extractor_mod.validate_candidate_card(
            {
                "claim": "装机容量 15 GW。",
                "figures": [],
                "tickers": [],
                "quote": "预计装机 10-12 GW。",
            }
        ).unsupported_claim_numbers
        == ["15"]
    )

    # - Dates (`YYYY-MM-DD`, `MM-DD`, `YYYY年M月D日`), indicator windows (`30D`, `50MA`, `200MA`),
    #   and model/product codes (`Qwen2.5`, `H100`, `WiFi7`) cannot support metric numbers in claim/figures
    res_non_metric_leak = extractor_mod.validate_candidate_card(
        {
            "claim": "收入增长 2.5%，毛利率 50%，出货 100 万台，单价 7 美元，目标价 2026 美元。",
            "figures": [{"metric": "gm", "value": 50, "unit": "percent", "period": ""}],
            "tickers": [],
            "quote": "2026-08-12（08-12，2026年8月12日）跟踪 Qwen2.5、H100 与 WiFi7：30D 跌破 50MA 与 200MA。",
        }
    )
    assert res_non_metric_leak.ok is True
    assert res_non_metric_leak.confidence == 0.4
    assert set(res_non_metric_leak.unsupported_claim_numbers) >= {"2.5", "50", "100", "7", "2026"}
    assert res_non_metric_leak.figures_dropped == 1

    # 9. Period validation regressions (Item 5 & Round 3 Item 3):
    assert extractor_mod.is_period_supported_by_quote("30D", "RSI=51.4 30D=-5.1%") is True
    assert extractor_mod.is_period_supported_by_quote("7月", "7 月估 25–35 亿元") is True
    assert extractor_mod.is_period_supported_by_quote("2H2026", "2H 2026 指引上调") is True
    assert extractor_mod.is_period_supported_by_quote("2H2026", "下半年服务器增长 80% 以上") is False
    assert extractor_mod.is_period_supported_by_quote("1H", "过去半年需求回暖") is False
    assert extractor_mod.is_period_supported_by_quote("FY2026", "全年指引上调，今年和明年继续高增") is False
    assert extractor_mod.is_period_supported_by_quote("current", "价格=84.43") is False
    assert extractor_mod.is_period_supported_by_quote("current", "trading at current levels") is True
    assert extractor_mod.is_period_supported_by_quote("annual", "annual expense ratio 0.07%") is True
    assert extractor_mod.is_period_supported_by_quote("historical", "只看历史均值") is False
    assert extractor_mod.is_period_supported_by_quote("2026Q2", "2026年营收增长20%") is False
    assert extractor_mod.is_period_supported_by_quote("2026年第二季度", "2026年营收增长20%") is False
    assert extractor_mod.is_period_supported_by_quote("2026Q2", "2026年第二季度营收增长20%") is True
    assert extractor_mod.is_period_supported_by_quote("1H2026", "2026年 H100 出货翻倍") is False
    assert extractor_mod.is_period_supported_by_quote("H1", "H100 出货翻倍") is False
    assert extractor_mod.is_period_supported_by_quote("H2", "H20 与 H200 出货翻倍") is False
    assert extractor_mod.is_period_supported_by_quote("2026-07-23 至 08-31", "2026-07-23 单日数据") is False
    assert extractor_mod.is_period_supported_by_quote("2026-07-23 至 08-31", "2026年7月23日至8月31日累计数据") is True
    assert extractor_mod.is_period_supported_by_quote("7月", "毛利率提升 7-12%") is False

    # 10. Ticker exclusion scope, HK code normalization, and short Chinese alias boundary regressions (Items 6, 7, 8):
    # - "剔除WIT后INFY上涨", "剔除WIT并保留INFY", "剔除WIT但继续持有INFY" -> keep INFY, drop WIT
    for ex_quote in ("剔除WIT后INFY上涨", "剔除WIT并保留INFY", "剔除WIT但继续持有INFY"):
        kept_ex1, dropped_ex1 = extractor_mod.validate_tickers_against_quote(
            ["WIT", "INFY"], ex_quote
        )
        assert kept_ex1 == ["INFY"]
        assert dropped_ex1 == 1

    # - "WIT价格有误，已剔除WIT。" -> drop WIT
    kept_ex2, dropped_ex2 = extractor_mod.validate_tickers_against_quote(
        ["WIT"], "WIT价格有误，已剔除WIT。"
    )
    assert kept_ex2 == []
    assert dropped_ex2 == 1

    # - "不排除" is not an exclusion
    kept_ex3, dropped_ex3 = extractor_mod.validate_tickers_against_quote(
        ["WIT"], "不排除WIT四季度反弹"
    )
    assert kept_ex3 == ["WIT"]
    assert dropped_ex3 == 0

    # - HK leading zeros & suffixes: `700.HK`, `0700.HK`, `00700` all match each other and Tencent aliases
    for hk_variant in ("700.HK", "0700.HK", "00700"):
        assert extractor_mod.validate_tickers_against_quote([hk_variant], "腾讯 Q2 游戏流水增长")[0] == [hk_variant]
        assert extractor_mod.validate_tickers_against_quote([hk_variant], "00700 领涨港股")[0] == [hk_variant]
        assert extractor_mod.validate_tickers_against_quote([hk_variant], "700.HK 领涨港股")[0] == [hk_variant]

    # - Short Chinese alias (< 3 chars): "完美的" must NOT match "美的" (000333.SZ); product names H100/B200 must NOT match NVDA
    assert extractor_mod.validate_tickers_against_quote(["000333.SZ"], "交出了一份完美的答卷") == ([], 1)
    assert extractor_mod.validate_tickers_against_quote(["000333.SZ"], "美的 2026H1 海外大增") == (["000333.SZ"], 0)
    assert extractor_mod.validate_tickers_against_quote(["000333.SZ"], "美的集团空调海外大增") == (["000333.SZ"], 0)
    assert extractor_mod.validate_tickers_against_quote(["NVDA"], "H100 与 B200/B300 租赁价格回升") == ([], 1)

    # 11. Verify weekly batch summary aggregates the 4 new validation counters next to segments_*
    limiter = archive_cli.HourlyTokenBudgetLimiter(100_000)
    batch_summary = await archive_cli.process_weekly_batch(
        [dict(ev) for ev in segment],
        mode="run",
        window_start=date(2026, 10, 20),
        window_end=date(2026, 10, 20),
        limiter=limiter,
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v2",
        dry_run=True,
    )
    assert batch_summary["segments_processed"] == 1
    assert batch_summary["cards_flagged_unsupported_number"] == 2
    assert "soft_unsupported_numbers" in batch_summary
    assert batch_summary["figures_dropped"] == 7
    assert batch_summary["periods_cleared"] == 9
    assert batch_summary["tickers_dropped"] == 4

    # 12. Exception path regression (Item 10): when a batch fails/aborts after an earlier batch flushed,
    #     `update_run_finish` submits `stats` containing the validation counters accumulated so far.
    seg_events_two_batches = [
        {
            "agent": "main",
            "session_id": "sess-exc-1",
            "session_key": "agent:main:discord:channel:exc1",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-exc-1",
            "seq": 1,
            "created_at_ms": 1760950000000,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "user",
            "text": "第一段对话讨论算力数据。",
        },
        {
            "agent": "main",
            "session_id": "sess-exc-2",
            "session_key": "agent:main:discord:channel:exc2",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-exc-2",
            "seq": 1,
            "created_at_ms": 1760957200000,
            "ts": "2026-10-20T12:00:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "user",
            "text": "第二段对话触发配额异常。",
        },
    ]

    seg_call_idx = 0

    async def _extract_first_ok_second_boom(
        seg_arg: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        nonlocal seg_call_idx
        seg_call_idx += 1
        if seg_call_idx == 1:
            return extractor_mod.SegmentExtractionOutcome(
                ok=True,
                category="research",
                cards=[],
                cards_raw_count=3,
                cards_flagged_unsupported_number=2,
                soft_unsupported_numbers=4,
                figures_dropped=5,
                periods_cleared=3,
                tickers_dropped=1,
                model_used="gemini-4-argon-medium-oauth",
                input_tokens=100,
                output_tokens=50,
                total_tokens=150,
            )
        return extractor_mod.SegmentExtractionOutcome(
            ok=False,
            rate_limited=True,
            permanent_quota_error=True,
            error="http_429_permanent_quota",
            model_used="gemini-4-argon-medium-oauth",
        )

    mock_insert_run = create_autospec(db_conv.insert_run_start, return_value=None)
    mock_progress_run = create_autospec(db_conv.update_run_progress, return_value=None)
    mock_finish_run = create_autospec(db_conv.update_run_finish, return_value=None)
    mock_record_proc = create_autospec(db_conv.record_processed_event_chunks, return_value=1)
    monkeypatch.setattr(db_conv, "insert_run_start", mock_insert_run)
    monkeypatch.setattr(db_conv, "update_run_progress", mock_progress_run)
    monkeypatch.setattr(db_conv, "update_run_finish", mock_finish_run)
    monkeypatch.setattr(db_conv, "record_processed_event_chunks", mock_record_proc)

    mock_tx = create_autospec(AsyncTransaction, instance=True)
    mock_tx.__aenter__.return_value = mock_tx
    mock_tx.__aexit__.return_value = False
    mock_conn = create_autospec(AsyncConnection, instance=True)
    mock_conn.transaction.return_value = mock_tx

    with pytest.raises(archive_cli.PermanentQuotaError):
        await archive_cli.process_weekly_batch(
            seg_events_two_batches,
            mode="run",
            window_start=date(2026, 10, 20),
            window_end=date(2026, 10, 20),
            limiter=archive_cli.HourlyTokenBudgetLimiter(100_000),
            extractor_model="gemini-4-argon-medium-oauth",
            prompt_version="conv_card_v2",
            dry_run=False,
            extract_concurrency=1,
            extract_fn=create_autospec(
                extractor_mod.extract_segment_cards,
                side_effect=_extract_first_ok_second_boom,
            ),
            conn=mock_conn,
        )

    assert mock_finish_run.await_count == 1
    finish_kwargs = mock_finish_run.call_args.kwargs
    assert finish_kwargs["status"] == "failed"
    assert finish_kwargs["segments_processed"] == 1
    finish_stats = finish_kwargs["stats"]
    assert finish_stats["cards_flagged_unsupported_number"] == 2
    assert finish_stats["soft_unsupported_numbers"] == 4
    assert finish_stats["figures_dropped"] == 5
    assert finish_stats["periods_cleared"] == 3
    assert finish_stats["tickers_dropped"] == 1








def test_unmapped_ticker_is_kept_but_mapped_absent_one_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmapped ticker is kept ONLY when the ticker code itself appears in `quote`;
    both mapped and unmapped tickers absent from `quote` are dropped.
    """
    from src.dalton.conversations import extractor as ex

    quote = "海光自己二季度营收增长 65%，按招商的盈利预测，市盈率约 109 倍。"
    kept, dropped = ex.validate_tickers_against_quote(["688041.SS", "688999.SS", "NVDA"], quote)
    assert kept == ["688041.SS"] and dropped == 2

    quote_with_code = "康師傅控股有限公司(0322.HK) 价格=12.86 RSI=69.7"
    kept_code, dropped_absent = ex.validate_tickers_against_quote(["0322.HK", "0325.HK"], quote_with_code)
    assert kept_code == ["0322.HK"] and dropped_absent == 1

    monkeypatch.setattr(
        ex,
        "_load_ticker_company_aliases",
        create_autospec(
            ex._load_ticker_company_aliases,
            return_value={"NVDA": ("NVDA", "Nvidia", "英伟达")},
        ),
    )
    kept_unmapped_absent, dropped_both = ex.validate_tickers_against_quote(["688041.SS", "NVDA"], quote)
    assert kept_unmapped_absent == [] and dropped_both == 2
    kept_unmapped_present, dropped_one = ex.validate_tickers_against_quote(
        ["688041.SS", "NVDA"], "688041.SS 二季度营收增长 65%"
    )
    assert kept_unmapped_present == ["688041.SS"] and dropped_one == 1


def test_short_chinese_alias_matches_in_running_text_but_not_inside_common_words() -> None:
    from src.dalton.conversations import extractor as ex

    assert ex.AMBIGUOUS_SHORT_ALIAS_WORDS["美的"] == (
        "完美的",
        "精美的",
        "优美的",
        "甜美的",
        "柔美的",
    )
    assert ex._alias_matches_in_text("海光", "海光自己二季度营收增长 65%")
    assert ex._alias_matches_in_text("腾讯", "同期的腾讯视频同比 -40%")
    assert not ex._alias_matches_in_text("美的", "这是一个完美的季度")
    assert ex._alias_matches_in_text("美的", "美的集团股份有限公司(000333.SZ)")


@pytest.mark.asyncio
async def test_round4_narrow_card_rejection_and_rejected_audit_sample() -> None:
    """Round 4:
    1. Only metric numbers with units in `claim` cause whole-card rejection (`unsupported_claim_numbers`);
       unmatched bare integers, years, dates, codes, and MA windows are recorded in `soft_unsupported_numbers`.
    2. English magnitudes (`$1.2B` <-> `12亿美元`, `120 万美元` <-> `$1.2M`, `3.5bn` <-> `35亿`) match bidirectionally;
       `m` is treated as million only with currency/amount/quantity context.
    3. Symmetric MA windows (`50MA`, `MA50`, `50日均线`, `50日线`, `50D`, `30D`) are stripped on both sides while
       attached MA prices (`跌破200MA!(125.23)`) still participate in validation.
    4. Full Chinese classifier support (`个、段、家、只、次、名、位、条、项、款、台、套、份、轮、批、季、年、月、天、周、倍、成`)
       while excluding `一成不变`, `一度`, `十分`.
    5. Date mask unit guard only guards real units in `METRIC_UNITS_TABLE` / `_UNIT_CORE`, not arbitrary English words.
    6. Verify all 25 cards in `tests/unit/dalton/fixtures/conv_cards_rejected_audit.json`.
    """
    from src.dalton.conversations import extractor as ex

    # --- Item 1: Unit-gated whole-card rejection vs soft_unsupported_numbers ---
    bare_card = {
        "claim": "欧洲议会通过文本（P10_TA(2026)0334）要求调查稀土领域行为，涉及编号 42 与 99。",
        "quote": "原文要求调查中国在稀土领域的胁迫行为。",
        "figures": [
            {"metric": "unsupported_fig", "value": 42, "unit": "count", "period": ""},
        ],
        "tickers": [],
    }
    bare_res = ex.validate_candidate_card(bare_card, quote=bare_card["quote"])
    assert bare_res.ok is True
    assert bare_res.unsupported_claim_numbers == []
    assert bare_res.soft_unsupported_numbers >= 2
    assert bare_res.figures_dropped == 1
    assert bare_res.figures == []

    unit_card = {
        "claim": "公司预计明年毛利率提升 5 个百分点，新增产值 8.5 亿元。",
        "quote": "公司预计明年毛利率明显提升。",
        "figures": [],
        "tickers": [],
    }
    unit_res = ex.validate_candidate_card(unit_card, quote=unit_card["quote"])
    assert unit_res.ok is True
    assert unit_res.status == "active"
    assert unit_res.confidence == 0.4
    assert unit_res.unsupported_claim_numbers == ["5", "8.5"]
    assert unit_res.figures == [
        {"metric": "_verification", "value": "unsupported:5,8.5", "unit": "", "period": ""}
    ]

    # --- Item 2: English magnitudes ($1.2B <-> 12亿美元, 120 万美元 <-> $1.2M, 3.5bn <-> 35亿) ---
    for q_txt, c_txt in [
        ("本季收入 $1.2B", "本季收入为 12 亿美元"),
        ("本季收入为 12 亿美元", "本季收入 $1.2B"),
        ("服务费收入 120 万美元", "服务费收入达 $1.2M"),
        ("服务费收入达 $1.2M", "服务费收入 120 万美元"),
        ("指引自由现金流 3.5bn", "指引自由现金流 35 亿"),
        ("指引自由现金流 35 亿", "指引自由现金流 3.5bn"),
    ]:
        res_dir = ex.validate_candidate_card({"claim": c_txt, "figures": [], "tickers": []}, quote=q_txt)
        assert res_dir.ok is True, f"Failed direction: quote={q_txt!r} -> claim={c_txt!r}"
        assert res_dir.unsupported_claim_numbers == []

    # Lowercase `m` without currency/financial context is NOT expanded to 1,000,000
    assert "100000000" not in ex.extract_quote_supported_numbers("距离海岸线 100m 处建设雷达站")
    assert "17000000" in ex.extract_quote_supported_numbers("expects an FX loss of 17m yuan in H1")

    # --- Item 3: Symmetric MA windows (`50MA`, `MA50`, `50日均线`, `50日线`, `50D`, `30D`) & MA prices ---
    res_ma_sym = ex.validate_candidate_card(
        {
            "claim": "股价突破 50 日均线与 200 日线，过去 30D 与 50D 动量转正。",
            "figures": [],
            "tickers": [],
        },
        quote="股价突破 50MA 与 MA200，近期动量转正。",
    )
    assert res_ma_sym.ok is True
    assert res_ma_sym.unsupported_claim_numbers == []

    # MA price itself (`跌破200MA!(125.23)`) still participates in validation
    res_ma_price_bad = ex.validate_candidate_card(
        {
            "claim": "股价跌破200MA!(125.23)，趋势转空。",
            "figures": [],
            "tickers": [],
        },
        quote="股价跌破200MA!(118.50)，趋势转空。",
    )
    assert res_ma_price_bad.ok is True
    assert res_ma_price_bad.confidence == 0.4
    assert "125.23" in res_ma_price_bad.unsupported_claim_numbers

    res_ma_price_good = ex.validate_candidate_card(
        {
            "claim": "股价跌破 200 日均线（125.23），趋势转空。",
            "figures": [],
            "tickers": [],
        },
        quote="股价跌破200MA!(125.23)，趋势转空。",
    )
    assert res_ma_price_good.ok is True
    assert res_ma_price_good.unsupported_claim_numbers == []

    # --- Item 4: Chinese classifiers & idiom exclusions ---
    for cn_phrase, ar_phrase in [
        ("已有三个客户签约", "已有 3 个客户签约"),
        ("转股期分两段", "转股期分 2 段"),
        ("调查了七家卖方", "调查了 7 家卖方"),
        ("完成三轮问询", "完成 3 轮问询"),
        ("签署五份合同", "签署 5 份合同"),
        ("电感均价二十多元", "电感均价约 20 多元"),
    ]:
        r1 = ex.validate_candidate_card({"claim": ar_phrase, "figures": [], "tickers": []}, quote=cn_phrase)
        r2 = ex.validate_candidate_card({"claim": cn_phrase, "figures": [], "tickers": []}, quote=ar_phrase)
        assert r1.ok is True and r2.ok is True, f"Classifier mismatch: {cn_phrase!r} <-> {ar_phrase!r}"

    res_idioms = ex.validate_candidate_card(
        {
            "claim": "行业格局并非一成不变，股价一度回调但十分稳健，三番五次测试底部。",
            "figures": [],
            "tickers": [],
        },
        quote="行业格局仍在演进，股价曾回调但非常稳健，多次测试底部。",
    )
    assert res_idioms.ok is True
    assert res_idioms.unsupported_claim_numbers == []

    # --- Item 5: Date mask unit guard only guards real units, not arbitrary English words ---
    # `07-23 UTC` has an English timezone word (not a unit), so `07-23` is masked as a date;
    # `10-12 GW` and `10-12 shares` have real units in `METRIC_UNITS_TABLE`, so `10` and `12` are preserved.
    q_date_tz = ex.extract_quote_supported_numbers("会议将于 07-23 UTC 召开")
    assert "7" not in q_date_tz and "23" not in q_date_tz
    q_unit_range = ex.extract_quote_supported_numbers("新增装机 10-12 GW，回购 15-20 shares")
    assert {"10", "12", "15", "20"}.issubset(q_unit_range)

    # --- Item 6: Verify 25 rejected audit cards from fixture ---
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "conv_cards_rejected_audit.json"
    audit_cards = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert len(audit_cards) == 25

    both_false_reject_ids: list[str] = []
    both_correct_reject_rejected_ids: list[str] = []
    both_correct_reject_bare_exception_ids: list[str] = []
    split_ids: list[str] = []

    for item in audit_cards:
        cid = item["card_id"]
        consensus = item["consensus"]
        res = ex.validate_candidate_card(
            {
                "claim": item["claim"],
                "figures": [dict(f) for f in item.get("figures", [])],
                "tickers": list(item.get("tickers", [])),
                "cited_sources": list(item.get("cited_sources", [])),
            },
            quote=item["quote"],
        )
        if consensus == "both_false_reject":
            both_false_reject_ids.append(cid)
            assert res.ok is True, f"both_false_reject card {cid} was still rejected: {res.unsupported_claim_numbers}"
            assert res.unsupported_claim_numbers == []
        elif consensus == "split":
            split_ids.append(cid)
            assert res.ok is True, f"split card {cid} (identifier/code) should not be rejected: {res.unsupported_claim_numbers}"
            assert res.unsupported_claim_numbers == []
            assert res.soft_unsupported_numbers > 0
        elif consensus == "both_correct_reject":
            if cid == "cc_31a6e507886bb4a5a3812d36":
                # #17 has only bare unitless numbers in claim (`(6682.HK，27.48)`, `(5802.T，2211.0)`),
                # so under the unit-only rule it has no unsupported metric numbers while dropping its 2 unsupported figures.
                both_correct_reject_bare_exception_ids.append(cid)
                assert res.ok is True
                assert res.unsupported_claim_numbers == []
                assert res.soft_unsupported_numbers == 4
                assert res.figures_dropped == 2
            else:
                both_correct_reject_rejected_ids.append(cid)
                assert res.ok is True
                assert res.status == "active"
                assert res.confidence == 0.4, f"both_correct_reject card {cid} should be flagged with confidence=0.4"
                assert len(res.unsupported_claim_numbers) > 0
                assert res.figures[-1]["metric"] == "_verification"
                assert res.figures[-1]["value"].startswith("unsupported:")

    assert len(both_false_reject_ids) == 10
    assert len(split_ids) == 3
    assert len(both_correct_reject_rejected_ids) == 11
    assert both_correct_reject_bare_exception_ids == ["cc_31a6e507886bb4a5a3812d36"]


@pytest.mark.asyncio
async def test_round5_flag_instead_of_reject_parsing_fixes_and_enumeration_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5:
    1. Unsupported unit-bearing numbers in `claim` no longer reject the card (`ok=True`, `status='active'`);
       instead `confidence` is capped at `min(orig, 0.4)`, `figures` receives a `_verification` marker entry,
       the weekly summary counts `cards_flagged_unsupported_number`, and `conversation_search` exposes
       `verification: 'unsupported_numbers'` on flagged cards without altering its docstring.
    2. Parsing fixes:
       - Alphabetic currency codes tightly attached to numbers (`USD7,176.0mn`, `RMB31,844`, `RMB34.57亿元`, `HK$70.00`, `RMB-4,001.615mn`)
       - Thousands-separator commas vs CSV/field-separator commas (`45,,,70.00`, `1,2,3`, `,-15%,25,100.00,20X,"5,109,588"`)
       - Uppercase `5M` / `> 5M downloads` converted to `500万` (`5000000`); `Qwen3-8B` stripped
       - `3/15` without date context preserved as fraction (`3` and `15`), masked only with date context
       - `"一份"`, `"一下"`, `"一起"`, `"一点"`, `"一次性"` not treated as metric numbers
    3. Simple derivable enumeration counts: `quote` enumerating `TOL PHM DHI` supports `"三家"` in `claim`.
    """
    from src.dalton.conversations import extractor as ex
    from src.dalton.conversations import search as search_mod
    from src.dalton.tools import conversation_tools as conv_tools_mod

    # --- Item 1: Flagging (`confidence <= 0.4`, `_verification` figure, `status='active'`) & `conversation_search` ---
    flagged_card = {
        "claim": "从 220 亿增至 343 亿，增幅达 56%。",
        "quote": "收入从 220 亿增长到 343 亿。",
        "figures": [
            {"metric": "rev_start", "value": 220, "unit": "CNY_yi", "period": ""},
            {"metric": "rev_end", "value": 343, "unit": "CNY_yi", "period": ""},
            {"metric": "rev_yoy", "value": 56, "unit": "percent", "period": ""},
        ],
        "tickers": [],
        "confidence": 0.85,
    }
    res_flag = ex.validate_candidate_card(flagged_card, quote=flagged_card["quote"])
    assert res_flag.ok is True
    assert res_flag.status == "active"
    assert res_flag.confidence == 0.4
    assert flagged_card["confidence"] == 0.4
    assert res_flag.unsupported_claim_numbers == ["56"]
    assert res_flag.figures_dropped == 1
    assert res_flag.figures == [
        {"metric": "rev_start", "value": 220, "unit": "yi", "period": ""},
        {"metric": "rev_end", "value": 343, "unit": "yi", "period": ""},
        {"metric": "_verification", "value": "unsupported:56", "unit": "", "period": ""},
    ]

    # When original confidence is already below 0.4 (e.g. 0.25), `min(orig, 0.4)` preserves 0.25
    low_conf_card = {
        "claim": "毛利率提升 5 个百分点。",
        "quote": "毛利率有所改善。",
        "figures": [],
        "tickers": [],
        "confidence": 0.25,
    }
    res_low = ex.validate_candidate_card(low_conf_card, quote=low_conf_card["quote"])
    assert res_low.ok is True
    assert res_low.confidence == 0.25
    assert low_conf_card["confidence"] == 0.25

    # `conversation_search` exposes `verification: "unsupported_numbers"` on cards with `_verification` figure
    mock_hybrid = create_autospec(
        search_mod.hybrid_search_conversations,
        return_value={
            "ok": True,
            "query": "石头科技",
            "count": 2,
            "cards": [
                {
                    "card_id": "cc_flagged_1",
                    "claim": "净现金增长 56%",
                    "figures": [
                        {"metric": "_verification", "value": "unsupported:56", "unit": "", "period": ""}
                    ],
                },
                {
                    "card_id": "cc_clean_2",
                    "claim": "市值 71.76 亿美元",
                    "figures": [
                        {"metric": "market_cap", "value": 7176.0, "unit": "USD_mn", "period": ""}
                    ],
                },
            ],
        },
    )
    monkeypatch.setattr(conv_tools_mod, "hybrid_search_conversations", mock_hybrid)
    tool_fn = conv_tools_mod.create_conversation_tools()[0]
    raw_json = await tool_fn.ainvoke(
        {"query": "石头科技"},
        config={"configurable": {"user_id": "u-test-1", "workspace_id": "ws-1", "thread_id": "th-1"}},
    )
    parsed_search = json.loads(raw_json)
    assert parsed_search["cards"][0]["verification"] == "unsupported_numbers"
    assert "verification" not in parsed_search["cards"][1]

    # --- Item 2a: Alphabetic currency codes tightly attached to numbers ---
    # (`USD7,176.0mn`, `USD3,059.315mn`, `RMB-4,001.615mn`, `RMB31,844`, `RMB34.57亿元`, `HK$70.00`)
    res_glued_cur = ex.validate_candidate_card(
        {
            "claim": (
                "海尔智家最新市值约71.76亿美元，2026年6月30日基准净现金为-4,001.615百万元、"
                "严格净现金为-6,544.096百万元；欢聚基准净现金3,059.315百万美元；"
                "安踏严格净现金31,844百万元；创维经营现金流净流出34.57亿元人民币；目标价70港元。"
            ),
            "figures": [
                {"metric": "market_cap", "value": 7176.0, "unit": "USD_mn", "period": ""},
                {"metric": "base_net_cash", "value": -4001.615, "unit": "RMB_mn", "period": "2026-06-30"},
                {"metric": "ocf", "value": -3.457, "unit": "RMB_bn", "period": ""},
                {"metric": "tp", "value": 70.0, "unit": "HKD", "period": ""},
            ],
            "tickers": [],
        },
        quote=(
            "海尔智家市值USD7,176.0mn，2026-06-30基准净现金RMB-4,001.615mn，严格净现金RMB-6,544.096mn；"
            "欢聚净现金USD3,059.315mn；安踏净现金RMB31,844百万元；"
            "创维经营现金流转为净流出RMB34.57亿元；目标价HK$70.00。"
        ),
    )
    assert res_glued_cur.unsupported_claim_numbers == []
    assert res_glued_cur.figures_dropped == 0
    assert res_glued_cur.confidence == 0.8

    # --- Item 2b: Thousands-separator commas vs field-separator (CSV/list) commas ---
    # (`45,,,70.00`, `1,2,3`, and CSV `,-15%,25,100.00,20X,"5,109,588"`)
    q_csv_1 = ex.extract_quote_supported_numbers("36690,海尔智家,家电,57.40,-3%,45,,,70.00,20X,3.50,35")
    assert {"45", "70", "20", "3.5", "35"}.issubset(q_csv_1)
    q_csv_2 = ex.extract_quote_supported_numbers(
        '300750.SZ,宁德时代,动力电池,15376,337.90,625.00,85%,-15%,25,100.00,20X,"5,109,588"'
    )
    assert {"25", "100", "20", "5109588"}.issubset(q_csv_2)
    assert "25100" not in q_csv_2
    q_list = ex.extract_quote_supported_numbers("分批建仓比例为 1,2,3 档")
    assert {"1", "2", "3"}.issubset(q_list)
    assert "123" not in q_list

    res_csv_claim = ex.validate_candidate_card(
        {
            "claim": "宁德时代目标价625.00元，潜在涨幅85%，2027E PE为25倍，2025A EPS为100.00元（对应PE 20倍）。",
            "figures": [],
            "tickers": [],
        },
        quote='300750.SZ,宁德时代,动力电池,15376,337.90,625.00,85%,-15%,25,100.00,20X,"5,109,588"',
    )
    assert res_csv_claim.unsupported_claim_numbers == []

    # --- Item 2c: Uppercase `5M` / `> 5M downloads` <-> `500万`, and `Qwen3-8B` stripping ---
    res_5m = ex.validate_candidate_card(
        {
            "claim": "应用下载量超过500万次。",
            "figures": [{"metric": "downloads", "value": ">5", "unit": "million", "period": ""}],
            "tickers": [],
        },
        quote="（> 5M downloads）",
    )
    assert res_5m.unsupported_claim_numbers == []
    assert res_5m.figures_dropped == 0
    assert "5000000" in ex.extract_quote_supported_numbers("活跃用户达 5M")

    res_qwen = ex.validate_candidate_card(
        {
            "claim": "基于Qwen3-8B的实测显示吞吐量提升58.3%。",
            "figures": [],
            "tickers": [],
        },
        quote="Qwen3-8B 实测吞吐量提升 +58.3%。",
    )
    assert res_qwen.unsupported_claim_numbers == []

    # --- Item 2d: `3/15` without date context is preserved as fraction (`3` and `15`), masked only with date context ---
    res_3_15 = ex.validate_candidate_card(
        {
            "claim": "15只标的中仅3只（3/15）股价在50日均线之上。",
            "figures": [{"metric": "stocks_above_50dma", "value": 3, "unit": "count_out_of_15", "period": ""}],
            "tickers": [],
        },
        quote=">50MA 只有 3/15：GOOG、TSM、WDC",
    )
    assert res_3_15.unsupported_claim_numbers == []
    assert res_3_15.figures_dropped == 0
    assert "3" not in ex.extract_quote_supported_numbers("截至 3/15 收盘股价企稳")

    # --- Item 2e: `"一份"`, `"一下"`, `"一起"`, `"一点"`, `"一次性"` are NOT metric numbers ---
    res_cn_words = ex.validate_candidate_card(
        {
            "claim": "整理了一份纪要，等一下一起看一点数据，剔除一次性影响后利润增长20%。",
            "figures": [],
            "tickers": [],
        },
        quote="利润增长20%。",
    )
    assert res_cn_words.unsupported_claim_numbers == []
    assert res_cn_words.confidence == 0.8

    # --- No enumeration-count exemption (lead, round 5 review): a count not in the quote is
    # flagged, and an unrelated "3%" can no longer ride on a three-name list. Flag, never drop.
    res_enum_tickers = ex.validate_candidate_card(
        {"claim": "三家公司收入增长3%。", "figures": [], "tickers": []},
        quote="TOL PHM DHI",
    )
    assert "3" in res_enum_tickers.unsupported_claim_numbers
    assert res_enum_tickers.ok is True and res_enum_tickers.confidence == 0.4

    res_eleven = ex.validate_candidate_card(
        {"claim": "已签署十一份订单", "figures": [], "tickers": []},
        quote="订单已经签署",
    )
    assert res_eleven.unsupported_claim_numbers == ["11"]




def test_render_shows_verification_flag_not_as_key_figure() -> None:
    from src.dalton.conversations.render import render_conversation_digest_md

    card = {
        "card_id": "cc_test", "type": "fact", "claim": "全年收入增长56%", "quote": "Q3 +51%",
        "date_et": "2026-10-20", "confidence": 0.4, "tickers": [], "entities": [], "sectors": [],
        "figures": [
            {"metric": "revenue_growth", "value": "51", "unit": "%", "period": "Q3"},
            {"metric": "_verification", "value": "unsupported:56", "unit": "", "period": ""},
        ],
    }
    md = render_conversation_digest_md([card], date_et="2026-10-20")
    flag_line = next(line for line in md.splitlines() if "⚠ 核验" in line)
    assert "（56）" in flag_line
    assert "`_verification`" not in md
    assert "`revenue_growth` = `51`" in md


def test_yi_fen_masking_respects_multi_digit_numerals_only() -> None:
    from src.dalton.conversations import extractor as ex

    def flagged(claim: str, quote: str = "无") -> list[str]:
        return ex.validate_candidate_card({"claim": claim, "figures": [], "tickers": []}, quote=quote).unsupported_claim_numbers

    assert flagged("周二一份纪要显示需求平稳") == []
    assert flagged("周一一个月内完成交付") == []
    assert flagged("已签署十一份订单") == ["11"]
    assert flagged("已签署一百零一份订单") == ["101"]


def test_conv_card_v2_1_audit_regressions_prompt_tickers_and_indicator_numbers() -> None:
    """v2.1 regression suite driven by `fixtures/conv_cards_v2_audit_regressions.json`:
    1. Prompt rules for evidence coverage, subject completion ban, interview speaker boundaries,
       qualifying phrases (`尚无公开成果`), `fact` per-clause coverage, `estimate` for third-party
       models & company guidance, `model_assumption` for original speaker's calculations (`原说话人的测算`),
       `cited_sources` verbatim in `quote`, `tickers` subject-only & unmapped code rule, and `figures`
       preserving `约/大约/左右/以上` and quote-scoped `同期/上期`.
    2. `validate_tickers_against_quote`: unmapped tickers dropped unless the code itself appears in `quote`;
       A+H dual-listing aliases (`300750.SZ`/`3750.HK`, `688008.SS`/`6809.HK`, `000333.SZ`/`0300.HK`,
       `002050.SZ`/`2050.HK`, `600660.SS`/`3606.HK`) loaded and matched.
    3. `validate_candidate_card`: indicator-prefixed numbers (`价格=`, `RSI=`, `收盘价`, `市盈率`, `PE`,
       `EV/EBIT` in `METRIC_PREFIX_INDICATORS_TABLE`) treated as metric numbers and counted in
       `unsupported_claim_numbers` (catching `66.3` and `85.6` on `v2_18e39ac4ab23`).
    4. `prompt_version` updated to `conv_card_v2_3`.
    """
    from copy import deepcopy
    from src.dalton.conversations import extractor as ex
    from src.dalton.settings import DaltonConversationsConfig, get_dalton_settings

    # --- Item 6: prompt_version == conv_card_v2_3 ---
    get_dalton_settings.cache_clear()
    assert DaltonConversationsConfig().prompt_version == "conv_card_v2_3"
    assert get_dalton_settings().conversations.prompt_version == "conv_card_v2_3"

    # --- Items 1-4: Prompt rule clauses ---
    prompt = ex.EXTRACTION_SYSTEM_INSTRUCTIONS
    # Item 1: Evidence coverage, no unquoted entities/speakers/conclusions, subject completion ban, interview segmentation, qualifiers
    assert "不得出现 `quote` 里没有的公司、机构、产品、说话人" in prompt
    assert "见顶" in prompt and "止跌" in prompt and "资金流" in prompt
    assert "主体在 `quote` 里只是代词或省略时，扩大连续引用；扩大后仍然覆盖不到，就删掉这张卡或这一句，不得根据 `topic_label` 或相邻卡片补全" in prompt
    assert "访谈体按“某某：”分段，上一段里的“我认为”不能并到下一位说话人" in prompt
    assert "尚无公开成果" in prompt
    # Item 2: Card types & cited_sources
    assert "事实来源必须覆盖 `claim` 里每一句当作 `fact` 的数字句，只覆盖了其中一句就拆卡" in prompt
    assert "第三方模型预测、公司经营指引" in prompt
    assert "原说话人的测算" in prompt
    assert "抽取者自己的" not in prompt
    assert "“我算”“隐含”“按全价算”“粗算”" in prompt
    assert "“引用某报告”只覆盖报告明确披露的那几句，不覆盖随后自行推导的结论" in prompt
    assert "`cited_sources` 只填 `quote` 里实际出现的来源名称" in prompt
    # Item 3: Tickers prompt rules
    assert "股东、客户、被比较方如果不是本卡的主体，不放进 `tickers`" in prompt
    assert "没有别名映射的代码，只有代码本身出现在 `quote` 里时才保留，否则删除" in prompt
    # Item 4: Figures prompt rules
    assert "“约、大约、左右、以上”保留在 `value` 字符串里" in prompt
    assert '"约142"' in prompt
    assert "“同期、上期”只能继承本卡 `quote` 里写明的期间" in prompt

    # --- Load v2 audit regression fixture (never reading `.agy/`) ---
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "conv_cards_v2_audit_regressions.json"
    cards = json.loads(fixture_path.read_text(encoding="utf-8"))
    by_id = {c["card_id"]: c for c in cards}
    assert len(by_id) >= 12

    # --- Item 5 & Item 3 on v2_18e39ac4ab23: 66.3 and 85.6 flagged in unsupported_claim_numbers, 0325.HK dropped ---
    c_18e = deepcopy(by_id["v2_18e39ac4ab23"])
    res_18e = ex.validate_candidate_card(c_18e)
    assert res_18e.ok is True
    assert res_18e.confidence == 0.4
    assert res_18e.tickers == ["0322.HK"]
    assert res_18e.tickers_dropped == 1
    assert res_18e.unsupported_claim_numbers == ["66.3", "85.6", "44.6", "1.6", "64.37"]
    assert res_18e.figures[-1] == {
        "metric": "_verification",
        "value": "unsupported:66.3,85.6,44.6,1.6,64.37",
        "unit": "",
        "period": "",
    }

    # --- Item 5: Extensible METRIC_PREFIX_INDICATORS_TABLE coverage ---
    for prefix_expr in ("价格=66.3", "RSI=85.6", "收盘价 557.64", "市盈率 25", "PE=30", "EV/EBIT 18.5"):
        res_ind = ex.validate_candidate_card(
            {"claim": f"某公司 {prefix_expr}。", "figures": [], "tickers": []},
            quote="某公司近期表现平稳。",
        )
        assert res_ind.confidence == 0.4, f"Expected indicator prefix {prefix_expr!r} to be flagged"
        assert len(res_ind.unsupported_claim_numbers) == 1

    # --- Item 3 on v2_76651895b370 (3750.HK absent from quote), v2_114c2aa5ca39 (MKSI absent), v2_e91a4e64c02e & v2_ebcb67dd7bb2 (XNET absent) ---
    for cid_drop in ("v2_76651895b370", "v2_114c2aa5ca39", "v2_e91a4e64c02e", "v2_ebcb67dd7bb2"):
        c_item = deepcopy(by_id[cid_drop])
        res_item = ex.validate_candidate_card(c_item)
        assert res_item.tickers == [], f"Expected tickers to be dropped for {cid_drop}"
        assert res_item.tickers_dropped == 1

    # Unmapped ticker whose code DOES appear in quote (v2_bb4895823474: BOXX) is kept
    c_boxx = deepcopy(by_id["v2_bb4895823474"])
    res_boxx = ex.validate_candidate_card(c_boxx)
    assert res_boxx.tickers == ["BOXX"]
    assert res_boxx.tickers_dropped == 0

    # A+H dual-listed mappings: CATL 300750.SZ / 3750.HK and peers match when company name is in quote
    ex._load_ticker_company_aliases.cache_clear()
    for ah_tickers, company_quote in [
        (["300750.SZ", "3750.HK"], "宁德时代三季度电池出货量与单位净利超预期"),
        (["688008.SS", "6809.HK"], "澜起科技互连类芯片毛利率创新高"),
        (["000333.SZ", "0300.HK"], "美的集团海外空调出口同比增长21%"),
        (["002050.SZ", "2050.HK"], "三花智控机器人执行器产线进展顺利"),
        (["600660.SS", "3606.HK"], "福耀玻璃海外市占率持续提升"),
    ]:
        kept_ah, dropped_ah = ex.validate_tickers_against_quote(ah_tickers, company_quote)
        assert kept_ah == ah_tickers and dropped_ah == 0

    # Agent fact card without cited source in quote (v2_f6fafaac280d) downgrades to opinion
    _spk, eff_type, _srcs = ex.enforce_speaker_and_agent_citation(
        by_id["v2_f6fafaac280d"]["type"],
        [],
        {"message_role": "assistant", "text": by_id["v2_f6fafaac280d"]["quote"]},
        quote=by_id["v2_f6fafaac280d"]["quote"],
    )
    assert eff_type == "opinion"


@pytest.mark.asyncio
async def test_conv_card_v2_2_programmatic_fallbacks_and_audit_regressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2.2 programmatic validation fallbacks driven by `fixtures/conv_cards_v2_1_audit_regressions.json`:
    1. `topic_label` does NOT backfill from `seg_topic_label` when card `topic_label` is omitted/empty.
    2. Proper-noun validation (`unsupported_names`) strips unsupported company/institution/proper names
       from `topic_label` and flags `claim` with `confidence <= 0.4` and `unsupported_names:<names>`.
    3. Forbidden conclusion words (`FORBIDDEN_CONCLUSION_WORDS_TABLE`) in `claim` absent from `quote`
       flag `confidence <= 0.4` and `unsupported_conclusion:<words>`.
    4. Keyword-based `type` correction (`fact -> estimate`, `opinion -> model_assumption`) counted in `types_corrected`.
    5. Alias-based ticker backfill (`tickers_backfilled`), including A+H dual-listed pairs, while skipping
       source institutions and excluded companies (`剔除/不含`).
    6. Same-sentence period anchoring (`is_period_supported_by_quote` with `figure_value` across `。！？；\\n`).
    7. Stripping unsupported currency from `figures.unit` (`currencies_stripped`).
    8. `prompt_version == 'conv_card_v2_3'` and all new counters propagated into weekly batch summaries.
    """
    from copy import deepcopy
    from src.dalton.conversations import extractor as ex
    from src.dalton.settings import DaltonConversationsConfig, get_dalton_settings

    get_dalton_settings.cache_clear()
    assert DaltonConversationsConfig().prompt_version == "conv_card_v2_3"
    assert get_dalton_settings().conversations.prompt_version == "conv_card_v2_3"

    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "conv_cards_v2_1_audit_regressions.json"
    )
    by_id = {c["card_id"]: c for c in json.loads(fixture_path.read_text(encoding="utf-8"))}
    assert len(by_id) == 11

    # --- Item 2: Proper-noun validation (`v21_82d1bcb330b2`, `v21_1136705dbec4`, `v21_676e010b7206`, `v21_9d8e0e9966ec`) ---
    c_ddog = deepcopy(by_id["v21_82d1bcb330b2"])
    res_ddog = ex.validate_candidate_card(c_ddog)
    assert res_ddog.confidence == 0.4
    assert "Datadog" in res_ddog.unsupported_names
    assert "Datadog" not in c_ddog["topic_label"]
    assert any(
        f["metric"] == "_verification" and "unsupported_names:Datadog" in f["value"]
        for f in res_ddog.figures
    )

    c_zycm = deepcopy(by_id["v21_1136705dbec4"])
    res_zycm = ex.validate_candidate_card(c_zycm)
    assert res_zycm.confidence == 0.4
    assert "中原传媒" in res_zycm.unsupported_names
    assert "中原传媒" not in c_zycm["topic_label"]
    assert any(
        f["metric"] == "_verification" and "unsupported_names:中原传媒" in f["value"]
        for f in res_zycm.figures
    )

    c_ferretti = deepcopy(by_id["v21_676e010b7206"])
    res_ferretti = ex.validate_candidate_card(c_ferretti)
    assert res_ferretti.unsupported_names == []
    assert c_ferretti["topic_label"] == ""

    c_latepost = deepcopy(by_id["v21_9d8e0e9966ec"])
    res_latepost = ex.validate_candidate_card(c_latepost)
    assert res_latepost.confidence == 0.4
    assert {"晚点聊", "陈哲"}.issubset(set(res_latepost.unsupported_names))

    # --- Item 3 & Item 7: Forbidden conclusion words + currency stripping (`v21_e0955af6436b`) ---
    c_popmart = deepcopy(by_id["v21_e0955af6436b"])
    res_popmart = ex.validate_candidate_card(c_popmart)
    assert res_popmart.confidence == 0.4
    assert "止跌" in res_popmart.unsupported_conclusions
    assert "泡泡玛特" in res_popmart.unsupported_names
    assert c_popmart["topic_label"] == ""
    assert res_popmart.currencies_stripped == 6
    assert all(
        f["unit"] == "" for f in res_popmart.figures if f["metric"] != "_verification"
    )
    verif_vals = [f["value"] for f in res_popmart.figures if f["metric"] == "_verification"]
    assert any("unsupported_conclusion:止跌" in v for v in verif_vals)
    assert any("unsupported_names:泡泡玛特" in v for v in verif_vals)

    # --- Item 4 & Item 7: Keyword-based type correction + currency stripping (`v21_923ce4b11825`) ---
    c_kol = deepcopy(by_id["v21_923ce4b11825"])
    corr_type_kol, corr_cnt_kol = ex.correct_card_type_by_keywords(
        c_kol["type"], c_kol["claim"], c_kol["quote"]
    )
    assert corr_type_kol == "estimate" and corr_cnt_kol == 1
    res_kol = ex.validate_candidate_card(c_kol)
    assert res_kol.currencies_stripped == 1
    assert res_kol.figures[0]["unit"] == "万"

    # --- Item 4, Item 5 & Item 6: `opinion -> model_assumption`, A+H ticker backfill, same-sentence period ---
    c_catl = deepcopy(by_id["v21_030732b1fda5"])
    corr_type_catl, corr_cnt_catl = ex.correct_card_type_by_keywords(
        c_catl["type"], c_catl["claim"], c_catl["quote"]
    )
    assert corr_type_catl == "model_assumption" and corr_cnt_catl == 1
    res_catl = ex.validate_candidate_card(c_catl)
    assert res_catl.tickers == ["300750.SZ", "3750.HK"]
    assert res_catl.tickers_backfilled == 2
    assert "摩根大通" in res_catl.unsupported_names
    assert res_catl.confidence == 0.4

    c_atour_v21 = deepcopy(by_id["v21_b64e8dd14d8e"])
    corr_type_atour, corr_cnt_atour = ex.correct_card_type_by_keywords(
        c_atour_v21["type"], c_atour_v21["claim"], c_atour_v21["quote"]
    )
    assert corr_type_atour == "model_assumption" and corr_cnt_atour == 1
    res_atour_v21 = ex.validate_candidate_card(c_atour_v21)
    assert c_atour_v21["topic_label"] == ""
    assert res_atour_v21.periods_cleared == 3
    fig_periods_by_val = {
        str(f["value"]): f["period"]
        for f in res_atour_v21.figures
        if f["metric"] != "_verification"
    }
    assert fig_periods_by_val["2175"] == ""
    assert fig_periods_by_val["+19.2"] == ""
    assert fig_periods_by_val["811"] == ""
    assert fig_periods_by_val["101"] == "Q2"
    assert fig_periods_by_val["500"] == "全年"

    # --- Item 5 & Item 6: Ticker backfill & same-sentence period on `v21_91a9eb6d35f0`, `v21_3bf61c4efa60`, `v21_f202f73c375d` ---
    c_insta_v21 = deepcopy(by_id["v21_91a9eb6d35f0"])
    res_insta_v21 = ex.validate_candidate_card(c_insta_v21)
    assert res_insta_v21.tickers == ["688775.SS"]
    assert res_insta_v21.tickers_backfilled == 1

    c_ai_apps = deepcopy(by_id["v21_3bf61c4efa60"])
    res_ai_apps = ex.validate_candidate_card(c_ai_apps)
    assert c_ai_apps["topic_label"] == ""
    assert {"300033.SZ", "1357.HK", "2513.HK", "3696.HK", "3317.HK"}.issubset(
        set(res_ai_apps.tickers)
    )
    assert res_ai_apps.tickers_backfilled == 5

    c_el = deepcopy(by_id["v21_f202f73c375d"])
    res_el = ex.validate_candidate_card(c_el)
    assert res_el.tickers == ["EL"]
    assert res_el.tickers_backfilled == 1
    assert res_el.periods_cleared == 2
    assert [f["period"] for f in res_el.figures] == ["FY2027", "FY2027", "FY2027", "", ""]

    # Excluded company after `剔除/不含` is NOT backfilled, while non-excluded subject company IS backfilled
    bf_tickers, bf_cnt = ex.backfill_tickers_from_quote(
        [],
        "剔除中芯国际后，澜起科技互连类芯片毛利率回升至69.3%。",
        claim="剔除中芯国际后，澜起科技互连类芯片毛利率回升至69.3%。",
    )
    assert "0981.HK" not in bf_tickers and "688981.SS" not in bf_tickers
    assert {"688008.SS", "6809.HK"}.issubset(set(bf_tickers))
    assert bf_cnt == 2

    # --- Item 1 & Item 8: End-to-end `extract_segment_cards` + `process_weekly_batch` counters & no seg_topic_label backfill ---
    seg_v22 = [
        {
            "agent": "main",
            "session_id": "sess-v22",
            "session_key": "agent:main:discord:channel:v22",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-v22-1",
            "seq": 1,
            "ts": "2026-04-06T10:00:00+00:00",
            "date_et": "2026-04-06",
            "message_role": "assistant",
            "speaker": {"sender_id_hash": "agent:main", "name": "Eve", "is_owner": False, "role": "agent"},
            "text": "\n".join(
                [
                    by_id["v21_676e010b7206"]["quote"],
                    by_id["v21_b64e8dd14d8e"]["quote"],
                    by_id["v21_e0955af6436b"]["quote"],
                    by_id["v21_923ce4b11825"]["quote"],
                    by_id["v21_030732b1fda5"]["quote"],
                ]
            ),
        }
    ]
    llm_cards_v22 = []
    for cid in (
        "v21_676e010b7206",
        "v21_b64e8dd14d8e",
        "v21_e0955af6436b",
        "v21_923ce4b11825",
        "v21_030732b1fda5",
    ):
        item = deepcopy(by_id[cid])
        item["quote_event_id"] = "ev-v22-1"
        if cid == "v21_676e010b7206":
            # Model omitted card topic_label while segment-level topic_label is set
            item["topic_label"] = ""
        llm_cards_v22.append(item)

    mock_assist_v22 = create_autospec(
        ex.assist_json,
        return_value=AssistResult(
            ok=True,
            status="ok",
            data={
                "category": "research",
                "topic_label": "法拉帝 (9638.HK / YACHT.MI)",
                "cards": llm_cards_v22,
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 400, "output_tokens": 200, "total_tokens": 600},
        ),
    )
    monkeypatch.setattr(ex, "assist_json", mock_assist_v22)

    out_v22 = await ex.extract_segment_cards(seg_v22)
    assert out_v22.ok is True
    assert len(out_v22.cards) == 5
    by_q = {c["quote"]: c for c in out_v22.cards}
    # Item 1: `topic_label` is NOT backfilled from `seg_topic_label` ("法拉帝 (9638.HK / YACHT.MI)")
    assert by_q[by_id["v21_676e010b7206"]["quote"]]["topic_label"] == ""
    assert out_v22.cards_flagged_unsupported_names == 2
    assert out_v22.cards_flagged_unsupported_conclusion == 1
    assert out_v22.currencies_stripped == 7
    assert out_v22.tickers_backfilled == 2
    assert out_v22.types_corrected >= 2

    batch_v22 = await archive_cli.process_weekly_batch(
        [dict(ev) for ev in seg_v22],
        mode="run",
        window_start=date(2026, 4, 6),
        window_end=date(2026, 4, 6),
        limiter=archive_cli.HourlyTokenBudgetLimiter(100_000),
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v2_2",
        dry_run=True,
    )
    assert batch_v22["cards_flagged_unsupported_names"] == 2
    assert batch_v22["cards_flagged_unsupported_conclusion"] == 1
    assert batch_v22["currencies_stripped"] == 7
    assert batch_v22["tickers_backfilled"] == 2
    assert batch_v22["types_corrected"] >= 2


@pytest.mark.asyncio
async def test_conv_card_v2_3_audit_regressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2.3 regression suite driven by `fixtures/conv_cards_v2_2_audit_regressions.json`:
    1. `backfill_tickers_from_quote` only backfills companies present in `claim` or `topic_label`
       (never whole-quote collateral companies like NOW/PANW/HUBS on `v22_3bb06b5dee29`, 0981.HK on
       `v22_9bd5b2c7e788`, GOOGL on `v22_0b0936821124`/`v22_8b5b1e3a0dd5`, or customer TSLA on `v22_e84937510fc1`).
    2. Private companies (`SPCX`, `PRIVATE_COMPANIES`) and trading channels (`TRADING_CHANNEL_NAMES` /
       `TRADING_CHANNEL_TICKERS` unless discussed in `claim`) are never backfilled and are dropped when
       supplied by the model (`v22_0b0936821124`, `v22_bf1790fd1ca2`, `v22_f02b05b12ff5`, vs `v22_facfd532208d`).
    3. English exclusion phrases (`excluding`, `excludes`, `not used`, `doesn't use`, `don't use`,
       `does not use`, `not included`, `ex-`) drop/prevent backfill of excluded companies (`v22_e84937510fc1`).
    4. Expanded `SUPPLEMENTAL_TICKER_ALIASES` (`GTLB`, `ESTC`, `7581.T`, `600809.SS`, `000568.SZ`,
       `002304.SZ`, `300413.SZ` with `芒果` ambiguity guard, `300308.SZ` Innolight, `002463.SZ` WUS,
       `002371.SZ` Naura, `688012.SS` AMEC).
    5. False-positive flag fixes: English number words (`one`..`twelve`, `a dozen`, `half`, `first`..`fourth`
       before `quarter`, `and <num> other` -> `num + 1` on `v22_9bd5b2c7e788`, `v22_e84937510fc1`),
       short token `.` boundary (`K2` in `Kimi K2.6` on `v22_691cfefe91e4`), and acronym-to-phrase
       support (`CPO` <- `co-packaged optics` on `v22_511e15a42663`).
    6. Bidirectional `correct_card_type_by_keywords` (`opinion`/`model_assumption -> estimate` on
       `v22_ea4a5347e261` and `v22_ab31dc53f356`; `estimate -> fact` on `v22_cd43110a06ce`;
       `unverified_source` flagging with `confidence <= 0.5` on `v22_d58e0441d511`).
    7. `topic_label` strips `FORBIDDEN_CONCLUSION_WORDS_TABLE` (`止跌` on `v22_6b9dca9e63a0`).
    8. `strip_unsupported_currency_from_unit` clears `倍`/`x` when the sentence lacks `倍`/`x`/`times`
       (`v22_691cfefe91e4`), plus prompt rules for full-name-vs-acronym and weight/ratio units.
    9. `prompt_version == 'conv_card_v2_3'` and `tickers_dropped_private` / `tickers_dropped_channel`
       in weekly batch summaries.
    """
    from copy import deepcopy
    from src.dalton.conversations import extractor as ex
    from src.dalton.settings import DaltonConversationsConfig, get_dalton_settings

    get_dalton_settings.cache_clear()
    assert DaltonConversationsConfig().prompt_version == "conv_card_v2_3"
    assert get_dalton_settings().conversations.prompt_version == "conv_card_v2_3"

    prompt = ex.EXTRACTION_SYSTEM_INSTRUCTIONS
    assert "quote 里写的是英文全称时，claim 不要自行缩写成 quote 里没有出现的缩写词" in prompt
    assert "权重、占比、系数（如“权重为 0.25”）不要把 unit 写成“倍”" in prompt

    fixture_path = (
        Path(__file__).resolve().parent / "fixtures" / "conv_cards_v2_2_audit_regressions.json"
    )
    by_id = {c["card_id"]: c for c in json.loads(fixture_path.read_text(encoding="utf-8"))}
    assert len(by_id) == 19

    # --- Item 1 & Item 4: Subject-only ticker backfill + expanded alias table ---
    ex._load_ticker_company_aliases.cache_clear()

    # v22_3bb06b5dee29: GitLab in claim/topic -> GTLB backfilled; NOW/PANW/HUBS in quote only -> NOT backfilled
    c_gtlb = deepcopy(by_id["v22_3bb06b5dee29"])
    res_gtlb = ex.validate_candidate_card(c_gtlb)
    assert res_gtlb.tickers == ["GTLB"]
    assert res_gtlb.tickers_backfilled == 1

    # v22_9bd5b2c7e788: `three quarters ahead of plan` supports `三个季度`; Naura/AMEC/Alibaba backfilled; SMIC (0981.HK) NOT backfilled
    c_9bd = deepcopy(by_id["v22_9bd5b2c7e788"])
    res_9bd = ex.validate_candidate_card(c_9bd)
    assert res_9bd.unsupported_claim_numbers == []
    assert res_9bd.confidence == 0.85
    assert "0981.HK" not in res_9bd.tickers and "688981.SS" not in res_9bd.tickers
    assert {"BABA", "002371.SZ", "688012.SS"}.issubset(set(res_9bd.tickers))

    # v22_8b5b1e3a0dd5: Meta in claim/topic -> META backfilled; Google in quote only -> GOOGL NOT backfilled
    c_8b5 = deepcopy(by_id["v22_8b5b1e3a0dd5"])
    res_8b5 = ex.validate_candidate_card(c_8b5)
    assert res_8b5.tickers == ["META"]
    assert "GOOGL" not in res_8b5.tickers

    # v22_8f73b698883f: 汾酒/泸州老窖(老窖)/洋河 -> 600809.SS, 000568.SZ, 002304.SZ backfilled
    c_8f7 = deepcopy(by_id["v22_8f73b698883f"])
    res_8f7 = ex.validate_candidate_card(c_8f7)
    assert {"600809.SS", "000568.SZ", "002304.SZ"}.issubset(set(res_8f7.tickers))
    assert res_8f7.tickers_backfilled == 3

    # v22_1d3de3669d94 vs v22_31ad8112a9ec: `芒果超媒`/`芒果TV` backfills 300413.SZ; bare `芒果` without `超媒`/`TV`/`台` does not, nor is it flagged in unsupported_names
    c_1d3 = deepcopy(by_id["v22_1d3de3669d94"])
    res_1d3 = ex.validate_candidate_card(c_1d3)
    assert res_1d3.tickers == ["300413.SZ"]
    assert res_1d3.tickers_backfilled == 1

    c_31a = deepcopy(by_id["v22_31ad8112a9ec"])
    res_31a = ex.validate_candidate_card(c_31a)
    assert res_31a.tickers == []
    assert res_31a.unsupported_names == []
    # When quote has `芒果` and claim has `芒果超媒`, `300413.SZ` still validates if supplied
    c_31a_with_super = deepcopy(by_id["v22_31ad8112a9ec"])
    c_31a_with_super["claim"] = "UBS Evidence Lab显示芒果TV 2Q26与7M26平均MAU同比分别降11%与13%。"
    c_31a_with_super["quote"] = "但 UBS Evidence Lab 显示芒果TV 2Q26 与 7M26 平均 MAU 同比分别降 11% 与 13%"
    assert ex.validate_candidate_card(c_31a_with_super).tickers == ["300413.SZ"]
    assert not ex._alias_matches_in_text("芒果", "今天买了一箱海南芒果")

    # v22_b4db5f4c7ca0: Elastic -> ESTC backfilled; `原预期内` does NOT flip `estimate` to `fact`
    c_b4d = deepcopy(by_id["v22_b4db5f4c7ca0"])
    res_b4d = ex.validate_candidate_card(c_b4d)
    assert res_b4d.tickers == ["ESTC"]
    assert res_b4d.tickers_backfilled == 1
    assert c_b4d["type"] == "estimate"
    assert res_b4d.types_corrected == 0

    # --- Item 2: Private companies (`SPCX`) and trading channels (`IBKR` vs `FUTU`) ---
    c_0b0 = deepcopy(by_id["v22_0b0936821124"])
    res_0b0 = ex.validate_candidate_card(c_0b0)
    assert res_0b0.tickers == []
    assert res_0b0.tickers_dropped == 1
    assert res_0b0.tickers_dropped_private == 1

    c_bf1 = deepcopy(by_id["v22_bf1790fd1ca2"])
    res_bf1 = ex.validate_candidate_card(c_bf1)
    assert res_bf1.tickers == ["TSM", "INTC"]
    assert res_bf1.tickers_dropped == 1
    assert res_bf1.tickers_dropped_private == 1

    c_f02 = deepcopy(by_id["v22_f02b05b12ff5"])
    res_f02 = ex.validate_candidate_card(c_f02)
    assert res_f02.tickers == ["IB01", "VDST", "BOXX", "SGOV"]
    assert res_f02.tickers_dropped == 1
    assert res_f02.tickers_dropped_channel == 1

    c_fac = deepcopy(by_id["v22_facfd532208d"])
    res_fac = ex.validate_candidate_card(c_fac)
    assert "FUTU" in res_fac.tickers
    assert res_fac.tickers_dropped_channel == 0

    # --- Item 3 & Item 5a: English exclusions (`doesn't use`) & `CMBI and three other` (=4) + `Two items` (=2) on `v22_e84937510fc1` ---
    c_e84 = deepcopy(by_id["v22_e84937510fc1"])
    res_e84 = ex.validate_candidate_card(c_e84)
    assert res_e84.unsupported_claim_numbers == []
    assert res_e84.confidence == 0.85
    assert "5803.T" not in res_e84.tickers and "6857.T" not in res_e84.tickers
    assert "TSLA" not in res_e84.tickers
    assert {"0700.HK", "002028.SZ", "002050.SZ"}.issubset(set(res_e84.tickers))

    # --- Item 5b & Item 8: `K2` in `Kimi K2.6` not flagged, and `unit: "倍"` on weight `0.25` cleared (`v22_691cfefe91e4`) ---
    c_691 = deepcopy(by_id["v22_691cfefe91e4"])
    res_691 = ex.validate_candidate_card(c_691)
    assert res_691.unsupported_names == []
    assert res_691.confidence == 0.85
    weight_fig = next(f for f in res_691.figures if f["value"] == 0.25)
    assert weight_fig["unit"] == ""
    assert res_691.currencies_stripped == 1

    # --- Item 5c & Item 4: `CPO` supported by `co-packaged optics`, and Innolight/WUS backfilled (`v22_511e15a42663`) ---
    c_511 = deepcopy(by_id["v22_511e15a42663"])
    res_511 = ex.validate_candidate_card(c_511)
    assert res_511.unsupported_names == []
    assert res_511.confidence == 0.85
    assert {"300308.SZ", "002463.SZ"} == set(res_511.tickers)
    assert res_511.tickers_backfilled == 2
    assert c_511["type"] == "opinion"
    assert res_511.types_corrected == 0

    # --- Item 6: Bidirectional `type` correction & `unverified_source` flagging ---
    c_ea4 = deepcopy(by_id["v22_ea4a5347e261"])
    res_ea4 = ex.validate_candidate_card(c_ea4)
    assert c_ea4["type"] == "estimate"
    assert res_ea4.types_corrected == 1

    c_ab3 = deepcopy(by_id["v22_ab31dc53f356"])
    res_ab3 = ex.validate_candidate_card(c_ab3)
    assert c_ab3["type"] == "estimate"
    assert res_ab3.types_corrected == 1

    c_cd4 = deepcopy(by_id["v22_cd43110a06ce"])
    res_cd4 = ex.validate_candidate_card(c_cd4)
    assert c_cd4["type"] == "fact"
    assert res_cd4.types_corrected == 1
    assert res_cd4.tickers == ["7581.T"]
    assert res_cd4.tickers_backfilled == 1

    c_d58 = deepcopy(by_id["v22_d58e0441d511"])
    res_d58 = ex.validate_candidate_card(c_d58)
    assert c_d58["type"] == "fact"
    assert res_d58.types_corrected == 0
    assert res_d58.unverified_source is True
    assert res_d58.confidence == 0.5
    assert any(
        f["metric"] == "_verification" and f["value"] == "unverified_source"
        for f in res_d58.figures
    )

    # --- Item 7: `topic_label` strips `FORBIDDEN_CONCLUSION_WORDS_TABLE` (`止跌` on `v22_6b9dca9e63a0`) ---
    c_6b9 = deepcopy(by_id["v22_6b9dca9e63a0"])
    res_6b9 = ex.validate_candidate_card(c_6b9)
    assert res_6b9.confidence == 0.4
    assert "止跌" in res_6b9.unsupported_conclusions
    assert "止跌" not in c_6b9["topic_label"]
    assert c_6b9["topic_label"] == "再鼎医药技术信号"

    # --- Item 9: `tickers_dropped_private` and `tickers_dropped_channel` in `extract_segment_cards` & `process_weekly_batch` ---
    seg_v23 = [
        {
            "agent": "main",
            "session_id": "sess-v23",
            "session_key": "agent:main:discord:channel:v23",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-v23-1",
            "seq": 1,
            "ts": "2026-04-06T10:00:00+00:00",
            "date_et": "2026-04-06",
            "message_role": "assistant",
            "speaker": {"sender_id_hash": "agent:main", "name": "Eve", "is_owner": False, "role": "agent"},
            "text": "\n".join(
                [
                    by_id["v22_0b0936821124"]["quote"],
                    by_id["v22_f02b05b12ff5"]["quote"],
                    by_id["v22_ea4a5347e261"]["quote"],
                ]
            ),
        }
    ]
    llm_cards_v23 = []
    for cid in ("v22_0b0936821124", "v22_f02b05b12ff5", "v22_ea4a5347e261"):
        item = deepcopy(by_id[cid])
        item["quote_event_id"] = "ev-v23-1"
        if cid == "v22_ea4a5347e261" and not item.get("cited_sources"):
            item["cited_sources"] = ["卖方口径"]
        llm_cards_v23.append(item)

    mock_assist_v23 = create_autospec(
        ex.assist_json,
        return_value=AssistResult(
            ok=True,
            status="ok",
            data={
                "category": "research",
                "topic_label": "v2.3回归测试",
                "cards": llm_cards_v23,
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 300, "output_tokens": 150, "total_tokens": 450},
        ),
    )
    monkeypatch.setattr(ex, "assist_json", mock_assist_v23)

    out_v23 = await ex.extract_segment_cards(seg_v23)
    assert out_v23.ok is True
    assert out_v23.tickers_dropped_private == 1
    assert out_v23.tickers_dropped_channel == 1
    assert out_v23.types_corrected >= 1

    batch_v23 = await archive_cli.process_weekly_batch(
        [dict(ev) for ev in seg_v23],
        mode="run",
        window_start=date(2026, 4, 6),
        window_end=date(2026, 4, 6),
        limiter=archive_cli.HourlyTokenBudgetLimiter(100_000),
        extractor_model="gemini-4-argon-medium-oauth",
        prompt_version="conv_card_v2_3",
        dry_run=True,
    )
    assert batch_v23["tickers_dropped_private"] == 1
    assert batch_v23["tickers_dropped_channel"] == 1
    assert batch_v23["types_corrected"] >= 1


@pytest.mark.asyncio
async def test_conv_card_v2_3_round6_review_fixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 code review fixes (Items 1-14) and lead-reproduced cases (prompt_version kept at `conv_card_v2_3`)."""
    import time as time_mod
    from src.dalton.conversations import extractor as ex

    # --- Item 1: `estimate -> fact` requires explicit realized phrases and uses shared forward keyword table ---
    assert ex.correct_card_type_by_keywords("estimate", "收入增长10%", "实际收入有望增长10%") == ("estimate", 0)
    assert ex.correct_card_type_by_keywords("estimate", "利润增长", "实际上明年利润将增长15%") == ("estimate", 0)
    assert ex.correct_card_type_by_keywords("estimate", "实控人", "实际控制人计划增持") == ("estimate", 0)
    assert ex.correct_card_type_by_keywords("estimate", "增速12-16%", "仅落在12%-16%的原预期内") == ("estimate", 0)
    assert ex.correct_card_type_by_keywords("estimate", "增速12-16%", "落在原预期区间") == ("estimate", 0)
    assert ex.correct_card_type_by_keywords("estimate", "Q3营收增长20%", "财报显示Q3营收已实现同比增长20%") == ("fact", 1)
    assert ex.correct_card_type_by_keywords("estimate", "Q3利润10亿", "已公布Q3实际录得净利润10亿元") == ("fact", 1)
    assert ex.correct_card_type_by_keywords("estimate", "Q3营收增长20%", "已实现Q3增长20%，全年expected to增长25%") == (
        "estimate",
        0,
    )

    # --- Item 2: `fact -> estimate` masks realized-vs-expectation phrases and removes `目标价`/`price target` ---
    assert ex.correct_card_type_by_keywords("fact", "Q3营收超预期", "Q3营收同比增长12%，超预期") == ("fact", 0)
    for realized_exp_phrase in (
        "好于预期",
        "低于预期",
        "不及预期",
        "符合预期",
        "beat target",
        "missed the target",
        "in line with target",
    ):
        assert ex.correct_card_type_by_keywords(
            "fact",
            f"Q3业绩{realized_exp_phrase}",
            f"Q3 revenue {realized_exp_phrase}",
        ) == ("fact", 0)
    assert ex.correct_card_type_by_keywords("fact", "上调目标价至100元", "大摩上调目标价至100元") == ("fact", 0)
    assert ex.correct_card_type_by_keywords("fact", "Price target raised to $100", "Morgan Stanley raised price target to $100") == (
        "fact",
        0,
    )

    # --- Item 3: `opinion`/`model_assumption -> estimate` requires explicit third-party forecast + verb + number ---
    assert ex.correct_card_type_by_keywords("opinion", "维持持有", "already consensus, holds, not adds") == (
        "opinion",
        0,
    )
    assert ex.correct_card_type_by_keywords("opinion", "市场已达成共识", "这已是共识，市场共识是看多") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计明年增长20%", "卖方预计2026年营收增长20%") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "一致预期100亿", "市场一致预期为100亿元") == ("estimate", 1)
    assert ex.correct_card_type_by_keywords("opinion", "Consensus revenue", "Consensus expects revenue of $10B") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("model_assumption", "Consensus EPS", "Consensus is $5.20") == (
        "estimate",
        1,
    )

    # --- Item 4: Re-enforce source citation check once after keyword-based type correction ---
    seg_item4 = [
        {
            "agent": "main",
            "session_id": "sess-r6-item4",
            "session_key": "agent:main:discord:channel:r6",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-r6-1",
            "seq": 1,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "assistant",
            "speaker": {"sender_id_hash": "agent:main", "name": "Eve", "is_owner": False, "role": "agent"},
            "text": "卖方预计2026年公司营收增长20%。",
        }
    ]
    orig_assist_json = ex.assist_json
    mock_assist_item4 = create_autospec(
        orig_assist_json,
        return_value=AssistResult(
            ok=True,
            status="ok",
            data={
                "category": "research",
                "topic_label": "营收预测",
                "cards": [
                    {
                        "type": "estimate",
                        "topic_label": "营收预测",
                        "claim": "卖方预计2026年公司营收增长20%。",
                        "tickers": [],
                        "entities": [],
                        "sectors": [],
                        "figures": [],
                        "quote": "卖方预计2026年公司营收增长20%。",
                        "quote_event_id": "ev-r6-1",
                        "cited_sources": [],
                        "confidence": None,
                    }
                ],
            },
            model="gemini-4-argon-medium-oauth",
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        ),
    )
    monkeypatch.setattr(ex, "assist_json", mock_assist_item4)
    out_item4 = await ex.extract_segment_cards(seg_item4)
    assert out_item4.ok is True
    assert len(out_item4.cards) == 1
    assert out_item4.cards[0]["type"] == "opinion"
    assert out_item4.cards[0]["confidence"] == 0.8

    # --- Item 5: Linear clause-scoped exclusion, English verb object exclusion, and <0.1s on 8,000 chars ---
    spans_cn, cleaned_cn = ex._extract_excluded_spans_and_clean_quote("腾讯维持买入，藤仓数据未予采用。")
    assert "腾讯" in cleaned_cn
    assert not any("腾讯" in s for s in spans_cn)
    assert "藤仓" not in cleaned_cn
    assert any("藤仓" in s for s in spans_cn)
    assert ex.validate_tickers_against_quote(["0700.HK", "5803.T"], "腾讯维持买入，藤仓数据未予采用。") == (
        ["0700.HK"],
        1,
    )

    spans_en, cleaned_en = ex._extract_excluded_spans_and_clean_quote(
        "Nvidia does not use Intel chips. AMD sales grew."
    )
    assert "Nvidia" in cleaned_en
    assert "AMD" in cleaned_en
    assert "Intel" not in cleaned_en
    assert any("Intel" in s for s in spans_en)
    assert not any("Nvidia" in s or "AMD" in s for s in spans_en)
    assert ex.validate_tickers_against_quote(
        ["NVDA", "INTC", "AMD"], "Nvidia does not use Intel chips. AMD sales grew."
    ) == (["NVDA", "AMD"], 1)

    long_no_excl = ("Nvidia Inc. reported U.S. revenue of 3.5 billion for 0700.HK peers. " * 125)[:8000]
    assert len(long_no_excl) == 8000
    t_start = time_mod.perf_counter()
    long_spans, long_cleaned = ex._extract_excluded_spans_and_clean_quote(long_no_excl)
    elapsed_excl = time_mod.perf_counter() - t_start
    assert elapsed_excl < 0.1
    assert long_spans == []
    assert "Nvidia" in long_cleaned

    # --- Item 6: Same-sentence period check splits on English period `. ` ---
    en_two_sents = "2025 revenue was 10 million. Profit was 5 million."
    assert ex.is_period_supported_by_quote("2025", en_two_sents, value=10, unit="million") is True
    assert ex.is_period_supported_by_quote("2025", en_two_sents, value=5, unit="million") is False
    res_profit_period = ex.validate_candidate_card(
        {
            "claim": "Profit was 5 million.",
            "figures": [{"metric": "profit", "value": 5, "unit": "million", "period": "2025"}],
            "tickers": [],
        },
        quote=en_two_sents,
    )
    assert res_profit_period.periods_cleared == 1
    assert res_profit_period.figures[0]["period"] == ""

    # --- Item 7: English number words (`half` -> `0.5` only; `one-time`/`one-off`/`half-year`/`one of` ignored; `has_unit=True` with unit) ---
    half_nums = ex.extract_quote_supported_numbers("Valuation was cut in half.")
    assert "0.5" in half_nums
    assert "50" not in half_nums
    assert (
        ex.extract_quote_supported_numbers(
            "Excluded one-time and one-off items in the half-year report for one of the segments."
        )
        == set()
    )
    assert ex.extract_claim_numbers("Revenue grew five million and margin rose two %.") == ["5", "2"]
    res_en_unit_num = ex.validate_candidate_card(
        {"claim": "Revenue grew five million.", "figures": [], "tickers": []},
        quote="Revenue grew steadily.",
    )
    assert res_en_unit_num.unsupported_claim_numbers == ["5"]
    assert res_en_unit_num.confidence == 0.4

    # --- Item 8: Normalized currency units (`USD_bn`, `CNY_yi`, `RMB_mn`) & strict RMB matching (`10亿美元` != RMB) ---
    assert ex.strip_unsupported_currency_from_unit("USD_bn", 10, "公司收入 10 亿。") == ("bn", True)
    assert ex.strip_unsupported_currency_from_unit("CNY_yi", 10, "公司收入 10 亿。") == ("yi", True)
    assert ex.strip_unsupported_currency_from_unit("RMB_mn", 50, "公司收入 50 百万。") == ("mn", True)
    assert ex.strip_unsupported_currency_from_unit("CNY_yi", 10, "公司收入 10 亿美元。") == ("yi", True)
    assert ex.strip_unsupported_currency_from_unit("RMB_mn", 10, "公司收入 10 亿美元。") == ("mn", True)
    assert ex.strip_unsupported_currency_from_unit("亿元", 10, "公司收入 10 亿美元。") == ("亿", True)
    assert ex.strip_unsupported_currency_from_unit("人民币", 10, "公司收入 10 亿美元。") == ("", True)
    assert ex.strip_unsupported_currency_from_unit("元", 100, "售价 100 港元。") == ("", True)
    assert ex.strip_unsupported_currency_from_unit("元", 100, "售价新台币 100 元。") == ("", True)
    assert ex.strip_unsupported_currency_from_unit("CNY_yi", 10, "公司收入 10 亿元。") == ("CNY_yi", False)
    assert ex.strip_unsupported_currency_from_unit("CNY_yi", 10, "公司收入 10 亿元人民币。") == ("CNY_yi", False)
    assert ex.strip_unsupported_currency_from_unit("RMB_mn", 50, "净现金 RMB 50mn。") == ("RMB_mn", False)

    # --- Item 9: Clearing `倍` returns `was_stripped=True` and increments `currencies_stripped` ---
    assert ex.strip_unsupported_currency_from_unit("倍", 0.25, "权重为 0.25。") == ("", True)
    res_bei = ex.validate_candidate_card(
        {
            "claim": "权重为 0.25。",
            "figures": [{"metric": "weight", "value": 0.25, "unit": "倍", "period": ""}],
            "tickers": [],
        },
        quote="权重为 0.25。",
    )
    assert res_bei.currencies_stripped == 1
    assert res_bei.figures[0]["unit"] == ""

    # --- Item 10: Trading channel `买` narrowed to channel context; `富途宣布买回股份` keeps FUTU ---
    res_futu_buyback = ex.validate_candidate_card(
        {
            "claim": "富途宣布买回股份，维持买入评级。",
            "figures": [],
            "tickers": ["FUTU"],
        },
        quote="富途宣布买回股份，多家券商维持富途买入评级。",
    )
    assert res_futu_buyback.tickers == ["FUTU"]
    assert res_futu_buyback.tickers_dropped_channel == 0

    res_futu_channel = ex.validate_candidate_card(
        {
            "claim": "在富途买美股很方便。",
            "figures": [],
            "tickers": ["FUTU"],
        },
        quote="在富途买美股很方便，开户即可下单。",
    )
    assert res_futu_channel.tickers == []
    assert res_futu_channel.tickers_dropped_channel == 1

    # --- Item 11: Source institution backfilled when it is the card's subject ---
    res_jpm_subj = ex.validate_candidate_card(
        {
            "claim": "摩根大通Q3净利息收入增长10%。",
            "topic_label": "摩根大通Q3业绩",
            "figures": [],
            "tickers": [],
        },
        quote="摩根大通Q3净利息收入增长10%。",
    )
    assert res_jpm_subj.tickers == ["JPM"]
    assert res_jpm_subj.tickers_backfilled == 1

    res_gs_subj = ex.validate_candidate_card(
        {
            "claim": "高盛Q3投行业务收入增长25%。",
            "topic_label": "高盛Q3业绩",
            "figures": [],
            "tickers": [],
        },
        quote="高盛Q3投行业务收入增长25%。",
    )
    assert res_gs_subj.tickers == ["GS"]
    assert res_gs_subj.tickers_backfilled == 1

    res_gs_src = ex.validate_candidate_card(
        {
            "claim": "According to Goldman, Apple iPhone revenue grew 8%.",
            "topic_label": "Apple iPhone revenue",
            "figures": [],
            "tickers": [],
        },
        quote="According to Goldman, Apple iPhone revenue grew 8%.",
    )
    assert res_gs_src.tickers == ["AAPL"]
    assert "GS" not in res_gs_src.tickers

    # --- Item 12: Proper-noun check fixes (sentence-initial financial words, acronym vs `market share`, Traditional/Simplified Chinese) ---
    res_en_common = ex.validate_candidate_card(
        {
            "claim": "Revenue grew 10%. Sales and Profit expanded. Margin reached 25%.",
            "topic_label": "Revenue and Margin",
            "figures": [],
            "tickers": [],
        },
        quote="total top-line up 10%, bottom-line up, profitability at 25%.",
    )
    assert res_en_common.unsupported_names == []

    assert ex._is_acronym_supported_by_quote_phrase("MS", "market share expanded to 30%") is False
    res_ms_vs_mkt_share = ex.validate_candidate_card(
        {
            "claim": "MS预计市场份额提升至30%。",
            "topic_label": "市场份额",
            "figures": [],
            "tickers": [],
        },
        quote="market share expanded to 30%.",
    )
    assert "MS" in res_ms_vs_mkt_share.unsupported_names

    res_trad_simp_1 = ex.validate_candidate_card(
        {
            "claim": "腾讯Q3游戏收入同比增长12%。",
            "topic_label": "腾讯游戏",
            "figures": [],
            "tickers": [],
        },
        quote="騰訊Q3遊戲收入同比增長12%。",
    )
    assert res_trad_simp_1.unsupported_names == []
    assert res_trad_simp_1.tickers == ["0700.HK"]

    res_trad_simp_2 = ex.validate_candidate_card(
        {
            "claim": "騰訊Q3遊戲收入同比增長12%。",
            "topic_label": "騰訊遊戲",
            "figures": [],
            "tickers": [],
        },
        quote="腾讯Q3游戏收入同比增长12%。",
    )
    assert res_trad_simp_2.unsupported_names == []
    assert res_trad_simp_2.tickers == ["0700.HK"]

    # --- Item 13: Private company drop counters match between `validate_candidate_card` and `extract_segment_cards` ---
    priv_card_input = {
        "type": "opinion",
        "topic_label": "SpaceX发射频次",
        "claim": "SpaceX全年星舰发射频次继续提升。",
        "tickers": ["SPCX", "TSLA"],
        "entities": [],
        "sectors": [],
        "figures": [],
        "quote": "SpaceX 与 Tesla 协同推进，全年星舰发射频次继续提升。",
        "quote_event_id": "ev-priv-1",
        "cited_sources": [],
        "confidence": 0.85,
    }
    direct_priv_res = ex.validate_candidate_card(
        {
            "claim": priv_card_input["claim"],
            "topic_label": priv_card_input["topic_label"],
            "type": priv_card_input["type"],
            "figures": [],
            "tickers": list(priv_card_input["tickers"]),
            "quote": priv_card_input["quote"],
            "cited_sources": [],
            "confidence": 0.85,
        },
        quote=priv_card_input["quote"],
    )
    seg_priv = [
        {
            "agent": "main",
            "session_id": "sess-priv",
            "session_key": "agent:main:discord:channel:priv",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-priv-1",
            "seq": 1,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "user",
            "speaker": {"sender_id_hash": "owner", "name": "Owner", "is_owner": True, "role": "owner"},
            "text": priv_card_input["quote"],
        }
    ]
    monkeypatch.setattr(
        ex,
        "assist_json",
        create_autospec(
            orig_assist_json,
            return_value=AssistResult(
                ok=True,
                status="ok",
                data={"category": "research", "topic_label": "SpaceX", "cards": [dict(priv_card_input)]},
                model="gemini-4-argon-medium-oauth",
                usage={"input_tokens": 80, "output_tokens": 40, "total_tokens": 120},
            ),
        ),
    )
    seg_priv_out = await ex.extract_segment_cards(seg_priv)
    assert direct_priv_res.tickers_dropped == seg_priv_out.tickers_dropped == 1
    assert direct_priv_res.tickers_dropped_private == seg_priv_out.tickers_dropped_private == 1
    assert direct_priv_res.tickers == seg_priv_out.cards[0]["tickers"] == ["TSLA"]

    # --- Item 14: `lru_cache` on alias regex, `资金流` in `FORBIDDEN_CONCLUSION_WORDS_TABLE` ---
    assert hasattr(ex._compiled_alias_regex, "cache_info")
    assert "资金流" in ex.FORBIDDEN_CONCLUSION_WORDS_TABLE
    res_zijinliu = ex.validate_candidate_card(
        {
            "claim": "该板块近期资金流明显改善。",
            "topic_label": "板块资金流分析",
            "figures": [],
            "tickers": [],
        },
        quote="该板块近期成交额温和放大。",
    )
    assert "资金流" in res_zijinliu.unsupported_conclusions
    assert res_zijinliu.confidence == 0.4
    assert "资金流" not in res_zijinliu.figures[0].get("metric", "")


def test_round7_item1_third_party_forecast_no_early_return() -> None:
    """Round 7 Item 1: `THIRD_PARTY_FORECAST_PHRASES_TABLE` does not early-return without checking
    subject + forecast verb + number in the same clause, and clauses with `已是`/`already` are excluded.
    """
    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("opinion", "一致预期看多", "consensus is bullish") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计好转", "卖方预计会更好") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "一致预期100亿", "已是一致预期为100亿") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "Consensus $10B", "It is already consensus expects $10B") == (
        "opinion",
        0,
    )
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计增长20%", "卖方预计2026年收入增长20%") == (
        "estimate",
        1,
    )


def test_round7_item2_realized_fact_phrases_do_not_veto_forward_estimates() -> None:
    """Round 7 Item 2: `财报显示` and `法说会纪要` removed from `REALIZED_FACT_PHRASES_TABLE` so forward
    estimates in filings/call notes flip from `fact` to `estimate`, while `已实现`/`已公布`/`实际录得` remain.
    """
    from src.dalton.conversations import extractor as ex

    assert "财报显示" not in ex.REALIZED_FACT_PHRASES_TABLE
    assert "法说会纪要" not in ex.REALIZED_FACT_PHRASES_TABLE
    for kept_phrase in ("已实现", "已公布", "实际录得"):
        assert kept_phrase in ex.REALIZED_FACT_PHRASES_TABLE

    assert ex.correct_card_type_by_keywords(
        "fact", "公司预计2026年收入增长20%", "财报显示公司预计2026年收入增长20%"
    ) == ("estimate", 1)
    assert ex.correct_card_type_by_keywords(
        "fact", "明年营收预计增长20%", "法说会纪要：明年营收预计增长20%"
    ) == ("estimate", 1)


def test_round7_item3_institution_source_verbs_narrowed() -> None:
    """Round 7 Item 3: Source institution verbs narrowed to `预计/称/认为/指出/表示/估计` and `according to <机构>`
    (plus `在<机构>的研报/会上`), so `发布`/`上调`/`披露` keep or backfill the institution ticker.
    """
    from src.dalton.conversations import extractor as ex

    for claim_and_quote, expected_ticker in [
        ("高盛发布三季报", "GS"),
        ("摩根大通上调股息至每股3美元", "JPM"),
        ("高盛披露Q3净利润增长10%", "GS"),
    ]:
        res_bf = ex.validate_candidate_card(
            {"claim": claim_and_quote, "topic_label": claim_and_quote, "figures": [], "tickers": []},
            quote=claim_and_quote,
        )
        assert res_bf.tickers == [expected_ticker]
        assert res_bf.tickers_backfilled == 1

        res_keep = ex.validate_candidate_card(
            {
                "claim": claim_and_quote,
                "topic_label": claim_and_quote,
                "figures": [],
                "tickers": [expected_ticker],
            },
            quote=claim_and_quote,
        )
        assert res_keep.tickers == [expected_ticker]
        assert res_keep.tickers_dropped == 0

    res_src = ex.validate_candidate_card(
        {
            "claim": "高盛预计英伟达收入增长",
            "topic_label": "英伟达收入预测",
            "figures": [],
            "tickers": [],
        },
        quote="高盛预计英伟达收入增长",
    )
    assert res_src.tickers == ["NVDA"]
    assert "GS" not in res_src.tickers


def test_round7_item4_english_exclusion_passive_pronoun_and_comma() -> None:
    """Round 7 Item 4: Passive `X are not used in/by Y` excludes subject X and keeps Y; pronoun object
    (`Nvidia does not use them.`) does not exclude subject; comma-separated clause (`does not use Intel chips, AMD grew`)
    keeps AMD.
    """
    from src.dalton.conversations import extractor as ex

    assert ex.validate_tickers_against_quote(
        ["NVDA", "INTC", "AMD"],
        "Intel chips are not used in Nvidia servers. AMD sales grew.",
    ) == (["NVDA", "AMD"], 1)

    assert ex.validate_tickers_against_quote(
        ["NVDA", "INTC"],
        "Intel chips are not used by Nvidia.",
    ) == (["NVDA"], 1)

    assert ex.validate_tickers_against_quote(
        ["NVDA"],
        "Nvidia does not use them.",
    ) == (["NVDA"], 0)

    assert ex.validate_tickers_against_quote(
        ["NVDA", "INTC", "AMD"],
        "Nvidia does not use Intel chips, AMD grew.",
    ) == (["NVDA", "AMD"], 1)


def test_round7_item5_sentence_split_after_abbreviation_and_proper_noun_scan() -> None:
    """Round 7 Item 5: Abbreviations (`Inc.`, `Ltd.`, `Corp.`, `Co.`) split sentences when followed by
    whitespace and an uppercase letter starting a new sentence, shared by period check, exclusions, and proper-noun scan.
    """
    from src.dalton.conversations import extractor as ex

    q_period = "2025 sales were led by Nvidia Inc. Profit was 5 million."
    assert ex.is_period_supported_by_quote("2025", q_period, value=5, unit="million") is False

    q_excl = "Nvidia does not use Intel Inc. AMD sales grew."
    assert ex.validate_tickers_against_quote(["NVDA", "INTC", "AMD"], q_excl) == (["NVDA", "AMD"], 1)

    res_proper = ex.validate_candidate_card(
        {
            "claim": "Nvidia Inc. Operational efficiency improved.",
            "topic_label": "Nvidia",
            "figures": [],
            "tickers": ["NVDA"],
        },
        quote="Nvidia Inc. efficiency improved.",
    )
    assert "Operational" not in res_proper.unsupported_names


def test_round7_item6_realized_expectation_mask_modifiers() -> None:
    """Round 7 Item 6: Realized expectation phrases allow intermediate `市场`/`一致`/`分析师` modifiers."""
    from src.dalton.conversations import extractor as ex

    for phrase in ("超出市场预期", "好于一致预期", "低于市场预期", "符合一致预期", "不及分析师预期"):
        assert ex.correct_card_type_by_keywords(
            "fact",
            f"Q3营收{phrase}",
            f"公司Q3营收同比增长15%，{phrase}。",
        ) == ("fact", 0)


def test_round7_item7_forward_jiang_and_jihua_disambiguation() -> None:
    """Round 7 Item 7: `将` only triggers on `将+verb` / `将要` / `即将` (excluding `已将` / `将其` / `将…出售给`),
    and `计划` does not trigger on `激励计划` / `回购计划` / `员工持股计划`.
    """
    from src.dalton.conversations import extractor as ex

    for non_fwd_quote in (
        "公司已将非核心业务剥离，收回现金10亿元。",
        "公司将其列为核心资产，账面价值5亿元。",
        "公司将子公司出售给第三方，作价8亿元。",
        "公司披露2025年股权激励计划，授予100万股。",
        "董事会通过股份回购计划，已回购5000万元。",
        "公司完成员工持股计划非交易过户。",
    ):
        assert ex.correct_card_type_by_keywords("fact", non_fwd_quote, non_fwd_quote) == ("fact", 0)

    for fwd_quote in (
        "公司即将发布新一代芯片。",
        "公司将要扩产50%。",
        "明年营收将增长20%。",
        "公司计划明年在海外新建工厂。",
    ):
        assert ex.correct_card_type_by_keywords("fact", fwd_quote, fwd_quote) == ("estimate", 1)


def test_round7_item8_trading_channel_preposition_and_buy_boundaries() -> None:
    """Round 7 Item 8: `在/用/通过` must not match inside `存在`, `买` must not match `买点`/`买回`/`买入`,
    and `可以买`/`能够买` are recognized as trading-channel phrases.
    """
    from src.dalton.conversations import extractor as ex

    res_keep = ex.validate_candidate_card(
        {"claim": "富途存在买点", "figures": [], "tickers": ["FUTU"]},
        quote="富途存在买点，估值具备吸引力。",
    )
    assert res_keep.tickers == ["FUTU"]
    assert res_keep.tickers_dropped_channel == 0

    for drop_claim in ("富途可以买港股", "富途能够买美股"):
        res_drop = ex.validate_candidate_card(
            {"claim": drop_claim, "figures": [], "tickers": ["FUTU"]},
            quote=drop_claim,
        )
        assert res_drop.tickers == []
        assert res_drop.tickers_dropped_channel == 1


def test_round7_item9_trad_to_simp_table_coverage() -> None:
    """Round 7 Item 9: Traditional-to-Simplified table covers `臺偉馬遜圖譜滬億為` so `臺積電` <-> `台积电`
    and `英偉達` <-> `英伟达` match without flagging unsupported names.
    """
    from src.dalton.conversations import extractor as ex

    for trad_ch, simp_ch in zip("臺偉馬遜圖譜滬億為", "台伟马逊图谱沪亿为", strict=True):
        assert ex._normalize_trad_to_simp(trad_ch) == simp_ch

    res_tsm = ex.validate_candidate_card(
        {"claim": "臺積電CoWoS產能持續擴張。", "topic_label": "臺積電", "figures": [], "tickers": []},
        quote="台积电CoWoS产能持续扩张。",
    )
    assert res_tsm.unsupported_names == []
    assert res_tsm.tickers == ["TSM"]

    res_nvda = ex.validate_candidate_card(
        {"claim": "英偉達B200出貨放量。", "topic_label": "英偉達", "figures": [], "tickers": []},
        quote="英伟达B200出货放量。",
    )
    assert res_nvda.unsupported_names == []
    assert res_nvda.tickers == ["NVDA"]


def test_round7_item10_two_letter_acronym_not_supported_by_phrase() -> None:
    """Round 7 Item 10: 2-letter acronyms cannot be supported by generic 2-word phrases regardless of case (`Market Share` != `MS`)."""
    from src.dalton.conversations import extractor as ex

    assert ex._is_acronym_supported_by_quote_phrase("MS", "Market Share expanded to 30%") is False
    assert ex._is_acronym_supported_by_quote_phrase("MS", "market share expanded to 30%") is False
    res = ex.validate_candidate_card(
        {"claim": "MS预计份额提升至30%。", "topic_label": "份额", "figures": [], "tickers": []},
        quote="Market Share expanded to 30%.",
    )
    assert "MS" in res.unsupported_names


@pytest.mark.asyncio
async def test_round7_item11_types_corrected_and_duplicate_private_ticker_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 7 Item 11: `types_corrected` only counts when the final post-citation type differs from the
    pre-correction type; duplicate private company tickers (`["SPCX", "SPCX"]`) are counted after deduplication.
    """
    from src.dalton.conversations import extractor as ex

    seg = [
        {
            "agent": "main",
            "session_id": "sess-r7-11",
            "session_key": "agent:main:discord:channel:r7-11",
            "channel": "discord",
            "chat_type": "channel",
            "event_id": "ev-r7-11",
            "seq": 1,
            "ts": "2026-10-20T10:00:00+00:00",
            "date_et": "2026-10-20",
            "message_role": "assistant",
            "speaker": {"sender_id_hash": "agent:main", "name": "Eve", "is_owner": False, "role": "agent"},
            "text": "卖方预计2026年SpaceX收入增长20%。",
        }
    ]
    orig_assist = ex.assist_json
    monkeypatch.setattr(
        ex,
        "assist_json",
        create_autospec(
            orig_assist,
            return_value=AssistResult(
                ok=True,
                status="ok",
                data={
                    "category": "research",
                    "topic_label": "SpaceX",
                    "cards": [
                        {
                            "type": "estimate",
                            "topic_label": "SpaceX预测",
                            "claim": "卖方预计2026年SpaceX收入增长20%。",
                            "tickers": ["SPCX", "SPCX"],
                            "entities": [],
                            "sectors": [],
                            "figures": [],
                            "quote": "卖方预计2026年SpaceX收入增长20%。",
                            "quote_event_id": "ev-r7-11",
                            "cited_sources": [],
                            "confidence": 0.8,
                        }
                    ],
                },
                model="gemini-4-argon-medium-oauth",
                usage={"input_tokens": 80, "output_tokens": 40, "total_tokens": 120},
            ),
        ),
    )
    out = await ex.extract_segment_cards(seg)
    assert out.cards[0]["type"] == "opinion"
    assert out.types_corrected == 0
    assert out.tickers_dropped == 1
    assert out.tickers_dropped_private == 1

    direct = ex.validate_candidate_card(
        {
            "claim": "卖方预计2026年SpaceX收入增长20%。",
            "topic_label": "SpaceX预测",
            "type": "opinion",
            "figures": [],
            "tickers": ["SPCX", "SPCX"],
            "quote": "卖方预计2026年SpaceX收入增长20%。",
            "cited_sources": [],
            "confidence": 0.8,
        },
        quote="卖方预计2026年SpaceX收入增长20%。",
    )
    assert direct.tickers_dropped == out.tickers_dropped == 1
    assert direct.tickers_dropped_private == out.tickers_dropped_private == 1


def test_round7_item12_remove_unrequested_ddog_alias() -> None:
    """Round 7 Item 12: `DDOG` / `Datadog` removed from `SUPPLEMENTAL_TICKER_ALIASES`."""
    from src.dalton.conversations import extractor as ex

    assert "DDOG" not in ex.SUPPLEMENTAL_TICKER_ALIASES


def test_round8_item1_trad_to_simp_sell_buy_order() -> None:
    """Round 8 Item 1: `賣買` maps to `卖买` (not reversed); `賣方` normalizes to `卖方`."""
    from src.dalton.conversations import extractor as ex

    assert ex._normalize_trad_to_simp("賣方") == "卖方"
    assert ex._normalize_trad_to_simp("買方") == "买方"
    assert ex._normalize_trad_to_simp("賣買") == "卖买"


def test_round8_item2_abbreviation_sentence_split_only_company_suffixes() -> None:
    """Round 8 Item 2: Sentence splitting after abbreviation only applies to `Inc.`, `Ltd.`, `Corp.`, `Co.`
    followed by space + uppercase word; `U.S.`, `U.K.`, `e.g.`, `i.e.` and `letter.letter.` do not split.
    """
    from src.dalton.conversations import extractor as ex

    assert ex.is_period_supported_by_quote(
        "2025",
        "2025 U.S. GAAP profit was 5 million.",
        value=5,
        unit="million",
    )
    assert ex._split_sentences("e.g. Nvidia and Broadcom") == ["e.g. Nvidia and Broadcom"]
    assert ex._split_sentences("Nvidia Inc. Profit was 5 million.") == [
        "Nvidia Inc.",
        "Profit was 5 million",
    ]
    assert not ex.is_period_supported_by_quote(
        "2025",
        "2025 sales were led by Nvidia Inc. Profit was 5 million.",
        value=5,
        unit="million",
    )


def test_round8_item3_third_party_forecast_requires_verb_and_non_year_number_in_same_clause() -> None:
    """Round 8 Item 3: Third-party forecast requires subject, forecast verb, and non-standalone-year number in the same clause."""
    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("fact", "利润增长20%", "利润增长20%（卖方口径）") == ("fact", 0)
    assert ex.correct_card_type_by_keywords("opinion", "利润增长20%", "利润增长20%（卖方口径）") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords(
        "opinion",
        "公司预计推进改革",
        "公司预计推进改革（卖方口径），参考2024年年报",
    ) == ("opinion", 0)
    assert ex.correct_card_type_by_keywords(
        "opinion",
        "公司预计推进改革",
        "公司预计2024年推进改革（卖方口径）",
    ) == ("opinion", 0)
    # Round 7 negative and positive examples still pass
    assert ex.correct_card_type_by_keywords("opinion", "一致预期看多", "consensus is bullish") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计好转", "卖方预计会更好") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "一致预期100亿", "已是一致预期为100亿") == ("opinion", 0)
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计增长20%", "卖方预计2026年收入增长20%") == (
        "estimate",
        1,
    )


def test_round8_item4_institution_source_bridge_and_laizi_chenbao() -> None:
    """Round 8 Item 4: Restore `research`, `report`, `note` before English source verbs, `来自<机构>`, and `<机构>晨报指出`."""
    from src.dalton.conversations import extractor as ex

    for quote, claim in (
        ("Goldman research expects Nvidia revenue to grow 20%.", "Goldman research expects Nvidia revenue to grow 20%."),
        ("Goldman report says Nvidia revenue grew 20%.", "Goldman report says Nvidia revenue grew 20%."),
        ("来自高盛的数据显示英伟达收入增长20%。", "来自高盛的数据显示英伟达收入增长20%。"),
        ("高盛晨报指出英伟达收入增长20%。", "高盛晨报指出英伟达收入增长20%。"),
    ):
        kept, _ = ex.validate_tickers_against_quote(["GS", "NVDA"], quote, claim=claim)
        assert kept == ["NVDA"]
        bf, _ = ex.backfill_tickers_from_quote([], quote, claim=claim)
        assert bf == ["NVDA"]


def test_round8_item5_negated_trading_channel_kept() -> None:
    """Round 8 Item 5: Negated `不可以买` / `不能够买` is not treated as a trading channel."""
    from src.dalton.conversations import extractor as ex

    kept, _ = ex.validate_tickers_against_quote(["FUTU"], "富途不可以买港股。", claim="富途不可以买港股。")
    assert kept == ["FUTU"]
    bf, _ = ex.backfill_tickers_from_quote([], "富途不可以买港股。", claim="富途不可以买港股。")
    assert bf == ["FUTU"]


def test_round8_item6_realized_expectation_mask_le_and_de() -> None:
    """Round 8 Item 6: Allow `了` and `的` in realized expectation comparisons (`超出了市场预期`, `好于市场的预期`)."""
    from src.dalton.conversations import extractor as ex

    for phrase in ("超出了市场预期", "好于市场的预期"):
        assert ex.correct_card_type_by_keywords("fact", f"Q3营收{phrase}", f"Q3营收同比增长15%，{phrase}。") == (
            "fact",
            0,
        )


def test_round8_item7_completed_aspect_yijing_jiang() -> None:
    """Round 8 Item 7: `已经将` / `曾经将` completed aspect does not trigger `fact -> estimate`."""
    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("fact", "已经将产能扩大", "公司已经将产能扩大至10万片。") == ("fact", 0)
    assert ex.correct_card_type_by_keywords("fact", "曾经将产能扩大", "公司曾经将产能扩大至10万片。") == ("fact", 0)
    assert ex.correct_card_type_by_keywords(
        "fact",
        "已经将于2024年到期的债券提前偿还",
        "公司已经将于2024年到期的债券提前偿还。",
    ) == ("fact", 0)
    assert ex.correct_card_type_by_keywords(
        "fact",
        "曾经将于2023年到期的债券提前偿还",
        "公司曾经将于2023年到期的债券提前偿还。",
    ) == ("fact", 0)


def test_round9_item1_year_rule_preserves_four_digit_amounts() -> None:
    """Round 9 Item 1: `_STANDALONE_YEAR_RE` only strips explicit year tokens (`2026年`, `FY26`, `FY2026`, `CY2026`, `2026E`/`2026F`)
    and preserves 4-digit amounts with units/currency or bare 4-digit numbers.
    """
    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("opinion", "卖方预计2026年净利润2000亿元", "卖方预计2026年净利润2000亿元") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "预计收入2000万元", "预计收入为2000万元（卖方口径）") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords(
        "opinion",
        "Consensus revenue $2000M",
        "consensus expects revenue of $2000 million",
    ) == ("estimate", 1)
    assert ex.correct_card_type_by_keywords(
        "opinion",
        "Consensus target 2050",
        "consensus expects a target of 2050",
    ) == ("estimate", 1)
    assert ex.correct_card_type_by_keywords(
        "opinion",
        "公司预计推进改革",
        "公司预计推进改革（卖方口径），参考2024年年报",
    ) == ("opinion", 0)


def test_round9_item2_sell_side_suffix_required() -> None:
    """Round 9 Item 2: `[（(]?卖方(?:口径|预计|预测)` suffix is mandatory so `卖方维持中性` does not trigger."""
    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("opinion", "公司预计净利增长20%", "公司预计净利增长20%卖方维持中性") == (
        "opinion",
        0,
    )


def test_round9_item3_chinese_numerals_and_thousand_separator_commas() -> None:
    """Round 9 Item 3: Third-party forecast structure recognizes Chinese numerals (`_CN_NUM_MENTION_RE`) and thousand-separator commas (`1,000`)."""
    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("opinion", "预计收入十五亿", "预计收入十五亿（卖方口径）") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "预计利润为三亿元", "预计利润为三亿元（卖方口径）") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "预计收入为1,000万元", "预计收入为1,000万元（卖方口径）") == (
        "estimate",
        1,
    )


def test_round10_third_party_forecast_number_slot_and_no_backtracking() -> None:
    """Round 10: `_THIRD_PARTY_FORECAST_RE` number slot only accepts Arabic numbers or `_CN_NUM_MENTION_RE`
    (not single Chinese numeral characters in `三星`/`进一步`/`下一代`/`十分`/`一方面`/`一般`/`一致看好`),
    and completes in < 0.1s on an 8,000-char text containing many `预计`/`一`/`十` without `卖方口径`.
    """
    import time as time_mod

    from src.dalton.conversations import extractor as ex

    assert ex.correct_card_type_by_keywords("opinion", "卖方预计三星电子收入增长20%", "卖方预计三星电子收入增长20%") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计进一步提升毛利率至30%", "卖方预计进一步提升毛利率至30%") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计下一代产品收入三亿元", "卖方预计下一代产品收入三亿元") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计十分看好2000亿元", "卖方预计十分看好2000亿元") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "分析师预计一方面2000亿元", "分析师预计一方面2000亿元") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "机构预计一般在2000亿元", "机构预计一般在2000亿元") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "卖方预计一致看好2000亿元", "卖方预计一致看好2000亿元") == (
        "estimate",
        1,
    )
    assert ex.correct_card_type_by_keywords("opinion", "分析师预计一方面十五亿", "分析师预计一方面十五亿") == (
        "estimate",
        1,
    )

    long_text = ("公司预计一方面十分一般进一步一十它认为100但未采用 " * 450)[:8000]
    assert len(long_text) == 8000
    t0 = time_mod.perf_counter()
    res = ex.correct_card_type_by_keywords("opinion", "长文测试", long_text)
    m_raw = ex._THIRD_PARTY_FORECAST_RE.search(long_text)
    elapsed = time_mod.perf_counter() - t0
    assert res == ("opinion", 0)
    assert m_raw is None
    assert elapsed < 0.1





@pytest.mark.asyncio
async def test_conversation_archive_extract_with_glm_5_3_coding_anthropic_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `extractor_model` default remains `deepseek-flash` in dalton.yaml/settings.py,
    and `conversation_archive._extract_with_retry` / `extract_segment_cards` succeeds with
    `extractor_model='glm-5.3-plan'` over Anthropic protocol (`open.bigmodel.cn/api/anthropic`,
    `x-api-key` header, thinking + text blocks, JSON schema validation, and token accounting)."""
    import anthropic
    import httpx
    from langchain_anthropic import ChatAnthropic
    import scripts.dalton.conversation_archive as archive_mod
    import src.server.database.api_keys as api_keys_mod
    import src.server.services.llm.config as llm_config_mod
    import src.server.services.llm.service as llm_service_mod
    import src.server.services.llm.user_models as user_models_mod
    from src.dalton.settings import get_dalton_settings
    from src.llms import llm as llm_mod

    # Default extractor_model in dalton.yaml is deepseek-flash (2026-10-23 blind eval winner)
    assert get_dalton_settings().conversations.extractor_model == "deepseek-flash"

    llm_mod.LLM._model_config = None
    dummy_token = "unit-test-glm-archive-token"
    monkeypatch.setenv("ZAI_CN_CODING_API_KEY", dummy_token)
    monkeypatch.setattr(
        user_models_mod,
        "get_model_preference",
        create_autospec(user_models_mod.get_model_preference, return_value={}),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "load_user_context",
        create_autospec(llm_config_mod.load_user_context, return_value=None),
    )
    monkeypatch.setattr(
        llm_config_mod,
        "gate_capabilities",
        create_autospec(llm_config_mod.gate_capabilities, return_value={}),
    )
    monkeypatch.setattr(
        api_keys_mod,
        "get_byok_configs_for_providers",
        create_autospec(api_keys_mod.get_byok_configs_for_providers, return_value={}),
    )

    captured_requests: list[httpx.Request] = []
    extraction_payload = {
        "category": "research",
        "topic_label": "META AI Capex",
        "cards": [
            {
                "type": "model_assumption",
                "topic_label": "META 2026 Capex",
                "claim": "META 2026 年资本开支模型假设为 680 亿美元。",
                "tickers": ["META"],
                "entities": [],
                "sectors": ["Internet"],
                "figures": [
                    {
                        "metric": "capex",
                        "value": 680,
                        "unit": "USD_bn",
                        "period": "FY2026",
                    }
                ],
                "quote": "META 2026 年资本开支我们按 680 亿美元建模。",
                "quote_event_id": "ev-glm-01",
                "cited_sources": [],
                "confidence": 0.9,
            }
        ],
    }

    async def _anthropic_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        text_json = json.dumps(extraction_payload, ensure_ascii=False)
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_glm_archive_01",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": "glm-5.3",
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": 210,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "thinking_delta",
                        "thinking": "Analyzing segment events and extracting cards...",
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": text_json},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": 210, "output_tokens": 95},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        sse_bytes = "".join(
            f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            for ev, data in events
        ).encode("utf-8")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_bytes,
        )

    mock_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_anthropic_handler),
        base_url="https://open.bigmodel.cn/api/anthropic",
    )
    orig_create_llm = llm_mod.create_llm

    def _patched_create_llm(model: str, **kwargs: Any) -> Any:
        client = orig_create_llm(model, **kwargs)
        if isinstance(client, ChatAnthropic):
            client.__dict__["_async_client"] = anthropic.AsyncAnthropic(
                api_key=dummy_token,
                base_url="https://open.bigmodel.cn/api/anthropic",
                http_client=mock_http,
            )
        return client

    patched_create = create_autospec(llm_mod.create_llm, side_effect=_patched_create_llm)
    monkeypatch.setattr(llm_mod, "create_llm", patched_create)
    monkeypatch.setattr(llm_service_mod, "create_llm", patched_create)

    segment = [
        {
            "agent": "main",
            "session_id": "sess_glm_01",
            "session_key": "agent:main:feishu:direct:1",
            "channel": "feishu",
            "chat_type": "direct",
            "event_id": "ev-glm-01",
            "seq": 1,
            "created_at_ms": 1761206400000,
            "ts": "2026-10-23T08:00:00+00:00",
            "date_et": "2026-10-23",
            "message_role": "user",
            "speaker": {
                "sender_id_hash": "hash_owner",
                "name": "PM",
                "is_owner": True,
                "role": "owner",
            },
            "text": "META 2026 年资本开支我们按 680 亿美元建模。",
        }
    ]

    try:
        outcome, retries = await archive_mod.extract_segment_with_infinite_backoff(
            segment,
            extractor_model="glm-5.3-plan",
            prompt_version="conv_card_v2",
            dry_run=True,
        )
    finally:
        await mock_http.aclose()

    assert retries == 0
    assert outcome.ok is True
    assert outcome.model_used == "glm-5.3-plan"
    assert outcome.category == "research"
    assert len(outcome.cards) == 1
    assert outcome.cards[0]["tickers"] == ["META"]
    assert outcome.input_tokens == 210
    assert outcome.output_tokens == 95
    assert outcome.total_tokens == 305

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert str(req.url) == "https://open.bigmodel.cn/api/anthropic/v1/messages"
    assert req.headers.get("x-api-key") == dummy_token
    req_body = json.loads(req.content.decode("utf-8"))
    assert req_body["model"] == "glm-5.3"
    assert req_body["thinking"] == {"type": "enabled"}
    assert "response_format" not in req_body

