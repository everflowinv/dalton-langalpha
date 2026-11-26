"""Unit tests for Dalton's conversation knowledge pipeline (extraction, dedup, search, render).

Covers:
1. Rule-based sanitization: JSON (`"api_key": "..."`, paired quotes with escapes), env (`KEY=...`),
   `Authorization: Bearer ...`, pairing codes, and API keys redacted regardless of length/entropy;
   ops and <30-char chitchat skipped.
2. Extraction & verbatim quote verification (`extractor.py`): fabricated quotes rejected, ops/private segments
   discarded without persisting raw LLM output (`persist_raw=False`), agent `fact`/`estimate`/`model_assumption`
   cards verified against segment text for `cited_sources` substrings (else downgraded to `opinion`),
   and oversized single events split with stable `chunk_idx` in `segment_events` / `expand_event_chunks`.
3. Deduplication & `supersedes`: single-ticker chronological `supersedes` marking all older active cards
   for `(ticker, metric, period)` as `superseded` after inserting all batch cards in the same transaction.
4. Hybrid search (`search.py` & `conversation_search` tool): requires authenticated `user_id` in `RunnableConfig`,
   logs caller in audit log, and returns team-shared cards without filtering by caller.
5. Workspace rendering (`render.py`) and the `/embed` bridge (`researchdata_bridge.py`): `SandboxBackend`
   autospec writes, `rendered_locations` tracking (including Home -> company workspace migration),
   `bulk_upsert_files` error propagation, unrendered date retry, hourly budget retry re-check, and
   `/embed` header-only auth + byte/concurrency limits.
"""

from __future__ import annotations

