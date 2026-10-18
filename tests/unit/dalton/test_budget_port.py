"""Unit tests for Dalton budget port, admission gate, and OpenClaw catalog overlay (P0.4–P0.8)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.dalton.budget.gate import build_local_run_gate, enforce_local_admission
from src.dalton.budget.port import (
    LocalBudgetPort,
    format_daily_budget_exhausted_message,
    format_task_cap_exhausted_message,
)
from src.dalton.budget.service import _local_day_bounds
from src.dalton.llm.openclaw_catalog import build_overlay
from src.dalton.llm.role_policy import check_roles, family_of


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

    task = format_task_cap_exhausted_message(Decimal("5.00"), Decimal("5.00"))
    assert "本任务预算 $5.00 已用完" in task


@pytest.mark.asyncio
async def test_local_budget_port_byok_grants_immediately():
    port = LocalBudgetPort()
    verdict = await port.acquire("user-1", "run-1", spent_credits=500.0, byok=True)
    assert verdict is not None
    assert verdict.granted is True
    assert verdict.ceiling_credits > 500.0

    cap = await port.capability()
    assert cap is not None
    assert cap["enabled"] is True
    assert cap["mode"] == "dalton-local"


@pytest.mark.asyncio
async def test_enforce_local_admission_denies_when_below_floor():
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
    mock_conn = MagicMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)
    mock_tx = MagicMock()
    mock_tx.__aenter__ = AsyncMock(return_value=None)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    mock_conn.transaction.return_value = mock_tx

    with (
        patch("src.dalton.budget.gate.get_db_connection", return_value=mock_conn),
        patch(
            "src.dalton.budget.gate.reconcile_and_summarize_day",
            new=AsyncMock(return_value=fake_summary),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await enforce_local_admission("local_dev_user", byok=False)
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["type"] == "dalton_daily_budget"


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

