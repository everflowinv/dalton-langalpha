"""Tests for the Bash tool's mcp_trace artifact (closes the bash provenance bypass).

The Bash tool returns ``(content, {"mcp_trace": [...]})`` so a script it runs that
calls MCP wrappers records the same provenance ExecuteCode does. These pin the
artifact wiring: trace passed through on success and failure, empty on a plain
command or a blocked memory path. Neutral placeholder data only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ptc_agent.agent.tools.bash import create_execute_bash_tool


def _make_backend(result: dict) -> Any:
    backend = SimpleNamespace()
    backend.filesystem_config = SimpleNamespace(working_directory="/home/workspace")
    backend.aexecute_bash = AsyncMock(return_value=result)
    backend.livefs = None
    backend.settled_livefs = AsyncMock(return_value=None)
    backend.provider = "docker"
    backend.channel = "docker"
    return backend


def _tool_call(command: str) -> dict:
    # Invoking with a ToolCall (not a bare args dict) makes a content_and_artifact
    # tool return a ToolMessage carrying .artifact, so we can assert on it.
    return {
        "type": "tool_call",
        "name": "Bash",
        "args": {"command": command},
        "id": "bash-1",
    }


class TestBashMcpTraceArtifact:
    @pytest.mark.asyncio
    async def test_success_surfaces_mcp_trace_on_artifact(self):
        trace = [{"server": "marketdata", "tool": "quote", "result_sha256": "a" * 64}]
        tool = create_execute_bash_tool(
            _make_backend(
                {
                    "success": True,
                    "stdout": "ok",
                    "stderr": "",
                    "exit_code": 0,
                    "mcp_trace": trace,
                }
            )
        )
        msg = await tool.ainvoke(_tool_call("python analysis.py"))
        assert msg.artifact == {"mcp_trace": trace}
        assert "ok" in msg.content

    @pytest.mark.asyncio
    async def test_failure_still_carries_mcp_trace(self):
        # A non-zero exit after a successful in-sandbox MCP call must keep the trace.
        trace = [{"server": "finance", "tool": "get_prices", "result_sha256": "b" * 64}]
        tool = create_execute_bash_tool(
            _make_backend(
                {
                    "success": False,
                    "stdout": "boom",
                    "stderr": "",
                    "exit_code": 1,
                    "mcp_trace": trace,
                }
            )
        )
        msg = await tool.ainvoke(_tool_call("python analysis.py"))
        assert msg.artifact == {"mcp_trace": trace}
        assert msg.content.startswith("ERROR")

    @pytest.mark.asyncio
    async def test_plain_command_has_empty_trace(self):
        tool = create_execute_bash_tool(
            _make_backend(
                {"success": True, "stdout": "", "stderr": "", "exit_code": 0}
            )
        )
        msg = await tool.ainvoke(_tool_call("mkdir foo"))
        assert msg.artifact == {"mcp_trace": []}

    @pytest.mark.asyncio
    async def test_memory_path_block_returns_empty_trace_and_skips_backend(self):
        backend = _make_backend({"success": True, "stdout": "", "exit_code": 0})
        tool = create_execute_bash_tool(backend)
        msg = await tool.ainvoke(_tool_call("cat .agents/user/memory/secret.md"))
        assert msg.artifact == {"mcp_trace": []}
        assert "ERROR" in msg.content
        backend.aexecute_bash.assert_not_called()


class TestBashTimeout:
    """A timeout the model passes in seconds must not become no deadline."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("timeout", "seconds"),
        [
            (30, 30),  # seconds by mistake: read as seconds, not truncated to 0
            (999, 600),
            (1500, 1),
            (45000, 45),
            (600000, 600),
            (3600000, 600),  # past the documented max
            (None, 120),
            (0, 120),
        ],
    )
    async def test_sandbox_gets_a_bounded_deadline(self, timeout, seconds):
        backend = _make_backend({"success": True, "stdout": "ok", "stderr": "", "exit_code": 0})
        tool = create_execute_bash_tool(backend)
        call = _tool_call("sleep 1")
        call["args"]["timeout"] = timeout
        await tool.ainvoke(call)
        assert backend.aexecute_bash.await_args.kwargs["timeout"] == seconds


class TestBashSandboxOutcome:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("execute", "outcome", "error_class"),
        [
            pytest.param(
                AsyncMock(return_value={"success": False, "stdout": "boom", "exit_code": 1}),
                "ok",
                None,
                id="nonzero-exit-is-the-agents",
            ),
            pytest.param(
                AsyncMock(
                    return_value={
                        "success": False,
                        "stdout": "",
                        "exit_code": -1,
                        "failure": "timeout",
                    }
                ),
                "unavailable",
                "timeout",
                id="timeout",
            ),
            pytest.param(
                AsyncMock(side_effect=ConnectionError("sandbox gone")),
                "error",
                "ConnectionError",
                id="dead-sandbox",
            ),
        ],
    )
    async def test_only_the_sandbox_failing_marks_the_run(
        self, traced_tool, execute, outcome, error_class
    ):
        """Every run names its provider, so an error rate reads per provider."""
        backend = _make_backend({})
        backend.aexecute_bash = execute

        msg, metadata = await traced_tool(
            create_execute_bash_tool(backend), _tool_call("python run.py")
        )

        assert msg.content.startswith("ERROR")
        assert metadata["tool_outcome"] == outcome
        assert metadata.get("error_class") == error_class
        assert metadata["tool_provider"] == "docker"
