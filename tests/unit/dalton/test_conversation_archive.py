"""Unit tests for Dalton's conversation knowledge retrieval and digest rendering."""

from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from deepagents.backends.protocol import ExecuteResponse
from ptc_agent.agent.backends.sandbox import SandboxBackend
from scripts.dalton import researchdata_bridge as bridge_mod
from src.dalton.conversations import render as render_mod
from src.dalton.conversations import search as search_mod
from src.dalton.database import conversations as db_conv
from src.dalton.settings import get_dalton_bridge_embed_url
from src.dalton.tools import build_dalton_tools


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
        search_mod.embed_query,
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
    monkeypatch.setattr(search_mod, "embed_query", mock_embed_q)
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
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(tmp_path))
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
    url = get_dalton_bridge_embed_url("127.0.0.1")
    assert url == "http://127.0.0.1:8960/embed"
    assert "token" not in url


def test_integration_conftest_sessionstart_test_db_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify `tests/integration/dalton/conftest.py` `pytest_sessionstart` aborts via `pytest.exit`
    before any DB connection or destructive SQL when explicit test DB config is missing or when
    the configured DB name does not end with `_test`.
    """
    had_dotenv_disabled = "PYTHON_DOTENV_DISABLED" in os.environ
    prev_dotenv_disabled = os.environ.get("PYTHON_DOTENV_DISABLED")
    try:
        from tests.integration.dalton import conftest as dalton_int_conftest
    finally:
        if had_dotenv_disabled and prev_dotenv_disabled is not None:
            os.environ["PYTHON_DOTENV_DISABLED"] = prev_dotenv_disabled
        else:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)

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




def test_round7_item12_remove_unrequested_ddog_alias() -> None:
    """Round 7 Item 12: `DDOG` / `Datadog` removed from `SUPPLEMENTAL_TICKER_ALIASES`."""
    from src.dalton.coverage.aliases import SUPPLEMENTAL_TICKER_ALIASES

    assert "DDOG" not in SUPPLEMENTAL_TICKER_ALIASES
