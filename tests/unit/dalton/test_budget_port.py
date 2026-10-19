"""Unit tests for Dalton budget port, admission gate, LLM journal, and assist_json (P0.4–P0.8, P0N.6–7, G0, G1)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from fastapi import HTTPException

from src.dalton.budget.gate import build_local_run_gate, enforce_local_admission
from src.dalton.llm.complete import _check_cash_budget_admission
from src.dalton.budget.port import (
    LocalBudgetPort,
    format_daily_budget_exhausted_message,
    format_task_cap_exhausted_message,
)
from src.dalton.budget.service import _local_day_bounds
from src.dalton.llm.assist import AssistResult, assist_json
from src.dalton.llm.complete import (
    BudgetExhaustedError,
    JournalIndeterminate,
    dalton_complete,
)
from src.dalton.llm.openclaw_catalog import build_overlay
from src.dalton.llm.role_policy import check_roles, family_of
from src.server.services.llm.service import LLMService


def _make_mock_conn() -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction.return_value = mock_tx
    return mock_conn


def test_local_day_bounds_shanghai():
    day_val, start_utc, end_utc, retry_s = _local_day_bounds("Asia/Shanghai")
    assert len(day_val.isoformat()) == 10
    assert end_utc > start_utc
    assert 1 <= retry_s <= 86400


def test_budget_exhausted_messages_chinese():
    unconfigured = format_daily_budget_exhausted_message(
        Decimal("0"), Decimal("0"), Decimal("0"), configured=False
    )
    assert "未设置日预算" in unconfigured
    assert "Dalton 预算" in unconfigured

    daily = format_daily_budget_exhausted_message(
        Decimal("30.00"), Decimal("28.50"), Decimal("1.50"), configured=True
    )
    assert "今日预算 $30.00 已用完" in daily
    assert "$28.50" in daily
    assert "$1.50" in daily
    assert "现金模型" in daily

    task = format_task_cap_exhausted_message(Decimal("5.00"), Decimal("5.00"))
    assert "本任务预算 $5.00 已用完" in task


@pytest.mark.asyncio
async def test_acquire_byok_enforces_daily_cash_cap():
    """G1.1: `acquire(..., byok=True)` must NOT return an unbounded +1,000,000 ceiling;
    it must enforce the daily USD cash cap and deny when exhausted."""
    port = LocalBudgetPort()
    mock_conn = _make_mock_conn()

    mock_cur = MagicMock()
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=None)
    mock_cur.execute = AsyncMock(return_value=None)
    mock_cur.fetchone = AsyncMock(
        return_value={
            "run_ref": "run-1",
            "reserved_usd": Decimal("1.00"),
            "observed_usd": Decimal("0.50"),
            "usage_usd": Decimal("0.50"),
            "task_cap_usd": None,
            "generation": 2,
        }
    )
    mock_conn.cursor.return_value = mock_cur

    exhausted_summary = {
        "configured": True,
        "retry_after_seconds": 1800,
        "_raw_rows": [
            {
                "run_ref": "run-1",
                "reserved_usd": Decimal("1.00"),
                "observed_usd": Decimal("0.50"),
                "usage_usd": Decimal("0.50"),
                "charge_usd": Decimal("1.00"),
                "task_cap_usd": None,
                "generation": 2,
            }
        ],
        "_decimals": {
            "cap_usd": Decimal("1.00"),
            "spent_usd": Decimal("1.50"),
            "settled_usd": Decimal("0.50"),
            "unreserved_usage_usd": Decimal("0.00"),
            "open_reserved_usd": Decimal("1.00"),
            "expired_conservative_usd": Decimal("0.00"),
        },
    }

    with (
        patch("src.dalton.budget.port.get_db_connection", return_value=mock_conn),
        patch(
            "src.dalton.budget.port.reconcile_and_summarize_day",
            new=AsyncMock(return_value=exhausted_summary),
        ),
        patch(
            "src.dalton.budget.port.resolve_user_budget_config",
            new=AsyncMock(
                return_value={
                    "user_id": "user-1",
                    "daily_cap_usd": Decimal("1.00"),
                    "default_task_cap_usd": None,
                    "timezone": "Asia/Shanghai",
                    "configured": True,
                }
            ),
        ),
    ):
        verdict = await port.acquire("user-1", "run-1", spent_credits=500.0, byok=True)
        assert verdict is not None
        assert verdict.granted is False
        assert verdict.ceiling_credits < 10_000.0  # Never 500 + 1_000_000
        assert verdict.quota is not None
        assert verdict.quota["type"] == "dalton_daily_budget"

    cap = await port.capability()
    assert cap is not None
    assert cap["enabled"] is True
    assert cap["mode"] == "dalton-local"


@pytest.mark.asyncio
async def test_enforce_local_admission_denies_when_below_floor_including_byok():
    """G1.1: `enforce_local_admission` must reject when daily cash budget is exhausted
    both for `byok=False` and `byok=True` (L24)."""
    fake_summary = {
        "configured": True,
        "retry_after_seconds": 1800,
        "_decimals": {
            "cap_usd": Decimal("10.00"),
            "spent_usd": Decimal("9.90"),
            "settled_usd": Decimal("9.00"),
            "unreserved_usage_usd": Decimal("0.00"),
            "open_reserved_usd": Decimal("0.90"),
            "expired_conservative_usd": Decimal("0.00"),
        },
    }
    mock_conn = _make_mock_conn()

    with (
        patch("src.dalton.budget.gate.get_db_connection", return_value=mock_conn),
        patch(
            "src.dalton.budget.gate.reconcile_and_summarize_day",
            new=AsyncMock(return_value=fake_summary),
        ),
    ):
        for is_byok in (False, True):
            with pytest.raises(HTTPException) as exc_info:
                await enforce_local_admission("local_dev_user", byok=is_byok)
            assert exc_info.value.status_code == 429
            assert exc_info.value.detail["type"] == "dalton_daily_budget"


def _floor_summary(cap: str, spent: str) -> dict:
    return {
        "configured": True,
        "retry_after_seconds": 1800,
        "_decimals": {
            "cap_usd": Decimal(cap),
            "spent_usd": Decimal(spent),
            "settled_usd": Decimal(spent),
            "unreserved_usage_usd": Decimal("0.00"),
            "open_reserved_usd": Decimal("0.00"),
            "expired_conservative_usd": Decimal("0.00"),
        },
    }


@pytest.mark.asyncio
async def test_admission_floor_semantics_per_turn_and_per_call():
    """R4 B3 regression: the two admission levels keep their original floor semantics.

    Per-turn (`enforce_local_admission`): plain `available < floor_usd` — a cap below
    the default floor ($0.25) can never start a turn; configure a realistic floor/cap.
    Per-call (`_check_cash_budget_admission`): `min(floor, cap)` — an in-flight turn
    may spend down to the cap. Neither may collapse the floor to ~$0 (PR #7 backdoor).
    """
    mock_conn = _make_mock_conn()

    async def run_gate(cap: str, spent: str) -> None:
        with (
            patch("src.dalton.budget.gate.get_db_connection", return_value=mock_conn),
            patch(
                "src.dalton.budget.gate.reconcile_and_summarize_day",
                new=AsyncMock(return_value=_floor_summary(cap, spent)),
            ),
        ):
            await enforce_local_admission("local_dev_user")

    async def call_allowed(cap: str, spent: str) -> bool:
        with (
            patch("src.dalton.llm.complete.get_db_connection", return_value=mock_conn),
            patch(
                "src.dalton.llm.complete.reconcile_and_summarize_day",
                new=AsyncMock(return_value=_floor_summary(cap, spent)),
            ),
        ):
            allowed, _, _ = await _check_cash_budget_admission("local_dev_user")
            return allowed

    # Per-turn: tiny cap below the floor cannot start; healthy cap passes until drained.
    with pytest.raises(HTTPException) as exc_info:
        await run_gate("0.02", "0.00")
    assert exc_info.value.status_code == 429
    await run_gate("0.30", "0.05")
    with pytest.raises(HTTPException):
        await run_gate("0.30", "0.10")

    # Per-call: floor clamps to the cap, never to ~$0.
    assert await call_allowed("0.02", "0.00") is True
    assert await call_allowed("0.02", "0.015") is False
    assert await call_allowed("0.30", "0.05") is True


@pytest.mark.asyncio
async def test_release_passes_generation_to_db():
    """G1.4 / R2-7: `release(..., generation=3)` passes `generation` through to `release_reservation`."""
    port = LocalBudgetPort()
    mock_conn = _make_mock_conn()
    mock_release_res = AsyncMock(return_value=1)

    with (
        patch("src.dalton.budget.port.get_db_connection", return_value=mock_conn),
        patch("src.dalton.budget.port.release_reservation", new=mock_release_res),
        patch(
            "src.dalton.budget.port.reconcile_and_summarize_day",
            new=AsyncMock(return_value={}),
        ),
    ):
        await port.release("user-1", "run-abc", generation=3)
        mock_release_res.assert_awaited_once_with(mock_conn, "run-abc", generation=3)


@pytest.mark.asyncio
async def test_dalton_complete_cache_hit_and_budget_rejection():
    """G1.3 / P0N.7:
    1. Cached `done` entry replays without calling LLM or charging cash_usd.
    2. Cash model call when daily budget is exhausted returns `status='budget_exhausted'` without calling LLM.
    3. `indeterminate` entry does not auto-resend when `resend_indeterminate=False`."""
    mock_llm = create_autospec(LLMService, instance=True)
    mock_llm.complete.return_value = (
        {"verdict": "approve"},
        {"input_tokens": 100, "output_tokens": 50},
    )

    # 1. Cache hit (`done`)
    with patch(
        "src.dalton.llm.complete.inspect_journal_state",
        new=AsyncMock(
            return_value=(
                "done",
                {
                    "invocation_id": "lj1:test",
                    "model_key": "grok-4.7",
                    "provider": "xai",
                    "billing_type": "platform",
                    "response": {"kind": "json", "data": {"verdict": "approve"}},
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "cash_usd": Decimal("0.012"),
                    "notional_usd": Decimal("0"),
                    "attempt": 1,
                },
            )
        ),
    ):
        res = await dalton_complete(
            mock_llm,
            user_id="local-dev-user",
            user_prompt="check this",
            scope_ref="review:1",
            purpose="publish_review",
            model="grok-4.7",
        )
        assert res.ok is True
        assert res.replayed is True
        assert res.cached is True
        assert res.cash_usd == 0.0
        assert res.data == {"verdict": "approve"}
        mock_llm.complete.assert_not_awaited()

    # 2. Budget rejection when cash cap is exceeded
    with (
        patch(
            "src.dalton.llm.complete.inspect_journal_state",
            new=AsyncMock(return_value=("proceed", None)),
        ),
        patch(
            "src.dalton.llm.complete._check_cash_budget_admission",
            new=AsyncMock(return_value=(False, "budget_exhausted", "今日预算 $10.00 已用完")),
        ),
    ):
        res_rejected = await dalton_complete(
            mock_llm,
            user_id="local-dev-user",
            user_prompt="check this",
            scope_ref="review:2",
            purpose="publish_review",
            model="grok-4.7",
        )
        assert res_rejected.ok is False
        assert res_rejected.status == "budget_exhausted"
        assert "已用完" in (res_rejected.error or "")
        mock_llm.complete.assert_not_awaited()

        with pytest.raises(BudgetExhaustedError):
            await dalton_complete(
                mock_llm,
                user_id="local-dev-user",
                user_prompt="check this",
                scope_ref="review:2",
                purpose="publish_review",
                model="grok-4.7",
                raise_on_error=True,
            )

    # 3. Indeterminate handling (no auto-resend)
    with patch(
        "src.dalton.llm.complete.inspect_journal_state",
        new=AsyncMock(
            return_value=(
                "indeterminate",
                {
                    "invocation_id": "lj1:indet",
                    "attempt": 1,
                    "error_kind": "timeout",
                },
            )
        ),
    ):
        res_indet = await dalton_complete(
            mock_llm,
            user_id="local-dev-user",
            user_prompt="check this",
            scope_ref="review:3",
            purpose="publish_review",
            model="grok-4.7",
            resend_indeterminate=False,
        )
        assert res_indet.ok is False
        assert res_indet.status == "indeterminate"
        mock_llm.complete.assert_not_awaited()

        with pytest.raises(JournalIndeterminate):
            await dalton_complete(
                mock_llm,
                user_id="local-dev-user",
                user_prompt="check this",
                scope_ref="review:3",
                purpose="publish_review",
                model="grok-4.7",
                resend_indeterminate=False,
                raise_on_error=True,
            )


@pytest.mark.asyncio
async def test_assist_json_validation_and_conservative_fallback():
    """G0 §0.3: `assist_json` validates JSON against schema and returns `ok=False` on any error without raising."""
    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["supported", "contradicted", "unrelated"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
    }

    # 1. Valid JSON matching schema
    with patch(
        "src.dalton.llm.assist.dalton_complete",
        new=AsyncMock(
            return_value=MagicMock(
                ok=True,
                status="done",
                data=None,
                text='```json\n{"verdict": "supported", "reason": "Matches 10-Q table"}\n```',
                model="gemini-3.8-flash-low-oauth",
                cash_usd=0.0,
                cached=True,
                error=None,
            )
        ),
    ):
        ok_res = await assist_json(
            "audit.judge",
            "Check if figure is supported",
            {"figure": "512M"},
            schema,
        )
        assert isinstance(ok_res, AssistResult)
        assert ok_res.ok is True
        assert ok_res.cached is True
        assert ok_res.data == {"verdict": "supported", "reason": "Matches 10-Q table"}

    # 2. Schema validation failure -> conservative ok=False (never raises)
    with patch(
        "src.dalton.llm.assist.dalton_complete",
        new=AsyncMock(
            return_value=MagicMock(
                ok=True,
                status="done",
                data={"verdict": "invalid_enum_val"},
                text='{"verdict": "invalid_enum_val"}',
                model="gemini-3.8-flash-low-oauth",
                cash_usd=0.0,
                cached=False,
                error=None,
            )
        ),
    ):
        bad_schema_res = await assist_json(
            "audit.judge",
            "Check if figure is supported",
            {"figure": "512M"},
            schema,
        )
        assert bad_schema_res.ok is False
        assert bad_schema_res.data is None
        assert bad_schema_res.error is not None

    # 3. Timeout / exception -> conservative ok=False (never raises)
    with patch(
        "src.dalton.llm.assist.dalton_complete",
        new=AsyncMock(side_effect=asyncio.TimeoutError("timed out after 60s")),
    ):
        timeout_res = await assist_json(
            "audit.judge",
            "Check if figure is supported",
            {"figure": "512M"},
            schema,
        )
        assert timeout_res.ok is False
        assert timeout_res.data is None
        assert "TimeoutError" in (timeout_res.error or "")


@pytest.mark.asyncio
async def test_acquire_byok_grants_bounded_chunk_when_budget_available():
    """G1.1: When daily budget has room, `acquire(..., byok=True)` reserves one chunk ($1.00)
    rather than granting +1,000,000 credits."""
    port = LocalBudgetPort()
    mock_conn = _make_mock_conn()

    mock_cur = MagicMock()
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=None)
    mock_cur.execute = AsyncMock(return_value=None)
    mock_cur.fetchone = AsyncMock(
        side_effect=[
            {
                "run_ref": "run-1",
                "reserved_usd": Decimal("0.50"),
                "observed_usd": Decimal("0.50"),
                "usage_usd": Decimal("0.50"),
                "task_cap_usd": None,
                "generation": 1,
            },
            {"generation": 2},
        ]
    )
    mock_conn.cursor.return_value = mock_cur

    avail_summary = {
        "configured": True,
        "retry_after_seconds": 1800,
        "_raw_rows": [
            {
                "run_ref": "run-1",
                "reserved_usd": Decimal("0.50"),
                "observed_usd": Decimal("0.50"),
                "usage_usd": Decimal("0.50"),
                "charge_usd": Decimal("0.50"),
                "task_cap_usd": None,
                "generation": 1,
            }
        ],
        "_decimals": {
            "cap_usd": Decimal("10.00"),
            "spent_usd": Decimal("0.50"),
            "settled_usd": Decimal("0.00"),
            "unreserved_usage_usd": Decimal("0.00"),
            "open_reserved_usd": Decimal("0.50"),
            "expired_conservative_usd": Decimal("0.00"),
        },
    }

    with (
        patch("src.dalton.budget.port.get_db_connection", return_value=mock_conn),
        patch(
            "src.dalton.budget.port.reconcile_and_summarize_day",
            new=AsyncMock(return_value=avail_summary),
        ),
        patch(
            "src.dalton.budget.port.resolve_user_budget_config",
            new=AsyncMock(
                return_value={
                    "user_id": "user-1",
                    "daily_cap_usd": Decimal("10.00"),
                    "default_task_cap_usd": None,
                    "timezone": "Asia/Shanghai",
                    "configured": True,
                }
            ),
        ),
    ):
        verdict = await port.acquire("user-1", "run-1", spent_credits=500.0, byok=True)
        assert verdict is not None
        assert verdict.granted is True
        # floor=$0.50 + chunk=$1.00 -> $1.50 = 1500 credits (never 1_000_500)
        assert verdict.ceiling_credits == pytest.approx(1500.0)
        assert verdict.generation == 2


def test_d007_migration_and_provider_breakdown():
    from src.dalton.budget.service import _notional_price_index, compute_provider_breakdown
    from src.dalton.migrations.versions import d007_llm_call_journal

    assert d007_llm_call_journal.revision == "d007"
    assert d007_llm_call_journal.down_revision == "d006"

    idx = _notional_price_index()
    assert "claude-opus-5-5-oauth" in idx

    breakdown = compute_provider_breakdown(
        token_usage_rows=[
            {
                "token_usage": {
                    "per_call_costs": [
                        {
                            "model_name": "grok-4.7",
                            "billing_type": "byok",
                            "cost": 0.25,
                            "input_tokens": 1000,
                            "output_tokens": 500,
                        },
                        {
                            "model_name": "claude-opus-5-5-oauth",
                            "billing_type": "oauth",
                            "cost": 0.10,
                            "input_tokens": 1000,
                            "output_tokens": 500,
                        },
                    ]
                }
            }
        ],
        journal_rows=[
            {
                "state": "done",
                "model_key": "deepseek-flash",
                "provider": "deepseek",
                "billing_type": "platform",
                "cash_usd": Decimal("0.02"),
                "notional_usd": Decimal("0"),
            }
        ],
    )
    assert breakdown["xai"]["cash_usd"] == pytest.approx(0.25)
    assert breakdown["claude-oauth"]["cash_usd"] == pytest.approx(0.0)
    assert breakdown["claude-oauth"]["notional_usd"] == pytest.approx(0.10)
    assert breakdown["deepseek"]["cash_usd"] == pytest.approx(0.02)


def test_build_local_run_gate():
    gate = build_local_run_gate("user-1", "run-123", token_callback=MagicMock())
    assert gate is not None
    assert gate.run_ref == "run-123"
    assert build_local_run_gate(None, "run-123", token_callback=MagicMock()) is None


def test_family_of_and_role_policy():
    assert family_of("ocb-openai-gpt-5.4") == "openai-gpt"
    assert family_of("ocb-openai-codex-gpt-5.4-codex") == "openai-gpt"
    assert family_of("ocb-claude-cli-gateway-claude-opus-5-5") == "anthropic-claude"
    assert family_of("ocb-google-gemini-3.8-pro") == "google-gemini"
    assert family_of("ocb-xai-grok-4.7") == "xai-grok"

    res = check_roles()
    assert isinstance(res, list)


def test_openclaw_catalog_tiered_pricing_and_discrepancy():
    catalog = {
        "meta": {"brokerVersion": "2.0.0", "openclawVersion": "2026.10.4"},
        "data": [
            {
                "ref": "google/gemini-3.8-flash",
                "provider": "google",
                "name": "Gemini 3.8 Flash",
                "api": "google-generative-ai",
                "contextWindow": 1000000,
                "maxTokens": 65536,
                "reasoning": True,
                "input": ["text", "image"],
                "toolCalling": True,
                "billing": "cash",
                "cost": {
                    "input": 0.75,
                    "output": 3.75,
                    "cacheRead": 0.075,
                },
            },
            {
                "ref": "openai/gpt-5.4",
                "provider": "openai",
                "name": "GPT 5.4",
                "api": "openai-responses",
                "contextWindow": 1050000,
                "maxTokens": 128000,
                "reasoning": True,
                "input": ["text", "image"],
                "toolCalling": True,
                "billing": "cash",
                "cost": {
                    "input": 2.5,
                    "output": 15.0,
                    "cacheRead": 0.25,
                    "tieredPricing": [
                        {
                            "range": [0, 272001],
                            "input": 2.5,
                            "output": 15.0,
                            "cacheRead": 0.25,
                        },
                        {
                            "range": [272001, -1],
                            "input": 5.0,
                            "output": 22.5,
                            "cacheRead": 0.5,
                        },
                    ],
                },
            },
        ],
    }
    v1_rate_cards = {
        "google/gemini-3.8-flash": {
            "rateCard": {
                "input": 1.5,
                "output": 7.5,
            }
        },
        "openai/gpt-5.4": {
            "rateCard": {
                "input": 2.5,
                "output": 15.0,
            }
        },
    }
    overlay = build_overlay(
        catalog=catalog,
        overrides={},
        v1_rate_cards=v1_rate_cards,
    )
    prov_models = {
        m["id"]: m for m in overlay["providers"]["models"]["openclaw-broker"]
    }
    flash = prov_models["google/gemini-3.8-flash"]
    # Conservative max between 0.75 and 1.5 is 1.5
    assert flash["pricing"]["input"] == 1.5
    assert flash["pricing"]["output"] == 7.5
    assert len(overlay["meta"]["price_discrepancies"]) >= 1

    gpt = prov_models["openai/gpt-5.4"]
    in_tiers = gpt["pricing"]["input_tiers"]
    assert len(in_tiers) == 2
    assert in_tiers[0]["max_tokens"] == 272000
    assert in_tiers[1]["rate"] == 5.0


@pytest.mark.asyncio
async def test_b1_prune_expired_journal_responses_preserves_metadata_and_clears_response():
    """P0H §5 B1: `prune_expired_journal_responses` sets `response = NULL` on >30d `done` rows
    while keeping metadata/cost columns, and `inspect_journal_state` treats pruned `done` rows as `proceed`."""
    from src.dalton.database.llm_journal import (
        inspect_journal_state,
        prune_expired_journal_responses,
    )

    executed_queries: list[tuple[str, tuple]] = []

    class _FakeCursor:
        rowcount = 3

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, sql: str, params: tuple = ()):
            executed_queries.append((sql, params))

        async def fetchone(self):
            return {
                "invocation_id": "lj1:old",
                "request_hash": "hash-1",
                "state": "done",
                "response": None,
                "cash_usd": Decimal("0.05"),
                "notional_usd": Decimal("0"),
            }

    class _FakeConn:
        def cursor(self, **kwargs):
            return _FakeCursor()

    pruned_count = await prune_expired_journal_responses(_FakeConn(), retention_days=30)
    assert pruned_count == 3
    sql_text, sql_params = executed_queries[0]
    assert "SET response = NULL" in sql_text
    assert "DELETE" not in sql_text.upper()
    assert sql_params == (30,)

    action, row = await inspect_journal_state(
        "lj1:old",
        request_hash="hash-1",
        conn=_FakeConn(),
    )
    assert action == "proceed"
    assert row is not None
    assert row["cash_usd"] == Decimal("0.05")


@pytest.mark.asyncio
async def test_b2_fetch_journal_cash_today_fails_closed_and_denies_admission():
    """P0H §5 B2: When `fetch_journal_cash_today` fails in `reconcile_and_summarize_day`,
    it must raise (not return $0) and `enforce_local_admission` must reject admission (503)."""
    from datetime import datetime, timezone
    from unittest.mock import create_autospec
    import src.dalton.budget.service as budget_svc
    from src.dalton.database.budget import fetch_journal_cash_today

    class _BrokenCur:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("relation dalton_llm_journal unavailable")

    now = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="dalton_llm_journal unavailable"):
        await fetch_journal_cash_today(_BrokenCur(), now, now, user_id="u1")

    mock_conn = _make_mock_conn()
    with (
        patch("src.dalton.budget.gate.get_db_connection", return_value=mock_conn),
        patch.object(
            budget_svc,
            "fetch_journal_cash_today",
            new=create_autospec(
                budget_svc.fetch_journal_cash_today,
                side_effect=RuntimeError("journal query failed"),
            ),
        ),
        patch.object(
            budget_svc,
            "resolve_user_budget_config",
            new=create_autospec(
                budget_svc.resolve_user_budget_config,
                return_value={
                    "user_id": "u1",
                    "daily_cap_usd": Decimal("20.00"),
                    "default_task_cap_usd": None,
                    "timezone": "Asia/Shanghai",
                    "configured": True,
                },
            ),
        ),
        patch.object(
            budget_svc,
            "acquire_budget_advisory_lock",
            new=create_autospec(budget_svc.acquire_budget_advisory_lock, return_value=None),
        ),
        patch.object(
            budget_svc,
            "reconcile_reservations_sql",
            new=create_autospec(budget_svc.reconcile_reservations_sql, return_value=None),
        ),
        patch.object(
            budget_svc,
            "fetch_day_reservations",
            new=create_autospec(budget_svc.fetch_day_reservations, return_value=[]),
        ),
        patch.object(
            budget_svc,
            "fetch_unreserved_usage_today",
            new=create_autospec(
                budget_svc.fetch_unreserved_usage_today, return_value=Decimal("0")
            ),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await enforce_local_admission("u1", byok=False)
        assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_b3_scoped_task_cap_and_post_call_indeterminate():
    """P0H §5 B3 & R2-N1 #6:
    1. `_check_cash_budget_admission` rejects when cumulative workspace cash spend >= `task_cap`.
    2. Post-call serialization errors in `dalton_complete` mark the journal row `indeterminate` (never `failed`)."""
    from unittest.mock import create_autospec
    import src.dalton.llm.complete as complete_mod

    mock_conn = _make_mock_conn()
    fake_summary = {
        "configured": True,
        "retry_after_seconds": 1800,
        "_raw_rows": [
            {
                "run_ref": "r1",
                "thread_id": "t-1",
                "workspace_id": "ws-1",
                "charge_usd": Decimal("5.20"),
            }
        ],
        "_decimals": {
            "cap_usd": Decimal("30.00"),
            "spent_usd": Decimal("5.20"),
            "settled_usd": Decimal("5.20"),
            "unreserved_usage_usd": Decimal("0.00"),
            "open_reserved_usd": Decimal("0.00"),
            "expired_conservative_usd": Decimal("0.00"),
        },
    }

    with (
        patch("src.dalton.llm.complete.get_db_connection", return_value=mock_conn),
        patch.object(
            complete_mod,
            "reconcile_and_summarize_day",
            new=create_autospec(
                complete_mod.reconcile_and_summarize_day, return_value=fake_summary
            ),
        ),
        patch.object(
            complete_mod,
            "resolve_user_budget_config",
            new=create_autospec(
                complete_mod.resolve_user_budget_config,
                return_value={"default_task_cap_usd": Decimal("5.00")},
            ),
        ),
        patch.object(
            complete_mod,
            "resolve_effective_task_cap",
            new=create_autospec(
                complete_mod.resolve_effective_task_cap,
                return_value=Decimal("5.00"),
            ),
        ),
    ):
        admitted, kind, msg = await complete_mod._check_cash_budget_admission(
            "u1", workspace_id="ws-1"
        )
        assert admitted is False
        assert kind == "task_cap_exhausted"
        assert "$5.00" in (msg or "")

    # Post-call serialization exception -> state="indeterminate" (not "failed")
    mock_llm = create_autospec(LLMService, instance=True)
    mock_llm.complete.return_value = (
        {"ok": True},
        {"input_tokens": 100, "output_tokens": 50},
    )
    mock_fail = create_autospec(complete_mod.complete_journal_failed, return_value=None)
    with (
        patch.object(
            complete_mod,
            "inspect_journal_state",
            new=create_autospec(
                complete_mod.inspect_journal_state, return_value=("proceed", None)
            ),
        ),
        patch.object(
            complete_mod,
            "_check_cash_budget_admission",
            new=create_autospec(
                complete_mod._check_cash_budget_admission,
                return_value=(True, None, None),
            ),
        ),
        patch.object(
            complete_mod,
            "claim_journal_invocation",
            new=create_autospec(
                complete_mod.claim_journal_invocation,
                return_value=("claimed", {"attempt": 1}),
            ),
        ),
        patch.object(
            complete_mod,
            "_serialize_response_payload",
            side_effect=ValueError("unserializable custom object"),
        ),
        patch.object(complete_mod, "complete_journal_failed", new=mock_fail),
        patch.object(
            complete_mod,
            "record_provider_event",
            new=create_autospec(complete_mod.record_provider_event, return_value=None),
        ),
    ):
        res = await dalton_complete(
            mock_llm,
            user_id="u1",
            user_prompt="ping",
            model="grok-4.7",
        )
        assert res.ok is False
        assert res.status == "indeterminate"
        assert res.error_kind == "post_call_error"
        mock_fail.assert_awaited_once()
        assert mock_fail.await_args.kwargs["state"] == "indeterminate"


@pytest.mark.asyncio
async def test_p0i_i3_startup_fail_fast_and_journal_pruning_and_oauth_events(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """P0I §3 P1, P2, P3, P4, P6 verification."""
    from datetime import datetime, timedelta, timezone

    from src.dalton.autonomy.probes import director as director_mod
    from src.dalton.budget import gate as gate_mod
    from src.dalton.database import llm_journal as journal_mod
    from src.dalton.database import provider_events as pe_mod
    from src.dalton.llm import complete as complete_mod
    from src.dalton.llm.providers.antigravity import oauth as ag_oauth
    from src.dalton.llm.role_policy import RolePolicyError
    from src.dalton.services import provider_status as ps_mod
    from src.server.services import credit_gate_port as cgp_mod

    # P1a: RolePolicyError propagates out of verify_local_wiring / verify_credit_gate_wiring
    with (
        patch.object(
            gate_mod,
            "local_gate_enabled",
            return_value=True,
        ),
        patch(
            "src.dalton.llm.role_policy.enforce_role_policy_on_startup",
            side_effect=RolePolicyError("assist role configured with contributor-oauth"),
        ),
    ):
        with pytest.raises(RolePolicyError, match="contributor-oauth"):
            await cgp_mod.verify_credit_gate_wiring()

    # P1b: Missing Dalton migration when DALTON_BUDGET_GATE=1 raises instead of swallowing
    mock_conn = _make_mock_conn()
    mock_cur = MagicMock()
    mock_cur.__aenter__ = AsyncMock(return_value=mock_cur)
    mock_cur.__aexit__ = AsyncMock(return_value=None)
    mock_cur.execute = AsyncMock(side_effect=RuntimeError("relation alembic_version_dalton does not exist"))
    mock_conn.cursor = MagicMock(return_value=mock_cur)

    with (
        patch("src.dalton.llm.role_policy.enforce_role_policy_on_startup", return_value=[]),
        patch.object(gate_mod, "is_dalton_budget_gate_enabled", return_value=True),
        patch.object(gate_mod, "get_db_connection", return_value=mock_conn),
    ):
        with pytest.raises(RuntimeError, match="alembic_version_dalton"):
            await gate_mod.verify_local_wiring()

    # P2: Daily director preflight calls prune_expired_journal_responses(retention_days=30)
    mock_prune = create_autospec(
        journal_mod.prune_expired_journal_responses, return_value=4
    )
    mock_dir_conn = _make_mock_conn()
    mock_dir_cur = MagicMock()
    mock_dir_cur.__aenter__ = AsyncMock(return_value=mock_dir_cur)
    mock_dir_cur.__aexit__ = AsyncMock(return_value=None)
    mock_dir_cur.execute = AsyncMock()
    mock_dir_cur.fetchall = AsyncMock(return_value=[])
    mock_dir_cur.fetchone = AsyncMock(return_value={"cnt": 0})
    mock_dir_conn.cursor = MagicMock(return_value=mock_dir_cur)

    with (
        patch(
            "src.dalton.database.publications.expire_stale_publications",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            journal_mod,
            "prune_expired_journal_responses",
            new=mock_prune,
        ),
        patch(
            "src.server.database.pool.get_db_connection",
            return_value=mock_dir_conn,
        ),
    ):
        await director_mod.probe_director(
            kind="director_daily",
            watch_state={},
            persist=True,
        )
        mock_prune.assert_awaited_once_with(retention_days=30)

    # P3: Antigravity OAuth does not have _LAST_INITIATED_AT dict and reads oauth_login_start from DB
    assert not hasattr(ag_oauth, "_LAST_INITIATED_AT")
    t0 = datetime(2026, 10, 19, 5, 0, 0, tzinfo=timezone.utc)
    fake_cache = MagicMock()
    fake_cache.enabled = False
    fake_cache.client = None
    with (
        patch("src.utils.cache.redis_cache.get_cache_client", return_value=fake_cache),
        patch.object(
            ag_oauth,
            "get_oauth_tokens",
            new=create_autospec(
                ag_oauth.get_oauth_tokens,
                return_value={
                    "access_token": "tok-old",
                    "updated_at": t0 - timedelta(minutes=5),
                },
            ),
        ),
        patch.object(
            pe_mod,
            "list_recent_provider_events",
            new=create_autospec(
                pe_mod.list_recent_provider_events,
                return_value=[
                    {
                        "user_id": "u-oauth",
                        "kind": "oauth_login_start",
                        "detail": {"initiated_at": t0.isoformat()},
                        "created_at": t0,
                    }
                ],
            ),
        ),
    ):
        # Token updated_at is older than oauth_login_start -> False
        assert await ag_oauth.check_login_complete("u-oauth") is False

    # P4: provider_status logs debug when get_user_api_keys raises
    with (
        caplog.at_level("DEBUG"),
        patch(
            "src.server.database.api_keys.get_user_api_keys",
            side_effect=RuntimeError("db down"),
        ),
        patch.object(
            ps_mod,
            "_fetch_usage_stats_24h",
            new=create_autospec(ps_mod._fetch_usage_stats_24h, return_value={}),
        ),
        patch.object(
            ps_mod,
            "list_recent_provider_events",
            new=create_autospec(ps_mod.list_recent_provider_events, return_value=[]),
        ),
        patch.object(
            ps_mod,
            "count_provider_errors_24h",
            new=create_autospec(ps_mod.count_provider_errors_24h, return_value={}),
        ),
    ):
        await ps_mod.get_providers_status(user_id="u-test")
        assert any("get_user_api_keys failed" in rec.message for rec in caplog.records)

    # P6: complete_journal_failed raising DB error logs both exceptions and still returns/raises via _finish
    mock_llm_fail = create_autospec(LLMService, instance=True)
    mock_llm_fail.complete.side_effect = TimeoutError("upstream llm timed out")
    mock_journal_fail_boom = create_autospec(
        complete_mod.complete_journal_failed,
        side_effect=RuntimeError("db connection lost during journal fail write"),
    )
    with (
        caplog.at_level("ERROR"),
        patch.object(
            complete_mod,
            "inspect_journal_state",
            new=create_autospec(
                complete_mod.inspect_journal_state, return_value=("proceed", None)
            ),
        ),
        patch.object(
            complete_mod,
            "_check_cash_budget_admission",
            new=create_autospec(
                complete_mod._check_cash_budget_admission,
                return_value=(True, None, None),
            ),
        ),
        patch.object(
            complete_mod,
            "claim_journal_invocation",
            new=create_autospec(
                complete_mod.claim_journal_invocation,
                return_value=("claimed", {"attempt": 1}),
            ),
        ),
        patch.object(
            complete_mod, "complete_journal_failed", new=mock_journal_fail_boom
        ),
        patch.object(
            complete_mod,
            "record_provider_event",
            new=create_autospec(complete_mod.record_provider_event, return_value=None),
        ),
    ):
        with pytest.raises(JournalIndeterminate, match="upstream llm timed out"):
            await dalton_complete(
                mock_llm_fail,
                user_id="u1",
                user_prompt="ping",
                model="grok-4.7",
                raise_on_error=True,
            )
        assert any(
            "complete_journal_failed failed" in rec.message
            and "upstream llm timed out" in rec.message
            for rec in caplog.records
        )


