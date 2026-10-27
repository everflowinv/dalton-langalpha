"""PostgreSQL integration tests for Dalton's OpenClaw conversation archive pipeline (`d009`).

Exercises the real SQLite export (`run_export`), `dalton_conversation_processed` 4-tuple chunk tracking
and persisted split boundaries (`chunk_spans`), `dalton_conversation_cards` insertion & chronological
`supersedes`, `dalton_conversation_runs` telemetry, and `workspace_files` rendering under the PM owner user,
replacing only the LLM extraction call and embedding bridge call via `create_autospec`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg.rows import dict_row

from deepagents.backends.protocol import ExecuteResponse
from ptc_agent.agent.backends.sandbox import SandboxBackend
from scripts.dalton import conversation_archive as archive_cli
from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.conversations import embedder as embedder_mod
from src.dalton.conversations import extractor as extractor_mod
from src.dalton.conversations import render as render_mod
from src.dalton.database import conversations as db_conv
from src.dalton.settings import DaltonConversationsConfig
from src.server.database.user import create_user
from src.server.database.workspace import (
    create_workspace,
    get_or_create_home_workspace,
)
from src.server.database.workspace_file import get_files_for_workspace

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


def _init_openclaw_sqlite(db_path: Path) -> sqlite3.Connection:
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


def _make_fixed_card(
    segment: list[dict[str, Any]],
    *,
    claim: str,
    quote: str,
    card_type: str = "estimate",
    tickers: list[str] | None = None,
    entities: list[str] | None = None,
    figures: list[dict[str, Any]] | None = None,
    topic_label: str = "AI 半导体跟踪",
) -> dict[str, Any]:
    ev = segment[0]
    ev_id = str(ev["event_id"])
    c_hash = extractor_mod.compute_content_hash(quote, ev_id)
    speaker = ev.get("speaker") if isinstance(ev.get("speaker"), dict) else {
        "sender_id_hash": "owner",
        "name": "Owner",
        "is_owner": True,
        "role": "owner",
    }
    card: dict[str, Any] = {
        "card_id": f"cc_{c_hash[:24]}",
        "source_type": "conversation",
        "agent": str(ev.get("agent") or "main"),
        "session_id": str(ev.get("session_id") or ""),
        "session_key": str(ev.get("session_key") or ev.get("session_id") or ""),
        "generation": str(ev.get("generation") or ""),
        "channel": str(ev.get("channel") or ""),
        "chat_type": str(ev.get("chat_type") or ""),
        "event_ids": [str(e.get("event_id") or "") for e in segment],
        "seq_range": [int(segment[0].get("seq", 0)), int(segment[-1].get("seq", 0))],
        "ts": str(ev.get("ts") or ""),
        "date_et": str(ev.get("date_et") or "")[:10],
        "speaker": speaker,
        "speaker_role": str(speaker.get("role") or "owner"),
        "tickers": list(tickers or []),
        "entities": list(entities or []),
        "sectors": ["Semiconductors"],
        "topic_label": topic_label,
        "type": card_type,
        "claim": claim,
        "figures": list(figures or []),
        "quote": quote,
        "quote_event_id": ev_id,
        "cited_sources": [],
        "confidence": 0.9,
        "status": "active",
        "supersedes": None,
        "superseded_card_ids": [],
        "extractor_model": "gemini-4-argon-medium-oauth",
        "prompt_version": "conv_card_v1",
        "content_hash": c_hash,
    }
    card["search_text"] = db_conv.build_card_search_text(card)
    return card


async def test_conversation_archive_end_to_end_pipeline_db(
    patched_get_db_connection,
    cleanup_tables,
    db_conn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify full OpenClaw SQLite -> export -> DB pipeline against real PostgreSQL (`d009`):
    1. Partial failure on chunk 1 of an oversized split message records chunk 0 (with `chunk_spans`),
       leaves chunk 1 unrecorded, and preserves the inbox `.jsonl` file on disk.
    2. Retry run (even after `max_segment_chars` changes from 6000 to 12000) reuses persisted
       `chunk_spans`, extracts only the remaining chunk 1, deletes the inbox `.jsonl` file,
       persists cards with 3072-d vectors, computes chronological single-ticker `supersedes`,
       and renders Markdown digests to the PM owner user's company & Home workspaces.
    3. Re-running the same input is idempotent (0 duplicate cards, 0 re-processed chunks).
    """
    pm_user_id = "pm-owner-integration-001"
    monkeypatch.setenv("DALTON_PM_USER_IDS", pm_user_id)
    assert render_mod.archive_owner_user_id() == pm_user_id
    assert archive_cli.archive_billing_user_id() == pm_user_id

    # Seed both the PM owner user and LOCAL_DEV_USER_ID to prove rendering targets the PM owner's workspaces
    await create_user(user_id=pm_user_id, email="pm-owner@dalton.test", name="PM Owner")
    pm_home_ws = await get_or_create_home_workspace(pm_user_id)
    pm_nvda_ws = await create_workspace(
        user_id=pm_user_id,
        name="NVDA",
        description="PM NVDA workspace",
        status="running",
    )

    await create_user(user_id=LOCAL_DEV_USER_ID, email="dev@dalton.test", name="Local Dev")
    dev_home_ws = await get_or_create_home_workspace(LOCAL_DEV_USER_ID)
    dev_nvda_ws = await create_workspace(
        user_id=LOCAL_DEV_USER_ID,
        name="NVDA",
        description="Dev NVDA workspace",
        status="running",
    )

    # Construct temporary OpenClaw SQLite DB
    ocw_agents_root = tmp_path / "ocw_agents"
    main_agent_dir = ocw_agents_root / "main" / "agent"
    main_agent_dir.mkdir(parents=True)
    sqlite_path = main_agent_dir / "openclaw-agent.sqlite"
    sconn = _init_openclaw_sqlite(sqlite_path)

    sconn.executemany(
        """
        INSERT INTO session_windows (session_id, session_key, channel, chat_type, account_id, primary_conversation_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("sess-discord", "agent:main:discord:channel:101", "discord", "channel", "acc1", "conv1"),
            ("sess-codex", "codex-desktop-main", "codex-desktop", "direct", "acc1", "conv2"),
        ],
    )
    sconn.executemany(
        "INSERT INTO transcript_rewrite_watermarks (session_id, generation) VALUES (?, ?)",
        [
            ("sess-discord", "gen-1"),
            ("sess-codex", "gen-1"),
        ],
    )

    ts_early_ms = int(datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ts_late_ms = int(datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ts_priv_ms = int(datetime(2026, 10, 18, 15, 5, tzinfo=timezone.utc).timestamp() * 1000)
    ts_codex_ms = int(datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc).timestamp() * 1000)
    ts_long_ms = int(datetime(2026, 10, 19, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)

    quote_early = "NVDA 2027 财年数据中心收入我们初步按 1800 亿美元放进模型。"
    quote_late = "最新产业链核查后，NVDA 2027 财年数据中心收入我们上调到 2050 亿美元。"
    text_private = "今天下午去医院体检看病，纯私人行程不要入库。"
    codex_long_paste = "光模块专家访谈长粘贴纪要\n\n" + "\n\n".join(
        f"第 {i} 段专家发言：讨论 800G 与 1.6T 光模块在北美云厂商导入节奏、硅光渗透率与明年价格年降幅度。"
        for i in range(1, 40)
    )
    assert len(codex_long_paste) > 2000
    quote_codex_human = "光模块访谈里提到的 OpenAI 与 Anthropic 明年推理集群需求值得重点跟踪。"

    long_para_0 = ("第一部分深度调研：NVDA GB200 NVL72 机柜铜连接与液冷供应链详细拆解。" * 105).strip()
    long_para_1 = ("第二部分深度调研：NVDA Rubin 下一代机架供电架构与光互联演进路线图。" * 105).strip()
    assert len(long_para_0) > 3300
    assert len(long_para_1) > 3300
    text_long_split = long_para_0 + "\n\n" + long_para_1

    def _msg_json(ev_id: str, role: str, content: str) -> str:
        payload: dict[str, Any] = {
            "type": "message",
            "id": ev_id,
            "message": {
                "role": role,
                "content": content,
            },
        }
        if role == "user":
            payload["message"]["__openclaw"] = {
                "senderId": "owner_01",
                "senderName": "Owner",
                "senderIsOwner": True,
                "transport": {"channel": "discord"},
            }
        return json.dumps(payload, ensure_ascii=False)

    sconn.executemany(
        """
        INSERT INTO transcript_events (session_id, seq, event_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("sess-discord", 1, _msg_json("ev-nvda-early", "user", quote_early), ts_early_ms),
            ("sess-discord", 2, _msg_json("ev-nvda-late", "user", quote_late), ts_late_ms),
            ("sess-discord", 3, _msg_json("ev-private", "user", text_private), ts_priv_ms),
            ("sess-discord", 4, _msg_json("ev-long-split", "user", text_long_split), ts_long_ms),
            ("sess-codex", 1, _msg_json("ev-codex-paste", "user", codex_long_paste), ts_codex_ms),
            ("sess-codex", 2, _msg_json("ev-codex-echo", "assistant", "整理如下：\n\n" + codex_long_paste), ts_codex_ms + 1000),
            ("sess-codex", 3, _msg_json("ev-codex-human", "user", quote_codex_human), ts_codex_ms + 2000),
        ],
    )
    sconn.executemany(
        """
        INSERT INTO transcript_event_identities (session_id, seq, event_id, event_type)
        VALUES (?, ?, ?, 'message')
        """,
        [
            ("sess-discord", 1, "ev-nvda-early"),
            ("sess-discord", 2, "ev-nvda-late"),
            ("sess-discord", 3, "ev-private"),
            ("sess-discord", 4, "ev-long-split"),
            ("sess-codex", 1, "ev-codex-paste"),
            ("sess-codex", 2, "ev-codex-echo"),
            ("sess-codex", 3, "ev-codex-human"),
        ],
    )
    sconn.commit()
    sconn.close()

    nvda_written: dict[str, str] = {}
    home_written: dict[str, str] = {}

    async def _write_nvda(rel_path: str, text: str) -> bool:
        nvda_written[rel_path] = text
        return True

    async def _write_home(rel_path: str, text: str) -> bool:
        home_written[rel_path] = text
        return True

    nvda_backend = create_autospec(SandboxBackend, instance=True)
    nvda_backend.awrite_text.side_effect = _write_nvda
    nvda_backend.aexecute.return_value = ExecuteResponse(output="", exit_code=0, truncated=False)

    home_backend = create_autospec(SandboxBackend, instance=True)
    home_backend.awrite_text.side_effect = _write_home
    home_backend.aexecute.return_value = ExecuteResponse(output="", exit_code=0, truncated=False)

    extracted_calls: list[tuple[str, int]] = []
    fail_second_split_chunk = True

    async def _fake_extract(
        segment: list[dict[str, Any]], **kwargs: Any
    ) -> extractor_mod.SegmentExtractionOutcome:
        ev = segment[0]
        ev_id = str(ev["event_id"])
        cidx = int(ev.get("chunk_idx", 0))
        extracted_calls.append((ev_id, cidx))

        if ev_id == "ev-long-split" and cidx == 1 and fail_second_split_chunk:
            return extractor_mod.SegmentExtractionOutcome(
                ok=False,
                model_used="gemini-4-argon-medium-oauth",
                rate_limited=False,
                error="http_503: simulated transient upstream failure on chunk 1",
            )

        if ev_id == "ev-nvda-early":
            card = _make_fixed_card(
                segment,
                claim="NVDA 2027 财年数据中心收入初步按 1800 亿美元建模",
                quote=quote_early,
                card_type="model_assumption",
                tickers=["NVDA"],
                figures=[{"metric": "datacenter_revenue", "value": 180, "unit": "USD_bn", "period": "FY2027"}],
            )
        elif ev_id == "ev-nvda-late":
            card = _make_fixed_card(
                segment,
                claim="NVDA 2027 财年数据中心收入上调至 2050 亿美元",
                quote=quote_late,
                card_type="model_assumption",
                tickers=["NVDA"],
                figures=[{"metric": "datacenter_revenue", "value": 205, "unit": "USD_bn", "period": "FY2027"}],
            )
        elif ev_id == "ev-codex-human":
            card = _make_fixed_card(
                segment,
                claim="OpenAI 与 Anthropic 明年推理集群需求值得重点跟踪",
                quote=quote_codex_human,
                card_type="opinion",
                tickers=[],
                entities=["OpenAI", "Anthropic"],
                topic_label="前沿模型推理需求",
            )
        else:
            q_sub = str(ev.get("text") or "")[:48]
            card = _make_fixed_card(
                segment,
                claim=f"NVDA 机柜供应链调研纪要分段 #{cidx}",
                quote=q_sub,
                card_type="fact",
                tickers=["NVDA"],
            )

        return extractor_mod.SegmentExtractionOutcome(
            ok=True,
            category="research",
            cards=[card],
            cards_raw_count=1,
            model_used="gemini-4-argon-medium-oauth",
            input_tokens=120,
            output_tokens=60,
            total_tokens=180,
        )

    async def _fake_embed(
        texts: list[str],
        *,
        task_type: str = "RETRIEVAL_DOCUMENT",
        bridge_url: str | None = None,
        bridge_token: str | None = None,
        timeout_s: float = 60.0,
    ) -> tuple[list[list[float]], str]:
        vec = [0.01] * 3072
        return [list(vec) for _ in texts], "gemini-embedding-001"

    mock_extract_fn = create_autospec(
        extractor_mod.extract_segment_cards,
        side_effect=_fake_extract,
    )
    mock_embed_fn = create_autospec(
        embedder_mod.embed_texts_via_bridge,
        side_effect=_fake_embed,
    )

    inbox_dir = tmp_path / "inbox"
    export_cursors_file = tmp_path / "export_cursors.json"

    # -------------------------------------------------------------------------
    # Run 1: Simulate failure on the 2nd chunk (`chunk_idx=1`) of `ev-long-split`
    # -------------------------------------------------------------------------
    with pytest.raises(archive_cli.SegmentExtractionError):
        await archive_cli.run_archive_pipeline(
            mode="run",
            since_date=date(2026, 10, 18),
            until_date=date(2026, 10, 19),
            inbox_dir=inbox_dir,
            export_cursors_file=export_cursors_file,
            ocw_agents_root=ocw_agents_root,
            agents=["main"],
            skip_host_export=False,
            extract_fn=mock_extract_fn,
            embed_fn=mock_embed_fn,
            backend_by_ticker={"NVDA": nvda_backend},
            home_backend=home_backend,
        )

    # Inbox file must still exist because chunk 1 of `ev-long-split` failed
    inbox_files_after_fail = list(inbox_dir.glob("*.jsonl"))
    assert len(inbox_files_after_fail) == 1
    assert not export_cursors_file.exists()

    # Check `dalton_conversation_processed` in real Postgres: chunk 0 is recorded with `chunk_count=2` and `chunk_spans`,
    # while chunk 1 is NOT recorded!
    async with db_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT agent, session_id, event_id, chunk_idx, chunk_count, chunk_max_chars, chunk_spans, status
            FROM dalton_conversation_processed
            ORDER BY event_id, chunk_idx
            """
        )
        proc_rows_1 = [dict(r) for r in await cur.fetchall()]

    proc_keys_1 = {(r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"])) for r in proc_rows_1}
    assert ("main", "sess-discord", "ev-nvda-early", 0) in proc_keys_1
    assert ("main", "sess-discord", "ev-nvda-late", 0) in proc_keys_1
    assert ("main", "sess-codex", "ev-codex-human", 0) in proc_keys_1
    assert ("main", "sess-discord", "ev-long-split", 0) in proc_keys_1
    assert ("main", "sess-discord", "ev-long-split", 1) not in proc_keys_1

    split_row_0 = next(
        r for r in proc_rows_1 if r["event_id"] == "ev-long-split" and int(r["chunk_idx"]) == 0
    )
    assert int(split_row_0["chunk_count"]) == 2
    assert int(split_row_0["chunk_max_chars"]) == 6000
    assert isinstance(split_row_0["chunk_spans"], list) and len(split_row_0["chunk_spans"]) == 2

    # -------------------------------------------------------------------------
    # Run 2: Increase `max_segment_chars` to 12000 and retry -> reuses persisted
    # boundaries from `dalton_conversation_processed`, extracts ONLY chunk 1, and deletes inbox!
    # -------------------------------------------------------------------------
    extracted_calls.clear()
    fail_second_split_chunk = False
    larger_cfg = DaltonConversationsConfig(max_segment_chars=12000)
    monkeypatch.setattr(
        archive_cli,
        "get_dalton_settings",
        create_autospec(
            archive_cli.get_dalton_settings,
            return_value=type("FakeSettings", (), {"conversations": larger_cfg})(),
        ),
    )

    run2_summary = await archive_cli.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 18),
        until_date=date(2026, 10, 19),
        inbox_dir=inbox_dir,
        export_cursors_file=export_cursors_file,
        ocw_agents_root=ocw_agents_root,
        agents=["main"],
        skip_host_export=True,
        extract_fn=mock_extract_fn,
        embed_fn=mock_embed_fn,
        backend_by_ticker={"NVDA": nvda_backend},
        home_backend=home_backend,
    )
    assert run2_summary["ok"] is True
    # Only the previously failed chunk 1 was extracted on retry!
    assert extracted_calls == [("ev-long-split", 1)]
    assert list(inbox_dir.glob("*.jsonl")) == []

    # Verify all 5 chunks are now in `dalton_conversation_processed`
    async with db_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT agent, session_id, event_id, chunk_idx FROM dalton_conversation_processed"
        )
        proc_keys_2 = {
            (r["agent"], r["session_id"], r["event_id"], int(r["chunk_idx"]))
            for r in await cur.fetchall()
        }
    assert proc_keys_2 == {
        ("main", "sess-discord", "ev-nvda-early", 0),
        ("main", "sess-discord", "ev-nvda-late", 0),
        ("main", "sess-codex", "ev-codex-human", 0),
        ("main", "sess-discord", "ev-long-split", 0),
        ("main", "sess-discord", "ev-long-split", 1),
    }

    # Verify cards and `supersedes` relationship in `dalton_conversation_cards`
    async with db_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT card_id, quote_event_id, status, supersedes, tickers, entities,
                   rendered_locations, files_written_at
            FROM dalton_conversation_cards
            ORDER BY ts ASC, card_id ASC
            """
        )
        db_cards = [dict(r) for r in await cur.fetchall()]

    assert len(db_cards) == 5
    early_card = next(c for c in db_cards if c["quote_event_id"] == "ev-nvda-early")
    late_card = next(c for c in db_cards if c["quote_event_id"] == "ev-nvda-late")
    codex_card = next(c for c in db_cards if c["quote_event_id"] == "ev-codex-human")

    assert early_card["status"] == "superseded"
    assert early_card["supersedes"] is None
    assert early_card["rendered_locations"] == []
    assert early_card["files_written_at"] is not None

    assert late_card["status"] == "active"
    assert late_card["supersedes"] == early_card["card_id"]
    assert late_card["files_written_at"] is not None
    assert late_card["rendered_locations"] == [
        {
            "target": "company",
            "ticker": "NVDA",
            "workspace_id": str(pm_nvda_ws["workspace_id"]),
            "path": "conversations/2026-10-18.md",
        }
    ]

    assert codex_card["status"] == "active"
    assert codex_card["tickers"] == []
    assert codex_card["entities"] == ["OpenAI", "Anthropic"]
    assert codex_card["rendered_locations"] == [
        {
            "target": "home",
            "ticker": None,
            "workspace_id": str(pm_home_ws["workspace_id"]),
            "path": "research/topics/conversations/2026-10-18.md",
        }
    ]

    # Verify rendered files were written to the PM owner's workspaces and NOT to LOCAL_DEV_USER_ID's workspaces
    assert "conversations/2026-10-18.md" in nvda_written
    assert "conversations/2026-10-19.md" in nvda_written
    assert "2050 亿美元" in nvda_written["conversations/2026-10-18.md"]
    assert "1800 亿美元" not in nvda_written["conversations/2026-10-18.md"]
    assert "research/topics/conversations/2026-10-18.md" in home_written
    assert "OpenAI" in home_written["research/topics/conversations/2026-10-18.md"]

    pm_nvda_files = await get_files_for_workspace(str(pm_nvda_ws["workspace_id"]), include_content=True)
    pm_home_files = await get_files_for_workspace(str(pm_home_ws["workspace_id"]), include_content=True)
    dev_nvda_files = await get_files_for_workspace(str(dev_nvda_ws["workspace_id"]), include_content=True)
    dev_home_files = await get_files_for_workspace(str(dev_home_ws["workspace_id"]), include_content=True)

    assert {f["file_path"] for f in pm_nvda_files} == {
        "conversations/2026-10-18.md",
        "conversations/2026-10-19.md",
    }
    assert {f["file_path"] for f in pm_home_files} == {
        "research/topics/conversations/2026-10-18.md",
    }
    assert dev_nvda_files == []
    assert dev_home_files == []

    # -------------------------------------------------------------------------
    # Run 3: Re-run the exact same input -> idempotent (no duplicate cards, no re-extraction)
    # -------------------------------------------------------------------------
    extracted_calls.clear()
    run3_summary = await archive_cli.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 18),
        until_date=date(2026, 10, 19),
        inbox_dir=inbox_dir,
        export_cursors_file=export_cursors_file,
        ocw_agents_root=ocw_agents_root,
        agents=["main"],
        skip_host_export=False,
        extract_fn=mock_extract_fn,
        embed_fn=mock_embed_fn,
        backend_by_ticker={"NVDA": nvda_backend},
        home_backend=home_backend,
    )
    assert run3_summary["ok"] is True
    assert run3_summary["cards_inserted_total"] == 0
    assert extracted_calls == []
    assert list(inbox_dir.glob("*.jsonl")) == []

    async with db_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT COUNT(*) AS cnt FROM dalton_conversation_cards")
        total_cards_after_rerun = int((await cur.fetchone())["cnt"])
    assert total_cards_after_rerun == 5
