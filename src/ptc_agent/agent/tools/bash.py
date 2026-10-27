"""Execute bash commands in the sandbox."""

from typing import Any

import structlog
from langchain_core.tools import BaseTool, tool

from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.agent.tools.mount_guard import run_guarded
from ptc_agent.agent.tools.utils import mark_sandbox_failed
from ptc_agent.core.paths import (
    MEMO_USER_DIR,
    MEMORY_USER_DIR,
    WorkspaceLayout,
)
from ptc_agent.core.sandbox import livefs_mount
from src.observability import TOOL_CHANNEL, TOOL_PROVIDER, mark_tool_failed

logger = structlog.get_logger(__name__)

# Memory and memo live in the store, which the sandbox sees only while the
# file mount serves it. Without the mount, refuse those paths so the agent
# routes them through the file tools instead of silently dropping writes on
# the sandbox (which would also let the agent fabricate fake memos invisible
# to the UI).
_MEMORY_ROUTE_ERROR = (
    f"ERROR: Store-backed paths ({MEMORY_USER_DIR}/**, "
    f"{WorkspaceLayout.MEMORY_DIR}/**, "
    f"{MEMO_USER_DIR}/**) are managed by the long-term memory/memo system and "
    "are NOT on the workspace filesystem. Use the Write, Edit, Read, Glob, or "
    "Grep file tools for these paths so they route to the store. Memo paths are "
    "additionally read-only to the agent — ask the user to upload via the memo "
    "panel. Bash will not see or persist memory or memo content."
)


# The automation files are rows in the database, which the sandbox sees only
# while the file mount serves them.
_FILES_ROUTE_ERROR = (
    "ERROR: {dir}/** is kept on the server, not on the sandbox filesystem, and no "
    "file mount serves it to Bash right now. Use Read, Edit and Write on the files in "
    "{dir}/. Bash can't see or change them."
)


# A directory only the file tools reach, mounted or not.
_FILE_TOOLS_ONLY_ERROR = (
    "ERROR: {dir}/ is reachable only through the file tools. Use Read, Edit and "
    "Write on the files in {dir}/. Bash can't see or change them."
)


_DEFAULT_TIMEOUT_S = 120
_MAX_TIMEOUT_S = 600


def _timeout_seconds(timeout: int | None) -> int:
    """The command's deadline in whole seconds, between 1 and 600.

    The argument is documented in milliseconds, but a model often passes
    seconds. Truncating 30 to 0 meant no deadline at all to the provider, so a
    foreground command could hold the turn until the sandbox paused. A value
    under 1000 is read as seconds: no shell command is meant to get less.
    """
    if not timeout or timeout < 0:
        return _DEFAULT_TIMEOUT_S
    seconds = timeout if timeout < 1000 else timeout // 1000
    return min(seconds, _MAX_TIMEOUT_S)


