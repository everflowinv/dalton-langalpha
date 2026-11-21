"""Background-slot calls that name no reasoning level run at low (user decision 2026-11-21)."""

from unittest.mock import create_autospec, patch

import pytest

from src.server.services.llm import config as cfg_mod
from tests.unit.server.services.llm.test_claude_code_routing import _resolve


async def _effort_passed(slot: str, pref: dict | None = None, **turn) -> str | None:
    real = cfg_mod.resolve_clients
    spy = create_autospec(real, side_effect=real)
    with patch.object(cfg_mod, "resolve_clients", spy):
        await _resolve(pref or {}, slot=slot, **turn)
    assert spy.await_count == 1
    return spy.await_args.kwargs["reasoning_effort"]


@pytest.mark.asyncio
async def test_background_slot_defaults_to_low():
    assert await _effort_passed("background") == "low"


@pytest.mark.asyncio
async def test_background_slot_keeps_an_explicit_level():
    assert await _effort_passed("background", reasoning_effort="high") == "high"


@pytest.mark.asyncio
async def test_background_call_that_names_its_model_is_unchanged():
    """Dalton assist calls (number audit, language review, dedup) name their model."""
    assert await _effort_passed("background", request_model="claude-sonnet-5-5") is None


@pytest.mark.asyncio
async def test_primary_slot_is_unchanged():
    assert await _effort_passed("primary") is None


@pytest.mark.asyncio
async def test_a_level_the_user_set_for_the_model_wins():
    pref = {"profiles": {"claude-haiku-5-5": {"reasoning_effort": "high"}}}
    assert await _effort_passed("background", pref) is None


def test_models_without_a_reasoning_ladder_are_untouched():
    assert cfg_mod.background_default_effort({}, "no-such-model") is None
    assert cfg_mod.background_default_effort({}, None) is None
    assert cfg_mod.background_default_effort({}, "claude-haiku-5-5") in ("low", None)
