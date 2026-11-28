"""Tests that sandbox job env advertises Drive / Drive-memo availability to `la` CLI."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.config import features as features_mod
from src.ptc_agent.core.sandbox import execution as exec_mod
from src.ptc_agent.core.sandbox.jobs import JobContext, JobRef


def _make_stub_sandbox() -> SimpleNamespace:
    ws = SimpleNamespace(
        mcp_client_config="/work/mcp.json",
        join=lambda rel: f"/work/{rel}",
    )
    return SimpleNamespace(
        layout=SimpleNamespace(cli_token="/run/cli.token"),
        workspace=lambda: ws,
    )


def test_job_env_advertises_drive_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    flag_spy = create_autospec(
        features_mod.system_flag,
        return_value=SimpleNamespace(enabled=False),
    )
    monkeypatch.setattr(features_mod, "system_flag", flag_spy)

    env = exec_mod._job_env(
        _make_stub_sandbox(),
        JobRef(
            command_id="j1",
            state_dir="/tmp/j1",
            log_path="/tmp/j1/out.log",
            trace_path="/tmp/j1/trace.jsonl",
            log_relpath=".agents/threads/t1/commands/j1.log",
            call_id="c1",
        ),
        JobContext(thread_id="t1", call_id="c1", env={}),
    )

    assert env["LANGALPHA_DRIVE_ENABLED"] == "0"
    assert env["LANGALPHA_DRIVE_MEMOS_ENABLED"] == "0"


def test_job_env_advertises_drive_enabled_when_feature_on(monkeypatch: pytest.MonkeyPatch) -> None:
    flag_spy = create_autospec(
        features_mod.system_flag,
        return_value=SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(features_mod, "system_flag", flag_spy)

    env = exec_mod._job_env(
        _make_stub_sandbox(),
        JobRef(
            command_id="j2",
            state_dir="/tmp/j2",
            log_path="/tmp/j2/out.log",
            trace_path="/tmp/j2/trace.jsonl",
            log_relpath=".agents/threads/t2/commands/j2.log",
            call_id="c2",
        ),
        JobContext(thread_id="t2", call_id="c2", env={}),
    )

    assert env["LANGALPHA_DRIVE_ENABLED"] == "1"
    assert env["LANGALPHA_DRIVE_MEMOS_ENABLED"] == "1"