def create_execute_bash_tool(
    backend: SandboxBackend,
    thread_id: str = "",
    *,
    call_context: livefs_mount.CallContext | None = None,
    file_tools_only: tuple[str, ...] = (),
    allowed_servers: frozenset[str] | None = None,
) -> BaseTool:
    """Factory function to create Bash tool with injected dependencies.

    Args:
        backend: SandboxBackend wrapping the sandbox
        thread_id: Short thread ID (first 8 chars) for thread-scoped script storage
        call_context: Who the command runs for, which a save through the file
            mount reads its defaults from
        file_tools_only: Directories under the sandbox root only the file
            tools reach; a command naming one is refused before it runs
        allowed_servers: The only MCP servers a script it runs may call, for
            a subagent the user limited; None leaves the workspace's whole set

    Returns:
        Configured Bash tool function
    """

    @tool("Bash", response_format="content_and_artifact")
    async def Bash(
        command: str,
        description: str | None = None,
        timeout: int | None = 120000,
        run_in_background: bool | None = False,
        working_dir: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Execute bash commands in a fresh shell in your workspace folder.

        Use for: git, npm, docker, system commands, directory operations,
        and running Python scripts written to files.
        NOT for: reading/writing/editing files - use Read/Write/Edit tools instead

        Args:
            command: The bash command to execute. Quote paths containing spaces.
            description: Brief description (5-10 words, active voice)
            timeout: Milliseconds (default: 120000, max: 600000)
            run_in_background: Run asynchronously (default: False). Use it for
                anything likely to outlast the timeout — a backtest, a bulk pull
                across many tickers, a dashboard server you need to keep alive.
            working_dir: Where to run (default: your workspace folder, which
                relative paths in the command resolve against)

        Returns:
            Combined stdout and stderr, or an ERROR message.

        With run_in_background=True it returns a command_id instead of output; read
        that with BashOutput.
        """
        return await run_guarded(
            backend,
            command,
            call_context,
            lambda call_id: _run(command, working_dir, timeout, run_in_background, call_id),
            memory_error=_MEMORY_ROUTE_ERROR,
            files_error=_FILES_ROUTE_ERROR,
            file_tools_only=file_tools_only,
            file_tools_error=_FILE_TOOLS_ONLY_ERROR,
            blocked_event="Blocked bash command touching a store-backed path",
            command_length=len(command),
        )

    async def _run(
        command: str,
        working_dir: str | None,
        timeout: int | None,
        run_in_background: bool | None,
        call_id: str | None,
    ) -> tuple[str, dict[str, Any]]:
        try:
            logger.debug(
                "Executing bash command",
                command_length=len(command),
                working_dir=working_dir,
                timeout=timeout,
                background=run_in_background,
                thread_id=thread_id or None,
            )

            # Execute bash command in sandbox
            result = await backend.aexecute_bash(
                command,
                working_dir=working_dir,
                timeout=_timeout_seconds(timeout),
                background=bool(run_in_background),
                thread_id=thread_id or None,
                call_id=call_id,
                allowed_servers=allowed_servers,
            )

            # Provenance for any MCP calls a script run here made (foreground
            # only; absent/empty for plain shell commands). Stripped from the
            # artifact by the provenance middleware before it leaves the host.
            artifact = {"mcp_trace": list(result.get("mcp_trace") or [])}

            if result["success"]:
                stdout = result.get("stdout", "")
                stderr = result.get("stderr", "")

                # Combine stdout and stderr for complete output
                output = stdout
                if stderr:
                    output += f"\n{stderr}" if output else stderr

                if output:
                    logger.debug(
                        "Bash command executed successfully",
                        command_length=len(command),
                        output_length=len(output),
                    )
                    return output, artifact
                # Command succeeded but no output (e.g., mkdir)
                logger.debug(
                    "Bash command executed successfully (no output)",
                    command_length=len(command),
                )
                return "Command completed successfully", artifact

            if result.get("failure"):
                mark_sandbox_failed(result["failure"])
            # Command failed — Daytona returns combined stdout+stderr in "stdout"
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            error_output = stderr or stdout or "Command execution failed (no output)"
            exit_code = result.get("exit_code", -1)

            logger.warning(
                "Bash command failed",
                command_length=len(command),
                exit_code=exit_code,
                output_length=len(error_output),
            )

            return (
                f"ERROR: Command failed (exit code {exit_code})\n{error_output}",
                artifact,
            )

        except Exception as e:
            error_msg = f"Failed to execute bash command: {e!s}"
            logger.error(
                error_msg,
                command_length=len(command),
                error=str(e),
                exc_info=True,
            )
            mark_tool_failed(e)
            return f"ERROR: {error_msg}", {"mcp_trace": []}

    # Every run, not only a failed one, so an error rate reads per provider.
    Bash.metadata = {
        TOOL_PROVIDER: backend.provider,
        TOOL_CHANNEL: backend.channel,
    }
    return Bash
