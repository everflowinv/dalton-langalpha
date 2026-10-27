"""Get output and status of background bash commands."""

from typing import Any, Literal

import structlog
from langchain_core.tools import BaseTool, tool

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.agent.limits import pending_read
from ptc_agent.agent.tools.utils import note_sandbox_exit
from ptc_agent.core.sandbox import livefs_mount
from src.observability import TOOL_CHANNEL, TOOL_PROVIDER, mark_tool_failed

logger = structlog.get_logger(__name__)


def create_bash_output_tool(
    backend: SandboxBackend,
    *,
    call_context: livefs_mount.CallContext | None = None,
) -> BaseTool:
    """Factory function to create BashOutput tool with injected dependencies.

    Args:
        backend: SandboxBackend wrapping the sandbox
        call_context: Who the background command runs for, whose thread's
            saves through the file mount after its launch report here

    Returns:
        Configured BashOutput tool function
    """

    @tool("BashOutput", response_format="content_and_artifact")
    async def BashOutput(
        command_id: str, action: Literal["status", "stop"] = "status"
    ) -> tuple[str, dict[str, Any]]:
        """Get the output/status of a background command, or stop it.

        Use this to check on commands started with run_in_background=True.

        Args:
            command_id: The command_id returned when the background command was started
            action: "status" (default) to check output, or "stop" to terminate the command

        Returns:
            Status and output of the background command, or confirmation of stop.
        """
        # A background command saves after the Bash call that launched it
        # returned, so what its saves reported waits on its thread's late
        # list; reading the job is where the agent learns what it did.
        return await livefs_mount.through_mount(
            backend.livefs, call_context, lambda _call_id: _read(command_id, action)
        )

    async def _read(command_id: str, action: str) -> tuple[str, dict[str, Any]]:
        try:
            if action == "stop":
                try:
                    stopped = await backend.astop_background_command(command_id)
                except Exception as e:
                    mark_tool_failed(e)
                    logger.warning(
                        "Failed to stop background command",
                        command_id=command_id,
                        error=str(e),
                    )
                    return (
                        f"ERROR: Could not stop background command {command_id}, "
                        f"so it may still be running: {e!s}",
                        {"mcp_trace": []},
                    )
                if stopped:
                    msg = f"Background command {command_id} stopped."
                else:
                    msg = f"No running background command found with id {command_id}."
                return msg, {"mcp_trace": []}

            result = await backend.aget_background_command_status(command_id)
            if not result.get("found", True):
                return (
                    f"No background command found with id {command_id}. A status "
                    "check that saw it finish already returned its output, or the "
                    "computer restarted since it started.",
                    {"mcp_trace": []},
                )

            is_running = result["is_running"]
            exit_code = result["exit_code"]
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            # MCP provenance for a backgrounded script's calls — present only on
            # the status read that observes completion. Stripped from the
            # artifact by the provenance middleware before it leaves the host.
            artifact = {"mcp_trace": list(result.get("mcp_trace") or [])}

            # Format status line
            if is_running:
                status = "RUNNING"
                # How the loop guard tells a poll from progress: stdout
                # grows, so no two reads of a running command look alike.
                artifact = pending_read(artifact)
            elif exit_code == 0:
                status = "COMPLETED (success)"
            else:
                status = f"COMPLETED (exit code {exit_code})"
            if not is_running:
                # The one read that sees it finish: the session goes with it.
                await note_sandbox_exit(backend, exit_code, result.get("failure"))

            parts = [f"Status: {status}"]
            if stdout:
                parts.append(f"Output:\n{stdout}")
            if stderr:
                parts.append(f"Errors:\n{stderr}")

            return "\n".join(parts), artifact

        except Exception as e:
            mark_tool_failed(e)
            error_msg = f"Failed to get background command output: {e!s}"
            logger.error(error_msg, command_id=command_id, exc_info=True)
            return f"ERROR: {error_msg}", {"mcp_trace": []}

    # Every run, as for Bash, so an error rate reads per provider.
    BashOutput.metadata = {
        TOOL_PROVIDER: backend.provider,
        TOOL_CHANNEL: backend.channel,
    }
    return BashOutput
