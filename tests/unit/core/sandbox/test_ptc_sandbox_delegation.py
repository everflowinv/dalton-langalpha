"""
Tests for PTCSandbox delegation to runtime/provider after refactor.

Verifies that PTCSandbox routes operations through the abstract
SandboxRuntime/SandboxProvider interfaces rather than calling
the Daytona SDK directly.

Covers:
- execute_bash_command -> runtime.exec
- aupload_file_bytes -> runtime.upload_file
- adownload_file_bytes -> runtime.download_file
- als_directory -> runtime.list_files
- stop_sandbox -> runtime.stop
- cleanup -> runtime.delete + provider.close
- close -> provider.close
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ptc_agent.config.core import (
    CoreConfig,
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.sandbox.runtime import (
    CodeRunResult,
    ExecResult,
    RuntimeState,
    SandboxGoneError,
    SandboxRuntime,
)
from tests.unit.core.sandbox.fake_provider import fake_provider


def _make_config(**overrides) -> CoreConfig:
    defaults = dict(
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        security=SecurityConfig(),
        mcp=MCPConfig(),
        logging=LoggingConfig(),
        filesystem=FilesystemConfig(),
    )
    defaults.update(overrides)
    return CoreConfig(**defaults)


@pytest.fixture
def mock_runtime():
    runtime = AsyncMock(spec=SandboxRuntime)
    runtime.id = "mock-runtime-1"
    runtime.working_dir = "/home/workspace"
    runtime.exec = AsyncMock(return_value=ExecResult("output", "", 0))
    runtime.upload_file = AsyncMock()
    runtime.upload_files = AsyncMock()
    runtime.download_file = AsyncMock(return_value=b"data")
    runtime.list_files = AsyncMock(return_value=[{"name": "file.txt", "is_dir": False}])
    runtime.code_run = AsyncMock(return_value=CodeRunResult("result", "", 0, []))
    runtime.get_state = AsyncMock(return_value=RuntimeState.RUNNING)
    runtime.start = AsyncMock()
    runtime.stop = AsyncMock()
    runtime.delete = AsyncMock()
    return runtime


@pytest.fixture
def mock_provider(mock_runtime):
    provider = fake_provider()
    provider.create = AsyncMock(return_value=mock_runtime)
    provider.get = AsyncMock(return_value=mock_runtime)
    provider.close = AsyncMock()
    provider.is_transient_error = MagicMock(return_value=False)
    return provider


class TestPTCSandboxDelegation:
    """Patch create_provider to return mock, verify PTCSandbox routes through runtime."""

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_execute_bash_routes_to_runtime_exec(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        await sandbox.execute_bash_command("ls -la")
        mock_runtime.exec.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_execute_bash_injects_mcp_trace_env_and_harvests(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        # A python script run via Bash must record the same MCP provenance
        # ExecuteCode does: the foreground command carries an MCP_TRACE_FILE env +
        # the wrapper PYTHONPATH, and the harvested trace is returned for the
        # provenance middleware. (Closes the bash provenance bypass.)
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        trace = [{"server": "marketdata", "tool": "quote", "result_sha256": "a" * 64}]
        with patch.object(
            sandbox, "_collect_mcp_trace", AsyncMock(return_value=trace)
        ) as collect:
            result = await sandbox.execute_bash_command("python analysis.py")

        exec_cmds = [c.args[0] for c in mock_runtime.exec.call_args_list if c.args]
        main_cmd = next(c for c in exec_cmds if "python analysis.py" in c)
        assert "export MCP_TRACE_FILE=" in main_cmd
        assert "export PYTHONPATH=" in main_cmd
        collect.assert_awaited_once()
        assert result["mcp_trace"] == trace

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_aupload_file_bytes_routes_to_runtime(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        await sandbox.aupload_file_bytes("/test/file.txt", b"content")
        mock_runtime.upload_file.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_adownload_file_bytes_routes_to_runtime(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        await sandbox.adownload_file_bytes("/test/file.txt")
        mock_runtime.download_file.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_als_directory_routes_to_runtime(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        await sandbox.als_directory("/home/workspace")
        mock_runtime.list_files.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_stop_sandbox_routes_to_runtime(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        await sandbox.stop_sandbox()
        mock_runtime.stop.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_cleanup_routes_to_runtime_and_provider(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime

        await sandbox.cleanup()
        mock_runtime.delete.assert_called()
        mock_provider.close.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_close_routes_to_provider(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())

        await sandbox.close()
        mock_provider.close.assert_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_archived_sandbox_is_restored_not_replaced(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        """Daytona reports recoverable=False on every healthy sandbox, archived included."""
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        mock_runtime.get_state.return_value = RuntimeState.ARCHIVED
        mock_runtime.get_metadata.return_value = {"recoverable": False}
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        sandbox = PTCSandbox(config=_make_config())

        await sandbox.reconnect("archived-sandbox")

        mock_runtime.start.assert_awaited_once()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_archiving_sandbox_waits_for_the_archive_then_restores(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        """A start mid-archive can land after the container is gone and strand the sandbox."""
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        states = iter(
            [RuntimeState.ARCHIVING, RuntimeState.ARCHIVING, RuntimeState.ARCHIVED]
        )
        reads = []

        async def get_state():
            state = next(states)
            reads.append(state)
            return state

        async def start(**kwargs):
            assert reads[-1] is RuntimeState.ARCHIVED

        mock_runtime.get_state = AsyncMock(side_effect=get_state)
        mock_runtime.start = AsyncMock(side_effect=start)
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        observed = MagicMock()
        sandbox = PTCSandbox(config=_make_config())

        with patch("asyncio.sleep", new=AsyncMock()):
            await sandbox.reconnect("archiving-sandbox", on_state_observed=observed)

        assert len(reads) == 3
        mock_runtime.start.assert_awaited_once()
        assert mock_runtime.start.await_args.kwargs["timeout"] == 300
        observed.assert_called_once_with("archived")

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_sandbox_still_archiving_is_transient_and_never_started(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        """Still archiving is a sandbox that exists, so a retry and never a replacement."""
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
        from ptc_agent.core.sandbox.runtime import SandboxTransientError

        mock_create_provider.return_value = mock_provider
        mock_runtime.get_state = AsyncMock(return_value=RuntimeState.ARCHIVING)
        sandbox = PTCSandbox(config=_make_config())
        clock = [0.0]

        async def sleep(seconds):
            clock[0] += seconds + 1  # each poll also pays a provider read

        with (
            patch("asyncio.sleep", new=AsyncMock(side_effect=sleep)),
            patch(
                "ptc_agent.core.sandbox.reconnect.time.monotonic",
                side_effect=lambda: clock[0],
            ),
        ):
            with pytest.raises(SandboxTransientError) as excinfo:
                await sandbox.reconnect("archiving-sandbox")

        assert not isinstance(excinfo.value, SandboxGoneError)
        assert "being archived" in str(excinfo.value)
        assert clock[0] <= 47  # the wait is bounded by time, not poll count
        mock_runtime.start.assert_not_awaited()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_archive_cancelled_mid_wait_starts_the_stopped_sandbox(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        """A cancelled archive ends stopped, which is safe to start."""
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        mock_runtime.get_state = AsyncMock(
            side_effect=[RuntimeState.ARCHIVING, RuntimeState.STOPPED]
        )
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        sandbox = PTCSandbox(config=_make_config())

        with patch("asyncio.sleep", new=AsyncMock()):
            await sandbox.reconnect("archiving-sandbox")

        mock_runtime.start.assert_awaited_once()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_archive_cancelled_into_running_is_used_as_is(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        """Something else started it mid-wait: usable, so no retry and no start."""
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        mock_runtime.get_state = AsyncMock(
            side_effect=[RuntimeState.ARCHIVING, RuntimeState.RUNNING]
        )
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        sandbox = PTCSandbox(config=_make_config())

        with patch("asyncio.sleep", new=AsyncMock()):
            await sandbox.reconnect("archiving-sandbox")

        mock_runtime.start.assert_not_awaited()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_errored_sandbox_the_provider_cannot_recover_is_gone(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        """Gone survives a transient-looking reason, and cleanup keeps the sandbox."""
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        mock_provider.is_transient_error = MagicMock(
            side_effect=lambda e: "timed out" in str(e)
        )
        mock_runtime.get_state.return_value = RuntimeState.ERROR
        mock_runtime.recover_from_error = AsyncMock(
            side_effect=SandboxGoneError("errored-sandbox", "container start timed out")
        )
        sandbox = PTCSandbox(config=_make_config())

        with pytest.raises(SandboxGoneError):
            await sandbox.reconnect("errored-sandbox")

        mock_runtime.recover_from_error.assert_awaited_once()
        mock_runtime.start.assert_not_awaited()
        assert sandbox.runtime is mock_runtime
        await sandbox.cleanup()
        mock_runtime.delete.assert_not_awaited()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_cleanup_deletes_once_a_reconnect_completes(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        sandbox = PTCSandbox(config=_make_config())

        await sandbox.reconnect("running-sandbox")
        await sandbox.cleanup()

        mock_runtime.delete.assert_awaited_once()


class TestBackgroundBashTrace:
    """The background-bash provenance bypass closure (BashOutput is the only
    result-bearing path for a backgrounded command, so the MCP trace must be
    injected at launch and harvested on the status read that sees completion)."""

    def _sandbox(self, mock_create_provider, mock_provider, mock_runtime):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime
        return sandbox

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_launch_injects_trace_env_named_after_the_session(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.fetch_working_dir = AsyncMock(return_value="/home/workspace")
        mock_runtime.list_sessions = AsyncMock(return_value=[])
        mock_runtime.create_session = AsyncMock()
        mock_runtime.session_execute = AsyncMock(return_value="cmd-xyz")

        result = await sandbox.execute_bash_command(
            "python long_job.py", background=True
        )

        # The executed bg command carries the MCP trace env (so a backgrounded
        # python script importing the wrappers records its calls) ...
        session_id = mock_runtime.create_session.call_args.args[0]
        bg_cmd = mock_runtime.session_execute.call_args.args[1]
        assert "export MCP_TRACE_FILE=" in bg_cmd
        assert "export PYTHONPATH=" in bg_cmd
        assert "python long_job.py" in bg_cmd
        # ... at a path any worker can derive from the command_id it returns.
        assert f"{session_id}.jsonl" in bg_cmd
        assert f"command_id: {session_id}" in result["stdout"]
        # A child shell, so the command's own `exit` cannot end the session's
        # shell before the exit code is recorded.
        assert bg_cmd.startswith("bash -c ")

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_status_harvests_trace_and_ends_session_on_completion(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.runtime import SessionCommandResult

        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.session_logs = AsyncMock(
            return_value=SessionCommandResult(
                cmd_id="c", exit_code=0, stdout="done", stderr=""
            )
        )
        mock_runtime.delete_session = AsyncMock()
        trace = [{"server": "marketdata", "tool": "quote", "result_sha256": "a" * 64}]
        with patch.object(
            sandbox, "_collect_mcp_trace", AsyncMock(return_value=trace)
        ) as collect:
            result = await sandbox.get_background_command_status("bg-0123456789ab")

        assert collect.await_args.args[0].endswith("/bg-0123456789ab.jsonl")
        assert result["mcp_trace"] == trace
        assert result["found"] and result["stdout"] == "done"
        mock_runtime.delete_session.assert_awaited_once_with("bg-0123456789ab")

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_status_while_running_does_not_harvest(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox.runtime import SessionCommandResult

        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.session_logs = AsyncMock(
            return_value=SessionCommandResult(
                cmd_id="c", exit_code=None, stdout="...", stderr=""
            )
        )
        mock_runtime.delete_session = AsyncMock()
        with patch.object(
            sandbox, "_collect_mcp_trace", AsyncMock(return_value=[])
        ) as collect:
            result = await sandbox.get_background_command_status("bg-0123456789ab")

        collect.assert_not_awaited()
        mock_runtime.delete_session.assert_not_awaited()
        assert result["is_running"] and result["mcp_trace"] == []

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_status_unknown_cmd_is_not_found(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.session_logs = AsyncMock(return_value=None)
        result = await sandbox.get_background_command_status("bg-0123456789ab")
        assert result["found"] is False and result["mcp_trace"] == []
        # An id that could not be a background session never reaches the sandbox.
        mock_runtime.session_logs.reset_mock()
        result = await sandbox.get_background_command_status("nope")
        assert result["found"] is False
        mock_runtime.session_logs.assert_not_called()

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_stop_drops_the_trace(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        from ptc_agent.core.sandbox import sessions

        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.delete_session = AsyncMock()
        mock_runtime.exec = AsyncMock()

        stopped = await sandbox.stop_background_command("bg-0123456789ab")
        assert stopped is True
        # A stopped command yields no output, so nothing to attest. The rm
        # follows the stop rather than holding it up.
        await asyncio.gather(*list(sessions._housekeeping))
        assert "bg-0123456789ab.jsonl" in mock_runtime.exec.call_args.args[0]


class TestEgressRelayCredentialPush:
    """The egress credential file is published atomically and fail-closed.

    The in-sandbox client reads it on every relay call, so a torn or missing
    write breaks the connector. The write stages to a temp and atomically
    renames; a nonzero sandbox exit is a failure the caller must see (it gates
    whether the JWT expiry is recorded), because ``exec`` returns a nonzero
    ``ExecResult`` rather than raising.
    """

    def _sandbox(self, mock_create_provider, mock_provider, mock_runtime):
        from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

        mock_create_provider.return_value = mock_provider
        sandbox = PTCSandbox(config=_make_config())
        sandbox.runtime = mock_runtime
        sandbox._work_dir = "/home/workspace"
        return sandbox

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_publish_stages_to_temp_then_atomically_replaces(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.exec = AsyncMock(return_value=ExecResult("", "", 0))

        ok = await sandbox.upload_egress_relay_credentials(
            {"relay_base_url": "https://relay.test", "token": "jwt", "grants": {}}
        )

        assert ok is True
        # Upload landed on a temp, never the live path.
        (_content, dest), _ = mock_runtime.upload_file.call_args
        assert dest.endswith(".tmp")
        assert dest.startswith("/home/workspace/_internal/.egress_relay.json.")
        # Publish is chmod-then-atomic-rename via os.replace (not `mv`), and the
        # temp is the one just uploaded.
        publish_cmd = mock_runtime.exec.call_args_list[0].args[0]
        assert "os.replace" in publish_cmd
        assert "chmod 600" in publish_cmd
        # The temp path (no shell-special chars → quoted verbatim) is the source.
        assert dest in publish_cmd

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_nonzero_exit_fails_closed_and_scavenges_temp(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        # The rename exec reports failure via exit code, not an exception.
        mock_runtime.exec = AsyncMock(return_value=ExecResult("", "boom", 1))

        ok = await sandbox.upload_egress_relay_credentials(
            {"relay_base_url": "https://relay.test", "token": "jwt", "grants": {}}
        )

        assert ok is False
        # A failed publish scavenges its orphaned temp (the last exec is the rm).
        assert any(
            "rm -f" in c.args[0] for c in mock_runtime.exec.call_args_list
        )

    @patch("ptc_agent.core.sandbox.ptc_sandbox.create_provider")
    @pytest.mark.asyncio
    async def test_removal_reports_confirmed_success(
        self, mock_create_provider, mock_provider, mock_runtime
    ):
        sandbox = self._sandbox(mock_create_provider, mock_provider, mock_runtime)
        mock_runtime.exec = AsyncMock(return_value=ExecResult("", "", 0))

        ok = await sandbox.upload_egress_relay_credentials(None)

        assert ok is True
        mock_runtime.upload_file.assert_not_called()
        assert "rm -f" in mock_runtime.exec.call_args_list[0].args[0]