from datetime import date
import logging
import json
import os
from pathlib import Path
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
from deepagents.backends.protocol import ExecuteResponse
from ptc_agent.agent.backends.sandbox import SandboxBackend
from scripts.dalton import researchdata_bridge as bridge_mod
from src.dalton.conversations import embedder as embedder_mod
from src.dalton.conversations import extractor as extractor_mod
from src.dalton.conversations import render as render_mod
from src.dalton.conversations import search as search_mod
from src.dalton.database import conversations as db_conv
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm.assist import AssistResult
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
            "text": "META 2026 年资本开支指引我们模型按 680 亿美元测算，同时 OpenAI 与 xAI 的推理算力需求仍在翻倍。",
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
                        "tickers": ["META", "OpenAI", "xAI"],
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
    assert "xAI" in owner_card["entities"]
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
        resend_max_attempts: int | None = None,
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
            "provider": "antigravity-oauth",
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
        attempt: int,
        response: dict[str, Any] | None,
        usage: dict[str, Any] | None = None,
        cash_usd: Any = 0,
        notional_usd: Any = 0,
        provider: str | None = None,
        billing_type: str | None = None,
        stamp_credential_holder: bool = True,
        attempt_usage: dict[str, Any] | None = None,
        unknown_usage_calls: int = 0,
        occurred_at: Any = None,
        call_records: list[dict[str, Any]] | None = None,
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
    # Authenticated callers in this suite are internal team members.
    from src.dalton.tools import conversation_tools as conv_tools_mod

    monkeypatch.setattr(
        conv_tools_mod,
        "get_user_tier",
        create_autospec(conv_tools_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        conv_tools_mod,
        "resolve_turn_team_memory_enabled",
        create_autospec(
            conv_tools_mod.resolve_turn_team_memory_enabled, return_value=True
        ),
    )

    analyst_tools = {t.name: t for t in build_dalton_tools("analyst", team_memory_enabled=True)}
    cos_tools = {t.name: t for t in build_dalton_tools("chief_of_staff", team_memory_enabled=True)}
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
    nvda_backend.aexecute.return_value = ExecuteResponse(output="", exit_code=0, truncated=False)
    home_backend = create_autospec(SandboxBackend, instance=True)
    home_backend.awrite_text.side_effect = _write_home
    home_backend.aexecute.return_value = ExecuteResponse(output="", exit_code=0, truncated=False)

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
    crwv_backend.aexecute.return_value = ExecuteResponse(output="", exit_code=0, truncated=False)
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
    old_ws_backend.aexecute.return_value = ExecuteResponse(output="", exit_code=0, truncated=False)
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
    before any DB connection or destructive SQL when explicit test DB config is missing or when
    the configured DB name does not end with `_test`.
    """
    from tests.integration.dalton import conftest as dalton_int_conftest

    mock_session = create_autospec(pytest.Session, instance=True)

    for var in (
        "TEST_DB_DSN",
        "DALTON_TEST_DB_DSN",
        "TEST_DATABASE_URL",
        "DALTON_DATA_JOBS_TEST_DSN",
        "TEST_DB_HOST",
        "TEST_DB_PORT",
        "TEST_DB_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    # Missing explicit test DB configuration must be refused (no localhost:5432 fallback)
    with pytest.raises(pytest.exit.Exception, match="without explicit test DB configuration"):
        dalton_int_conftest.pytest_sessionstart(mock_session)

    # Non-_test DB name must be refused regardless of host/port
    monkeypatch.setenv("TEST_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("TEST_DB_PORT", "55432")
    monkeypatch.setenv("TEST_DB_NAME", "langalpha_prod")
    with pytest.raises(pytest.exit.Exception, match="must end with '_test'"):
        dalton_int_conftest.pytest_sessionstart(mock_session)

    # Empty TEST_DB_NAME must be refused as missing explicit test DB configuration
    monkeypatch.setenv("TEST_DB_NAME", "")
    with pytest.raises(pytest.exit.Exception, match="without explicit test DB configuration"):
        dalton_int_conftest.pytest_sessionstart(mock_session)

    # TEST_DB_NAME alone without explicit host/port must still be refused
    monkeypatch.delenv("TEST_DB_HOST", raising=False)
    monkeypatch.delenv("TEST_DB_PORT", raising=False)
    monkeypatch.setenv("TEST_DB_NAME", "langalpha_test")
    with pytest.raises(pytest.exit.Exception, match="without explicit test DB configuration"):
        dalton_int_conftest.pytest_sessionstart(mock_session)

    # Explicit host + port + *_test DB name is accepted
    monkeypatch.setenv("TEST_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("TEST_DB_PORT", "55432")
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



def _pin_root_logging_for_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore root's logging config at teardown, before the CLI touches it.

    The CLI's entry points run ``configure_archive_logging``, a process-entry
    helper that force-resets root to INFO with its own handler. In-process,
    that leaks to every later test (an INFO httpx line quoting a URL with a
    secret then lands in unrelated caplogs). The monkeypatch snapshots pin
    root's level and handlers for restoration once this test ends; the CLI
    stays free to reconfigure while the test itself runs.
    """
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(root, "handlers", list(root.handlers))



















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
    monkeypatch.setattr(
        conv_tools_mod,
        "get_user_tier",
        create_autospec(conv_tools_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        conv_tools_mod,
        "resolve_turn_team_memory_enabled",
        create_autospec(
            conv_tools_mod.resolve_turn_team_memory_enabled, return_value=True
        ),
    )
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

    # --- Item 2: Listed SpaceX (`SPCX`) stays in tickers, private companies (`OPENAI`, `PRIVATE_COMPANIES`)
    # and trading channels (`IBKR` vs `FUTU`) ---
    c_0b0 = deepcopy(by_id["v22_0b0936821124"])
    res_0b0 = ex.validate_candidate_card(c_0b0)
    assert res_0b0.tickers == ["SPCX"]
    assert res_0b0.tickers_dropped_private == 0

    c_bf1 = deepcopy(by_id["v22_bf1790fd1ca2"])
    res_bf1 = ex.validate_candidate_card(c_bf1)
    assert res_bf1.tickers == ["TSM", "INTC", "SPCX"]
    assert res_bf1.tickers_dropped_private == 0

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
        if cid == "v22_0b0936821124":
            # SPCX is now listed; add an unlisted private company to verify private-drop counter.
            item["tickers"] = list(item.get("tickers") or []) + ["OPENAI"]
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
        "topic_label": "OpenAI算力扩张",
        "claim": "OpenAI全年推理算力继续扩张。",
        "tickers": ["OPENAI", "TSLA"],
        "entities": [],
        "sectors": [],
        "figures": [],
        "quote": "OpenAI 与 Tesla 协同推进，全年推理算力继续扩张。",
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
            "text": "卖方预计2026年OpenAI收入增长20%。",
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
                    "topic_label": "OpenAI",
                    "cards": [
                        {
                            "type": "estimate",
                            "topic_label": "OpenAI预测",
                            "claim": "卖方预计2026年OpenAI收入增长20%。",
                            "tickers": ["OPENAI", "OPENAI"],
                            "entities": [],
                            "sectors": [],
                            "figures": [],
                            "quote": "卖方预计2026年OpenAI收入增长20%。",
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
            "claim": "卖方预计2026年OpenAI收入增长20%。",
            "topic_label": "OpenAI预测",
            "type": "opinion",
            "figures": [],
            "tickers": ["OPENAI", "OPENAI"],
            "quote": "卖方预计2026年OpenAI收入增长20%。",
            "cited_sources": [],
            "confidence": 0.8,
        },
        quote="卖方预计2026年OpenAI收入增长20%。",
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


















































def test_normalize_number_token_extreme_values_and_non_finite_return_none() -> None:
    """Verify `normalize_number_token` returns `None` without raising on huge exponents (`'1e5000'`),
    overly long digit strings, `'Infinity'`, and `'NaN'`, while preserving normal number behavior.
    """
    assert extractor_mod.MAX_NUMBER_INTEGER_DIGITS == 30

    # Extreme / non-finite / non-numeric inputs return None without raising
    for bad_token in (
        "1e5000",
        "-1e5000",
        "1e-5000",
        "1e30",
        "9" * 31,
        "9" * 5000,
        "0" * 31,
        "1" + "0" * 5000 + ".25",
        "Infinity",
        "-Infinity",
        "+Infinity",
        "inf",
        "-inf",
        "NaN",
        "nan",
        "sNaN",
        "",
        "   ",
        "not-a-number",
    ):
        assert extractor_mod.normalize_number_token(bad_token) is None

    # Normal numbers behave as expected
    assert extractor_mod.normalize_number_token("1,200") == "1200"
    assert extractor_mod.normalize_number_token("35.0%") == "35"
    assert extractor_mod.normalize_number_token("1e6") == "1000000"
    assert extractor_mod.normalize_number_token("-0.25") == "0.25"
    assert extractor_mod.normalize_number_token("0") == "0"
    assert extractor_mod.normalize_number_token("1e29") == "1" + "0" * 29
    assert extractor_mod.normalize_number_token("9" * 30) == "9" * 30

    # End-to-end card validation & figure/Chinese numeral helpers do not raise on extreme tokens
    quote_with_huge = "NVDA 压测日志出现异常值 1e5000 与 Infinity、NaN，四季度机柜出货 1.2 万柜。"
    supported = extractor_mod.extract_quote_supported_numbers(quote_with_huge)
    assert "1.2" in supported
    assert "12000" in supported

    val_res = extractor_mod.validate_candidate_card(
        {
            "claim": "NVDA 四季度机柜出货 1.2 万柜（日志含 1e5000）",
            "topic_label": "NVDA 机柜出货",
            "type": "fact",
            "figures": [
                {"metric": "rack_shipments", "value": "1.2", "unit": "万柜", "period": "2026Q4"},
                {"metric": "overflow_noise", "value": "1e5000", "unit": "", "period": "2026Q4"},
            ],
            "tickers": ["NVDA"],
            "quote": quote_with_huge,
            "cited_sources": ["company_filing"],
            "confidence": "NaN",
        },
        quote=quote_with_huge,
    )
    assert val_res.ok is True
    assert val_res.confidence == 0.8
    assert [f["metric"] for f in val_res.figures] == ["rack_shipments"]
    assert extractor_mod._parse_chinese_number("亿" * 20) is None
    assert extractor_mod._normalize_figure_value(10**400) == str(10**400)
    assert extractor_mod._normalize_figure_value("Infinity") == "infinity"








