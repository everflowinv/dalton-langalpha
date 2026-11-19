"""The locked re-check of an automation update runs end to end when the model changes.

Regression: the re-check called two helpers upstream had removed, so changing an
automation's model raised NameError (500) instead of updating it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

from src.server.models.automation import AutomationUpdate
from src.server.services.automations import lifecycle

WS = "11111111-1111-1111-1111-111111111111"


def _run(update: AutomationUpdate, locked_row: dict, current: dict, monkeypatch):
    hosted = create_autospec(lifecycle.check_claude_code_hosted)
    monkeypatch.setattr(lifecycle, "check_claude_code_hosted", hosted)
    asyncio.run(
        lifecycle.recheck_locked_automation_update(
            user_id="u1",
            update=update,
            current=current,
            stored_in=WS,
            stored_pin=None,
            new_cron=None,
            new_tz=None,
            pref_for_lock=None,
            locked_row=locked_row,
            effective_kwargs={"llm_model": update.llm_model},
            locked_conn=None,
        )
    )
    return hosted


def test_model_change_rechecks_hosting_without_name_error(monkeypatch) -> None:
    row = {"workspace_id": WS, "llm_model": "gpt-6.1-sol-oauth", "thread_strategy": "new", "trigger_type": "cron"}
    # The row changed under the lock (another writer moved its model), so the re-check runs.
    locked = {**row, "llm_model": "gpt-6-astra-oauth"}
    hosted = _run(AutomationUpdate(llm_model="gpt-6.2-sol-oauth"), locked, row, monkeypatch)
    hosted.assert_awaited_once()
    assert hosted.await_args.args[1] == "gpt-6.2-sol-oauth"


def test_continue_strategy_model_change_runs_through(monkeypatch) -> None:
    row = {"workspace_id": WS, "llm_model": "gpt-6.1-sol-oauth", "thread_strategy": "continue", "trigger_type": "cron"}
    locked = {**row, "llm_model": "gpt-6-astra-oauth"}
    _run(AutomationUpdate(llm_model="gpt-6.2-sol-oauth"), locked, row, monkeypatch)
