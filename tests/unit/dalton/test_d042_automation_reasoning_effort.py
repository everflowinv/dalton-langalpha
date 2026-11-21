"""Tests for Dalton migration d042 (automations.reasoning_effort)."""

from __future__ import annotations

from unittest.mock import create_autospec

from alembic.operations import Operations

from src.dalton.migrations.versions import d042_automation_reasoning_effort as d042
from src.llms.reasoning import REASONING_LEVELS


def test_d042_revision_chain_and_upgrade_downgrade_sql(monkeypatch) -> None:
    assert d042.revision == "d042"
    assert d042.down_revision == "d041"
    assert d042.__doc__ and "Downgrade / Rollback Notes" in d042.__doc__
    assert "automation_id = '<verified_automation_id>'" in d042.__doc__
    assert "config_version = <verified_config_version>" in d042.__doc__
    assert "LIKE '%-oauth'" not in d042.__doc__

    executed_up: list[str] = []
    op_mock = create_autospec(Operations, instance=True)
    op_mock.execute.side_effect = lambda sql: executed_up.append(str(sql))
    monkeypatch.setattr(d042, "op", op_mock)

    d042.upgrade()
    assert any("SET LOCAL lock_timeout = '5s'" in s for s in executed_up)
    alter_up = "\n".join(executed_up)
    assert "ALTER TABLE automations" in alter_up
    assert "ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(16)" in alter_up
    assert "ck_automations_reasoning_effort" in alter_up
    for level in REASONING_LEVELS:
        assert f"'{level}'" in alter_up

    executed_down: list[str] = []
    op_mock.execute.side_effect = lambda sql: executed_down.append(str(sql))
    d042.downgrade()
    assert any("SET LOCAL lock_timeout = '5s'" in s for s in executed_down)
    assert any(
        "ALTER TABLE automations DROP COLUMN IF EXISTS reasoning_effort" in s
        for s in executed_down
    )
