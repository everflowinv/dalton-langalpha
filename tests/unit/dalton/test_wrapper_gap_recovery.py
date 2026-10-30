"""The wrapper-gap recovery chain (48h review item 11, MSFT thread 2026-10-28).

A sandbox whose generated ``tools.<server>`` wrappers were deleted after a
completed sync still holds a manifest claiming everything is current, so the
next sync skips the install and every ``from tools.<server> import ...``
fails — and the agent then reads the outage as "no such data". Covered here:
the sync's existence probe on the branch that would otherwise skip, the one
forced re-install on a gap, the warning when the re-install still leaves it
missing, and the code run's result saying which kind of failure it was.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from ptc_agent.config.core import (
    CoreConfig,
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    MCPServerConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.sandbox import assets, jobs
from ptc_agent.core.sandbox import execution as execution_mod
from ptc_agent.core.sandbox.execution import _WRAPPER_IMPORT_HINT
from ptc_agent.core.sandbox.runtime import (
    ExecResult,
    SandboxRuntime,
    SandboxTransientError,
)
from tests.unit.core.sandbox.fake_provider import fake_provider
from tests.unit.ptc_agent.agent.job_fakes import outcome

DIR = "msft-a1b2"
WORKSPACE = "ws-msft"
TOOLS = {
    "researchdata": [
        SimpleNamespace(name="list_filings", input_schema={"type": "object"})
    ]
}


def _make_sandbox():
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

    config = CoreConfig(
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        security=SecurityConfig(),
        mcp=MCPConfig(servers=[MCPServerConfig(name="researchdata", source="builtin")]),
        logging=LoggingConfig(),
        filesystem=FilesystemConfig(),
    )
    with patch(
        "ptc_agent.core.sandbox.ptc_sandbox.create_provider",
        return_value=fake_provider(),
    ):
        sandbox = PTCSandbox(config=config)
    sandbox.mcp_registry = MagicMock()
    sandbox.mcp_registry.get_all_tools = MagicMock(return_value=TOOLS)
    return sandbox


async def _settled_sync(
    sandbox,
    *,
    probe: str | None | Exception = "",
    probe_call=None,
    runtime=None,
    keep_runtime_call: bool = False,
):
    """A warm sync whose manifest and claim already match the sandbox.

    ``probe`` is what the wrapper existence probe answers: the servers it
    reports missing, "" for none, or an exception the probe itself raises.
    ``probe_call`` replaces the ``_runtime_call`` double outright, for cases
    the answer alone cannot stage (a hang, a slow channel). ``runtime`` puts
    a caller-owned runtime in place (identity to assert against), and
    ``keep_runtime_call`` leaves the REAL ``_runtime_call`` wired to it, so
    the retry/reconnect plumbing itself is what the test exercises.
    Everything the settled fast path would otherwise do is stubbed.
    """
    manifest = await sandbox._compute_sandbox_manifest()
    ledger = {
        "schema_version": 1,
        "union_version": 3,
        "claims": {"researchdata": [WORKSPACE]},
        "servers": {"researchdata": {"enabled": True}},
        "tool_versions": {
            WORKSPACE: manifest["modules"]["tool_modules"]["version"]
        },
    }
    sandbox._wait_ready = AsyncMock()
    sandbox.ensure_sandbox_ready = AsyncMock()
    sandbox._prune_disabled_tool_modules = AsyncMock()
    sandbox._read_unified_manifest = AsyncMock(return_value=manifest)
    sandbox._install_tool_modules = AsyncMock()
    sandbox._start_internal_mcp_servers = AsyncMock()
    sandbox._write_unified_manifest = AsyncMock()
    sandbox._cleanup_legacy_manifests = AsyncMock()
    sandbox._upload_mcp_server_files_impl = AsyncMock()
    sandbox._upload_internal_packages = AsyncMock()
    sandbox.adownload_file_bytes = AsyncMock(
        return_value=json.dumps(ledger).encode("utf-8")
    )
    sandbox.runtime = (
        runtime if runtime is not None else create_autospec(SandboxRuntime, instance=True)
    )
    if keep_runtime_call:
        pass  # the real one, wired to the caller's runtime
    elif probe_call is not None:
        sandbox._runtime_call = probe_call
    elif isinstance(probe, Exception):
        sandbox._runtime_call = create_autospec(
            sandbox._runtime_call, side_effect=probe
        )
    else:
        sandbox._runtime_call = create_autospec(
            sandbox._runtime_call,
            return_value=ExecResult(stdout=probe, stderr="", exit_code=0),
        )
    with patch("ptc_agent.core.sandbox.assets.run_layout_migrations", AsyncMock()):
        return await sandbox.sync_sandbox_assets(
            reusing_sandbox=True,
            project=ProjectContext(WORKSPACE, DIR),
        )


def _probe_calls(mock) -> list:
    """Only the wrapper existence probes among ``_runtime_call``'s calls.

    The sync's delegator also mkdirs the workspace folders on the way out,
    which rides the same channel; the probes are the ``[ -f … ]`` batch.
    """
    return [
        c
        for c in mock.await_args_list
        if len(c.args) > 1 and isinstance(c.args[1], str) and "[ -f " in c.args[1]
    ]


class TestWrapperGapSync:
    @pytest.mark.asyncio
    async def test_a_missing_wrapper_forces_one_reinstall(self):
        sandbox = _make_sandbox()
        result = await _settled_sync(sandbox, probe="researchdata\n")

        assert "tool_modules" in result.refreshed_modules
        sandbox._install_tool_modules.assert_awaited_once()
        project = sandbox._install_tool_modules.await_args.kwargs["project"]
        assert (project.workspace_id, project.dir_name) == (WORKSPACE, DIR)
        # Exactly one probe before the install and one after it.
        assert len(_probe_calls(sandbox._runtime_call)) == 2

    @pytest.mark.asyncio
    async def test_a_wrapper_still_missing_after_the_resync_warns(self, monkeypatch):
        import structlog
        from structlog.testing import capture_logs

        sandbox = _make_sandbox()
        with capture_logs() as logs:
            # A module logger another test already used may be cached with the
            # old processors (cache_logger_on_first_use), which capture_logs
            # cannot see; a fresh one taken inside the capture always can.
            monkeypatch.setattr(assets, "logger", structlog.get_logger(assets.__name__))
            await _settled_sync(sandbox, probe="researchdata\n")

        record = next(
            l
            for l in logs
            if l.get("event")
            == "Tool wrapper modules still missing after a forced re-sync"
        )
        assert record["servers"] == ["researchdata"]

    @pytest.mark.asyncio
    async def test_present_wrappers_do_nothing_else(self):
        sandbox = _make_sandbox()
        result = await _settled_sync(sandbox, probe="")

        assert result.refreshed_modules == []
        sandbox._install_tool_modules.assert_not_awaited()
        # The settled fast path pays the one probe and nothing more.
        probes = _probe_calls(sandbox._runtime_call)
        assert len(probes) == 1
        assert ".agents/tools/researchdata.py" in probes[0].args[1]

    @pytest.mark.asyncio
    async def test_an_unanswerable_probe_reinstalls_nothing(self):
        """A probe the sandbox will not answer is not a missing module; a
        transient failure here must not turn every sync into a re-install."""
        sandbox = _make_sandbox()
        result = await _settled_sync(sandbox, probe=OSError("connection refused"))

        assert result.refreshed_modules == []
        sandbox._install_tool_modules.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_probe_that_outlives_its_budget_is_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The probe is an optional check: a stuck exec channel must not
        hold the acquisition hostage to the default 60s/120s retry budget.
        The wall is monkeypatched down to 50 ms — the probe task still
        starts (a cancelled await leaves no mock record, so the channel
        double records its own kwargs), the wall then cancels it, and every
        other call on the wire answers immediately. A revert to the default
        budget fails on the recorded kwargs, not on a hang."""
        monkeypatch.setattr(assets, "_WRAPPER_PROBE_BUDGET_S", 0.05)

        seen: list[dict] = []

        async def _stuck_channel_for_probes(*args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if "[ -f " in cmd:
                seen.append(dict(kwargs))
                await asyncio.sleep(30)
                raise AssertionError("the probe was left waiting on a stuck channel")
            return ExecResult(stdout="", stderr="", exit_code=0)

        sandbox = _make_sandbox()
        probe_call = create_autospec(
            sandbox._runtime_call, side_effect=_stuck_channel_for_probes
        )
        result = await _settled_sync(sandbox, probe="", probe_call=probe_call)

        # The acquisition completes and reinstalls nothing: timed out reads
        # as unverifiable, never as missing.
        assert result.refreshed_modules == []
        sandbox._install_tool_modules.assert_not_awaited()
        # Exactly one probe attempt, on the probe's own budget.
        assert len(seen) == 1
        assert seen[0]["timeout"] == assets._WRAPPER_PROBE_EXEC_TIMEOUT_S
        assert seen[0]["retries"] == 2
        assert seen[0]["total_timeout"] == 0.05

    @pytest.mark.asyncio
    async def test_a_failing_probe_never_reconnects_or_swaps_the_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Transient channel errors and the wall timeout both read as
        unverifiable, and the optional probe must leave the connection it
        rode exactly as it found it: no reconnect attempt, no runtime
        replacement. The real ``_runtime_call`` is what runs here, against a
        provider that classifies everything transient — the exact shape the
        reconnect switch exists for."""
        runtime = create_autospec(SandboxRuntime, instance=True)
        mode = {"probe": "transient"}

        async def _flaky_channel(*args, **kwargs):
            # This double hangs off runtime.exec, so the command is args[0]
            # (a double on _runtime_call would see it at args[1], behind the
            # exec callable).
            cmd = args[0] if args else ""
            if "[ -f " not in cmd:
                return ExecResult(stdout="", stderr="", exit_code=0)
            if mode["probe"] == "transient":
                raise SandboxTransientError("channel reset mid-probe")
            await asyncio.sleep(30)
            raise AssertionError("the probe was left waiting on a stuck channel")

        runtime.exec = AsyncMock(side_effect=_flaky_channel)
        sandbox = _make_sandbox()
        sandbox.provider.is_transient_error = MagicMock(return_value=True)
        reconnect_spy = AsyncMock()
        sandbox._ensure_sandbox_connected = reconnect_spy

        result = await _settled_sync(
            sandbox, probe="", runtime=runtime, keep_runtime_call=True
        )

        assert result.refreshed_modules == []
        sandbox._install_tool_modules.assert_not_awaited()
        reconnect_spy.assert_not_awaited()
        assert sandbox.runtime is runtime

        # The same invariants when the wall cancels a probe stuck mid-flight.
        monkeypatch.setattr(assets, "_WRAPPER_PROBE_BUDGET_S", 0.05)
        mode["probe"] = "stuck"
        result = await _settled_sync(
            sandbox, probe="", runtime=runtime, keep_runtime_call=True
        )

        assert result.refreshed_modules == []
        sandbox._install_tool_modules.assert_not_awaited()
        reconnect_spy.assert_not_awaited()
        assert sandbox.runtime is runtime


# What `cat <ws>/.agents/tools/mcp_client_config.json` answers in the code-run
# fixture: the JSON text, or an exception the read raises.
_INSTALL_RECORD: dict = {"value": ""}


@pytest.fixture
def sandbox_with_runtime():
    """A sandbox a code run can be started on.

    The same shape ``test_turn_cwd`` builds: a real PTCSandbox with a
    spec'd runtime whose calls pass through. Defined here rather than
    imported so the fixture's binding never shadows the parameter that
    requests it (F811).
    """
    box = _make_sandbox()
    runtime = create_autospec(SandboxRuntime, instance=True)
    _INSTALL_RECORD["value"] = json.dumps(
        {
            "schema_version": 1,
            "workspace_id": WORKSPACE,
            "dir_name": DIR,
            "servers": {"researchdata": {"transport": "stdio"}},
        }
    )

    async def _exec(command, *args, **kwargs):
        # Workspace A's own install record, read with `cat`: the config
        # written beside its wrappers names researchdata. The shared
        # registry may say otherwise — the hint must follow this file.
        if str(command).startswith("cat ") and str(command).rstrip("'").endswith(
            "mcp_client_config.json"
        ):
            record = _INSTALL_RECORD["value"]
            if isinstance(record, Exception):
                raise record
            return ExecResult(stdout=record, stderr="", exit_code=0)
        return ExecResult(stdout="", stderr="", exit_code=0)

    runtime.exec = AsyncMock(side_effect=_exec)
    runtime.upload_file = AsyncMock()
    box.runtime = runtime

    async def _passthrough(func, *args, **kwargs):
        for retry_kwarg in (
            "retry_policy",
            "allow_reconnect",
            "retries",
            "initial_delay_s",
            "total_timeout",
        ):
            kwargs.pop(retry_kwarg, None)
        return await func(*args, **kwargs)

    box._runtime_call = AsyncMock(side_effect=_passthrough)
    return box


def _tb(name: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "/w/code.py", line 1, in <module>\n'
        f"ModuleNotFoundError: No module named '{name}'\n"
    )


class TestWrapperImportHint:
    @pytest.mark.asyncio
    async def test_a_wrapper_import_failure_gets_the_channel_hint(
        self, sandbox_with_runtime
    ):
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("tools.researchdata")),
            raised="tools.researchdata",
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=True)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job(
                "from tools.researchdata import list_filings", wait_s=60
            )

        assert result.exit_code == 1
        # The traceback stays, and the reading it gets is named beside it.
        assert "No module named 'tools.researchdata'" in result.output
        assert _WRAPPER_IMPORT_HINT in result.output
        # A wrapper is a local module, so nothing was pip-installed for it.
        sandbox_with_runtime._install_package.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_hint_follows_the_workspaces_install_record_not_the_registry(
        self, sandbox_with_runtime
    ):
        """A sibling workspace's sync swaps the shared registry on the same
        computer; the workspace's own install record is what its wrappers
        were built from. Workspace A installed researchdata, the registry
        now holds only sibling B's websearch — the hint still fires."""
        sandbox_with_runtime.mcp_registry.get_all_tools = MagicMock(
            return_value={
                "websearch": [SimpleNamespace(name="search", input_schema={})]
            }
        )
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("tools.researchdata")),
            raised="tools.researchdata",
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=True)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job(
                "from tools.researchdata import list_filings", wait_s=60
            )

        assert _WRAPPER_IMPORT_HINT in result.output

    @pytest.mark.asyncio
    async def test_a_sibling_registry_name_this_workspace_never_had_gets_no_hint(
        self, sandbox_with_runtime
    ):
        """``websearch`` sits in the sibling-swapped registry but not in
        this workspace's install record. Reading the registry would
        mislabel a missing import the workspace never had; the record does
        not."""
        sandbox_with_runtime.mcp_registry.get_all_tools = MagicMock(
            return_value={
                "websearch": [SimpleNamespace(name="search", input_schema={})]
            }
        )
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("tools.websearch")),
            raised="tools.websearch",
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=True)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job(
                "from tools.websearch import search", wait_s=60
            )

        assert _WRAPPER_IMPORT_HINT not in result.output

    @pytest.mark.asyncio
    async def test_an_unreadable_install_record_gets_no_hint(
        self, sandbox_with_runtime
    ):
        """No record, no hint: a guess would relabel the agent's own typo
        as a channel outage, so silence is the honest answer."""
        _INSTALL_RECORD["value"] = OSError("config gone")
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("tools.researchdata")),
            raised="tools.researchdata",
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=True)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job(
                "from tools.researchdata import list_filings", wait_s=60
            )

        assert _WRAPPER_IMPORT_HINT not in result.output

    @pytest.mark.asyncio
    async def test_a_misspelled_server_name_gets_no_channel_hint(
        self, sandbox_with_runtime
    ):
        """``tools.research_data`` names no wrapper this workspace installed;
        the code is wrong, and a channel-outage note would send the agent
        hunting other sources instead of fixing the name."""
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("tools.research_data")),
            raised="tools.research_data",
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=True)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job(
                "from tools.research_data import list_filings", wait_s=60
            )

        assert result.exit_code == 1
        assert _WRAPPER_IMPORT_HINT not in result.output

    @pytest.mark.asyncio
    async def test_a_deeper_import_under_a_real_wrapper_gets_no_channel_hint(
        self, sandbox_with_runtime
    ):
        """``tools.researchdata.foo`` is not the wrapper module's own name —
        the import is reaching past a module that exists; the channel is
        fine and the hint would misread it."""
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("tools.researchdata.foo")),
            raised="tools.researchdata.foo",
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=True)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job(
                "from tools.researchdata.foo import bar", wait_s=60
            )

        assert result.exit_code == 1
        assert _WRAPPER_IMPORT_HINT not in result.output

    @pytest.mark.asyncio
    async def test_a_plain_missing_package_gets_no_channel_hint(
        self, sandbox_with_runtime
    ):
        failed = dataclasses.replace(
            outcome(exit_code=1, output=_tb("realpkg")), raised="realpkg"
        )
        sandbox_with_runtime._install_package = AsyncMock(return_value=False)
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job("import realpkg", wait_s=60)

        assert _WRAPPER_IMPORT_HINT not in result.output

    @pytest.mark.asyncio
    async def test_a_run_with_no_raised_record_gets_no_channel_hint(
        self, sandbox_with_runtime
    ):
        failed = outcome(exit_code=1, output=_tb("anything"))
        with patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
            result = await sandbox_with_runtime.start_code_job("import x", wait_s=60)

        assert _WRAPPER_IMPORT_HINT not in result.output



@pytest.mark.asyncio
async def test_a_failure_that_is_not_a_wrapper_import_reads_nothing(sandbox_with_runtime):
    """The optional record read only runs for a bare ``tools.<server>``
    failure; a deeper import returns at once, with no extra exec."""
    failed = dataclasses.replace(
        outcome(exit_code=1, output=_tb("tools.researchdata.foo")),
        raised="tools.researchdata.foo",
    )
    sandbox_with_runtime._install_package = AsyncMock(return_value=True)
    with patch.object(
        execution_mod, "_installed_wrapper_servers", AsyncMock(return_value=frozenset())
    ) as reader, patch.object(jobs, "run_job", new=AsyncMock(return_value=failed)):
        result = await sandbox_with_runtime.start_code_job(
            "import tools.researchdata.foo", wait_s=60
        )
    reader.assert_not_awaited()
    assert _WRAPPER_IMPORT_HINT not in result.output
