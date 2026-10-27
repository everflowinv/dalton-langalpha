"""Code and bash execution in the sandbox, with MCP trace harvest.

Functions take the owning ``PTCSandbox`` as their explicit first argument;
``PTCSandbox`` exposes same-name delegators, so call sites and patch
semantics are unchanged.
"""

import asyncio
import hashlib
import json
import shlex
import textwrap
import time
import uuid
from typing import Any

import structlog

from src.observability import (
    safe_record,
    sandbox_execute_duration_ms,
)
from src.observability.tracing import tracer as _otel_tracer

from ptc_agent.core.sandbox.livefs_runtime import protocol as livefs_protocol
from ptc_agent.core.sandbox import sessions as _sessions
from ptc_agent.core.sandbox.retry import RetryPolicy

from ..paths import SandboxLayout, WorkspaceLayout

from ptc_agent.core.sandbox._shared import (
    SANDBOX_TIMEOUT,
    TURN_CWD_ENV,
    ChartData,
    ExecutionResult,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

logger = structlog.get_logger(__name__)

# Relative names, each joined against the tier that owns it below.
_THREADS = WorkspaceLayout.THREADS_DIR
_SYSTEM_CODE = SandboxLayout.SYSTEM_CODE_DIR

#: The servers a subagent the user limited may call, which the connector
#: client checks on every call (``_require_enabled``; that file ships into the
#: sandbox and cannot import this). Exported only for such a subagent.
_ALLOWED_SERVERS_ENV = "PTC_ALLOWED_SERVERS"


def _failure_class(is_timeout: bool, error: BaseException) -> str:
    return SANDBOX_TIMEOUT if is_timeout else type(error).__name__


def _allowed_servers_value(servers: frozenset[str]) -> str:
    """Comma-joined and sorted, so one set always exports one string."""
    return ",".join(sorted(servers))


def _computer_path(sandbox: "PTCSandbox", relative: str) -> str:
    """Absolute path for a runtime directory the machine owns.

    ``.system`` belongs to the computer, not to the turn's folder, and
    ``normalize_path`` folds a relative name into the workspace, so it is
    joined against the root instead.
    """
    return sandbox.layout.join(relative)


def _workspace_path(sandbox: "PTCSandbox", relative: str) -> str:
    """Absolute path for a runtime directory the turn's workspace owns."""
    return sandbox.workspace().join(relative)


async def _collect_mcp_trace(sandbox: "PTCSandbox", trace_path: str) -> list[dict]:
    """Read + parse the per-execution MCP trace JSONL, then best-effort delete it.

        Returns one dict per valid JSON line; malformed lines are skipped so a
        partial write (e.g. after a crash) never breaks result assembly. Never
        raises — tracing is provenance-only and must not affect execution.
        """
    # Host-memory bound on the read. The generated client's per-execution
    # body budget caps what IT emits, but MCP_TRACE_FILE is visible to
    # agent-authored sandbox code, which can append to the JSONL directly —
    # so the budget is not a host-side safety bound. Size the file first and
    # skip a file far past any legit trace rather than pulling it (possibly
    # GBs) into memory. ~4x the body budget (RESULT_BODY_TRACE_BUDGET_BYTES)
    # leaves slack for snippets/args/metadata; the extractor clamps anyway.
    _MCP_TRACE_READ_MAX_BYTES = 16 * 1024 * 1024
    # A trace this small comes back in the size probe's own output; a larger
    # one is downloaded, since command output is not built for megabytes.
    _MCP_TRACE_INLINE_MAX_BYTES = 256 * 1024

    records: list[dict] = []
    content: str | None = None
    try:
        # Size and, for a small trace, contents in one round trip: the size
        # line comes first. The delete stays a separate call so a retried read
        # still finds the file.
        path = shlex.quote(trace_path)
        read_res = await sandbox._runtime_call(
            sandbox.runtime.exec,
            f'[ -s {path} ] || exit 0; n=$(wc -c < {path}); echo "$n"; '
            f'[ "$n" -le {_MCP_TRACE_INLINE_MAX_BYTES} ] && cat {path}',
            retry_policy=RetryPolicy.SAFE,
        )
        size_line, _, body = (getattr(read_res, "stdout", "") or "").partition("\n")
        trace_bytes = int(size_line.strip() or 0)
        if trace_bytes == 0:
            # No trace written — the common case for a bash command that
            # imported no MCP wrappers (git/npm/ls/...). The file was never
            # created, so skip the rm below (nothing to delete): one sandbox
            # round trip on every non-MCP run.
            return records
        if trace_bytes > _MCP_TRACE_READ_MAX_BYTES:
            logger.warning(
                "MCP trace file over read cap; skipping",
                path=trace_path,
                bytes=trace_bytes,
                cap=_MCP_TRACE_READ_MAX_BYTES,
            )
        elif trace_bytes <= _MCP_TRACE_INLINE_MAX_BYTES:
            content = body
        else:
            content = await sandbox.aread_file_text(trace_path)
    except Exception as e:
        logger.debug("Failed to read MCP trace file", path=trace_path, error=str(e))
        content = None
    if content:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(entry, dict):
                records.append(entry)
    try:
        # Delete via the same raw absolute trace_path the read above used.
        # trace_path is already rooted at the live work_dir; normalize_path
        # would re-prepend work_dir when it isn't under allowed_directories,
        # so the rm would miss and leak the file (read uses the raw path).
        await sandbox._runtime_call(
            sandbox.runtime.exec,
            f"rm -f {shlex.quote(trace_path)}",
            retry_policy=RetryPolicy.SAFE,
        )
    except Exception as e:
        logger.debug("Failed to clean up MCP trace file", path=trace_path, error=str(e))
    return records


async def execute(
    sandbox: "PTCSandbox",
    code: str,
    timeout: int | None = None,
    *,
    auto_install: bool = True,
    max_retries: int = 2,
    thread_id: str | None = None,
    call_id: str | None = None,
    allowed_servers: frozenset[str] | None = None,
    _carry_mcp_trace: list[dict] | None = None,
) -> ExecutionResult:
    """Execute Python code in the sandbox with optional auto-install for missing dependencies.

        Args:
            code: Python code to execute
            timeout: Optional timeout in seconds
            auto_install: Whether to automatically install missing packages on ImportError (default: True)
            max_retries: Maximum number of retries after auto-installing packages (default: 2)
            thread_id: Optional thread ID (first 8 chars) for thread-scoped code storage
            call_id: The tool call's id for the file mount, which files what
                this code saves through it under the call
            allowed_servers: The only MCP servers this code may call, for a
                subagent the user limited; None leaves the workspace's set
            _carry_mcp_trace: MCP trace accumulated from prior auto-install
                attempts, prepended to this attempt's trace so provenance from
                a failed-then-retried run isn't lost (internal).

        Returns:
            ExecutionResult with execution details
        """
    await sandbox._wait_ready()

    sandbox.execution_count += 1
    execution_id = f"exec_{sandbox.execution_count:04d}"
    code_hash = hashlib.sha256(code.encode()).hexdigest()[:16]

    logger.debug(
        "Executing code",
        execution_id=execution_id,
        code_hash=code_hash,
        code_length=len(code),
        auto_install=auto_install,
        thread_id=thread_id,
    )

    timeout_val = timeout or sandbox.config.security.max_execution_time
    start_time = time.time()

    _exec_span = _otel_tracer.start_span(
        "sandbox.execute",
        attributes={"code_bytes": len(code), "execution_id": execution_id},
    )
    # finally — guarantees end() runs on asyncio.CancelledError too
    # (it's a BaseException so the except-clauses below would skip it).
    # Set before the try so the crash path can still read any trace lines
    # flushed before the failure (durability).
    trace_path: str | None = None
    # True once _collect_mcp_trace has run (it reads AND deletes the file).
    # The finally block only cleans up when this stayed False — i.e. an
    # asyncio.CancelledError unwound the turn before either the success or
    # crash path collected, which would otherwise orphan the JSONL.
    trace_collected = False
    carry_trace = list(_carry_mcp_trace or [])
    save_code: asyncio.Task[None] | None = None
    try:
        # Write code to thread dir or fallback to code/
        if thread_id:
            code_path = f"{_THREADS}/{thread_id}/code/{execution_id}.py"
            code_abs = _workspace_path(sandbox, code_path)
            # Ensure per-thread code dir exists (lazy, once per thread)
            if thread_id not in sandbox._thread_dirs_created:
                await sandbox._runtime_call(
                    sandbox.runtime.exec,
                    f"mkdir -p {shlex.quote(_workspace_path(sandbox, f'{_THREADS}/{thread_id}/code'))}",
                    retry_policy=RetryPolicy.SAFE,
                )
                sandbox._thread_dirs_created.add(thread_id)
        else:
            code_path = f"{_SYSTEM_CODE}/{execution_id}.py"
            code_abs = _computer_path(sandbox, code_path)

        async def _save_code() -> None:
            try:
                await sandbox._runtime_call(
                    sandbox.runtime.upload_file,
                    code.encode("utf-8"),
                    code_abs,
                    retry_policy=RetryPolicy.SAFE,
                )
            except Exception as upload_err:
                logger.warning(
                    "Failed to save code file to sandbox (non-fatal)",
                    code_path=code_path,
                    error=str(upload_err),
                )

        # The saved copy goes up beside the run, not ahead of it: nothing reads
        # it before the result returns, and a round trip is most of a short run.
        save_code = asyncio.create_task(_save_code())

        # Execute code
        # Set PYTHONPATH so code can import the tool wrappers and the runtime.
        # MCP + GitHub env vars are injected at sandbox creation time.
        # The cached root, like the bash path below: it is set on create and
        # reconnect, and re-asking the provider costs a round trip per run.
        layout = sandbox.layout
        workspace = sandbox.workspace()
        exec_env = {
            "PYTHONPATH": ":".join(workspace.pythonpath(layout)),
            # Read by the shipped sitecustomize.py at interpreter startup.
            TURN_CWD_ENV: workspace.workspace,
            "PTC_WORKSPACE_CONFIG": workspace.mcp_client_config,
        }
        if allowed_servers is not None:
            exec_env[_ALLOWED_SERVERS_ENV] = _allowed_servers_value(allowed_servers)

        # Per-execution MCP provenance trace file. A unique id (uuid suffix)
        # keeps the file unique across concurrent executions on a shared
        # sandbox (parallel subagents). The generated mcp_client creates the
        # parent dir lazily; we read + delete it after the run.
        trace_path = (
            f"{layout.system_trace}/{execution_id}_{uuid.uuid4().hex}.jsonl"
        )
        exec_env["MCP_TRACE_FILE"] = trace_path
        if call_id:
            exec_env[livefs_protocol.CALL_ENV] = call_id

        # Use code_run() for native artifact support (captures matplotlib charts)
        result = await sandbox._runtime_call(
            sandbox.runtime.code_run,
            code,
            env=exec_env,
            timeout=timeout_val,
            retry_policy=RetryPolicy.UNSAFE,
            total_timeout=timeout_val + 30,
        )
        await save_code

        stdout = result.stdout
        stderr = result.stderr
        exit_code = result.exit_code
        success = exit_code == 0

        # Extract charts from artifacts
        charts = []
        for artifact in result.artifacts:
            charts.append(
                ChartData(
                    type=artifact.type,
                    title=artifact.name or "",
                    png_base64=artifact.data if artifact.data else None,
                    elements=[],
                )
            )

        duration = time.time() - start_time

        # Collect MCP provenance trace written in-sandbox (best-effort),
        # prepending any trace carried over from prior retry attempts so a
        # failed-then-retried run keeps the pre-failure sources (dedup is
        # handled downstream by the extractor).
        mcp_trace = [*carry_trace, *await sandbox._collect_mcp_trace(trace_path)]
        trace_collected = True

        execution_result = ExecutionResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            execution_id=execution_id,
            code_hash=code_hash,
            charts=charts,
            mcp_trace=mcp_trace,
        )

        # Auto-install missing packages and retry if enabled
        if not success and auto_install and max_retries > 0:
            missing_packages = sandbox._detect_missing_imports(stderr)
            # DALTON-HOOK(sandbox-pip-system): skip names matching local
            # workspace directories or modules before calling uv pip install.
            if missing_packages:
                from ptc_agent.core.sandbox import mcp_setup as _mcp_setup

                missing_packages = await _mcp_setup._filter_local_packages(
                    sandbox, missing_packages
                )
            if missing_packages:
                logger.info(
                    "Attempting auto-install and retry",
                    execution_id=execution_id,
                    missing_packages=missing_packages,
                    retries_remaining=max_retries,
                )

                # A retry re-runs everything before the failing import, so it
                # is only worth its side effects once an install succeeded.
                installed = [
                    await sandbox._install_package(package)
                    for package in missing_packages
                ]
                if any(installed):
                    # Retry with a decremented retry count, carrying this
                    # attempt's trace forward so its provenance survives.
                    return await sandbox.execute(
                        code=code,
                        timeout=timeout,
                        auto_install=auto_install,
                        max_retries=max_retries - 1,
                        thread_id=thread_id,
                        call_id=call_id,
                        allowed_servers=allowed_servers,
                        _carry_mcp_trace=mcp_trace,
                    )

        logger.info(
            "Code execution completed",
            execution_id=execution_id,
            success=success,
            duration=duration,
            charts_captured=len(charts),
        )

        _exec_span.set_attribute("success", True)
        safe_record(
            sandbox_execute_duration_ms,
            (time.time() - start_time) * 1000.0,
            {"success": "true", "kind": "code"},
        )

        return execution_result

    except Exception as e:
        if save_code is not None:
            # Best effort and never raises; awaited so it is not left detached.
            await save_code
        duration = time.time() - start_time
        is_timeout, error_detail, stderr_msg = sandbox._classify_execution_error(
            e,
            duration,
            timeout_val,
            f"Execution timed out after {duration:.0f}s (limit: {timeout_val}s). "
                "The script was killed before completion — no output was captured. "
                "Split into smaller steps or optimize the script to run faster.",
        )

        logger.error(
            "Code execution failed",
            execution_id=execution_id,
            error=error_detail,
            duration=duration,
            is_timeout=is_timeout,
        )

        _exec_span.record_exception(e)
        _exec_span.set_attribute("success", False)
        _exec_span.set_attribute("is_timeout", is_timeout)
        safe_record(
            sandbox_execute_duration_ms,
            duration * 1000.0,
            {"success": "false", "kind": "code"},
        )

        # Recover any MCP trace lines flushed before the crash (durability),
        # plus any trace carried over from prior retry attempts.
        crash_mcp_trace: list[dict] = list(carry_trace)
        if trace_path:
            crash_mcp_trace.extend(await sandbox._collect_mcp_trace(trace_path))
            trace_collected = True

        return ExecutionResult(
            success=False,
            stdout="",
            stderr=stderr_msg,
            duration=duration,
            execution_id=execution_id,
            code_hash=code_hash,
            charts=[],
            mcp_trace=crash_mcp_trace,
            failure=_failure_class(is_timeout, e),
        )
    finally:
        _exec_span.end()
        # asyncio.CancelledError (BaseException) skips the except above, so a
        # disconnect/cancel after MCP_TRACE_FILE was set would leave the JSONL
        # behind. Delete it here when neither path collected. shield() so the
        # rm still runs even though this await is itself unwinding a cancel.
        if (
            trace_path is not None
            and not trace_collected
            and sandbox.runtime is not None
        ):
            try:
                await asyncio.shield(
                    sandbox._runtime_call(
                        sandbox.runtime.exec,
                        f"rm -f {shlex.quote(trace_path)}",
                        retry_policy=RetryPolicy.SAFE,
                    )
                )
            except asyncio.CancelledError:
                # Propagate the cancel we're unwinding; the shielded rm above
                # has already been dispatched and runs to completion detached.
                raise
            except Exception as e:
                logger.debug(
                    "Failed to clean up MCP trace file on cancel",
                    path=trace_path,
                    error=str(e),
                )


async def execute_bash_command(
    sandbox: "PTCSandbox",
    command: str,
    working_dir: str | None = None,
    timeout: int = 60,
    *,
    background: bool = False,
    thread_id: str | None = None,
    call_id: str | None = None,
    allowed_servers: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Execute a bash command in the sandbox.

        Args:
            command: Bash command to execute
            working_dir: Working directory for command execution (default: the
                turn's workspace folder, or the computer root without one)
            timeout: Maximum execution time in seconds (default: 60)
            background: Run command in background
            thread_id: Optional thread ID (first 8 chars) for thread-scoped script storage
            call_id: The tool call's id for the file mount
            allowed_servers: As for ``execute``

        Returns:
            Dictionary with success, stdout, stderr, exit_code, bash_id, command_hash
        """
    # The runtime's own cwd is the computer root, so a relative or virtual
    # working_dir has to be folded into the workspace here or it lands beside
    # the sibling folders.
    working_dir = sandbox.normalize_path(working_dir or ".")
    await sandbox._wait_ready()
    start_time = time.time()

    # Per-execution MCP provenance trace file (foreground only; set just
    # before exec). Initialized here so the finally can clean it up on a
    # cancel that skips the except below.
    trace_path: str | None = None
    trace_collected = False

    try:
        # Generate bash execution ID for tracking
        sandbox.bash_execution_count += 1
        bash_id = f"bash_{sandbox.bash_execution_count:04d}"
        command_hash = hashlib.sha256(command.encode()).hexdigest()[:16]
        from datetime import UTC, datetime

        timestamp = datetime.now(tz=UTC).isoformat()

        logger.debug(
            "Executing bash command",
            bash_id=bash_id,
            command_hash=command_hash,
            command_length=len(command),
            working_dir=working_dir,
        )

        # Build the full bash command with working directory
        # Use cd to change directory, then execute command
        full_command = f"cd {shlex.quote(working_dir)} && {command}"

        # Audit: save .sh script for traceability (non-fatal)
        script_content = textwrap.dedent(f"""\
                #!/bin/bash
                # Bash Execution Log
                # ID: {bash_id}
                # Working Directory: {working_dir}
                # Timestamp: {timestamp}
                # Command Hash: {command_hash}

                set -e
                {full_command}
            """)

        if thread_id:
            script_relative_path = f"{_THREADS}/{thread_id}/code/{bash_id}.sh"
            script_abs = _workspace_path(sandbox, script_relative_path)
            if thread_id not in sandbox._thread_dirs_created:
                await sandbox._runtime_call(
                    sandbox.runtime.exec,
                    f"mkdir -p {shlex.quote(_workspace_path(sandbox, f'{_THREADS}/{thread_id}/code'))}",
                    retry_policy=RetryPolicy.SAFE,
                )
                sandbox._thread_dirs_created.add(thread_id)
        else:
            script_relative_path = f"{_SYSTEM_CODE}/{bash_id}.sh"
            script_abs = _computer_path(sandbox, script_relative_path)

        try:
            assert sandbox.runtime is not None
            await sandbox._runtime_call(
                sandbox.runtime.upload_file,
                script_content.encode("utf-8"),
                script_abs,
                retry_policy=RetryPolicy.SAFE,
            )
        except Exception as upload_err:
            logger.warning(
                "Failed to save bash script to sandbox (non-fatal)",
                bash_id=bash_id,
                error=str(upload_err),
            )

        # Background execution via a dedicated session per command
        if background:
            session_id = await sandbox._create_bg_session()
            assert sandbox.runtime is not None
            # Inject the same MCP provenance trace env the foreground path
            # uses so a backgrounded `python script.py` that imports the MCP
            # wrappers records its mcp_trace too, harvested on completion in
            # get_background_command_status. The audit .sh stays clean; only
            # the executed command carries the exports.
            _, bg_command = sandbox._build_trace_env_command(
                bash_id,
                full_command,
                call_id=call_id,
                trace_path=_sessions.bg_trace_path(sandbox, session_id),
                own_shell=True,
                allowed_servers=allowed_servers,
            )
            try:
                await sandbox._runtime_call(
                    sandbox.runtime.session_execute,
                    session_id,
                    bg_command,
                    retry_policy=RetryPolicy.UNSAFE,
                    total_timeout=30,
                )
            except Exception:
                # Clean up the session so it doesn't leak on the sandbox
                try:
                    await sandbox._runtime_call(
                        sandbox.runtime.delete_session,
                        session_id,
                        retry_policy=RetryPolicy.SAFE,
                    )
                except Exception:
                    logger.debug("Failed to clean up bg session after execute failure", session_id=session_id)
                raise
            logger.debug(
                "Background command started",
                bash_id=bash_id,
                session_id=session_id,
            )
            return {
                "success": True,
                "stdout": (
                    f"Background command started (command_id: {session_id})\n"
                        f"Use BashOutput tool with command_id=\"{session_id}\" to check output and status."
                ),
                "stderr": "",
                "exit_code": 0,
                "bash_id": bash_id,
                "command_hash": command_hash,
            }

        # Execute directly via process.exec — no file upload dependency.
        # Inject a per-execution MCP provenance trace file + the wrapper
        # import path so a python script run here (e.g. `python analysis.py`
        # importing `tools.{server}`) records the same mcp_trace ExecuteCode
        # does — closing the bash provenance bypass. PYTHONPATH is prepended
        # (preserving any existing value). The audit .sh above stays clean;
        # only the executed command carries the exports.
        assert sandbox.runtime is not None
        trace_path, exec_command = sandbox._build_trace_env_command(
            bash_id, full_command, call_id=call_id, allowed_servers=allowed_servers
        )
        exec_result = await sandbox._runtime_call(
            sandbox.runtime.exec,
            exec_command,
            timeout=timeout,
            retry_policy=RetryPolicy.UNSAFE,
            total_timeout=timeout + 30,
        )

        exit_code = exec_result.exit_code
        stdout = exec_result.stdout
        safe_record(
            sandbox_execute_duration_ms,
            (time.time() - start_time) * 1000.0,
            {"success": "true" if exit_code == 0 else "false", "kind": "bash"},
        )

        # Harvest the in-sandbox MCP trace (best-effort; reads + deletes the
        # file). No MCP call → file absent → empty list, no behavior change.
        mcp_trace = await sandbox._collect_mcp_trace(trace_path)
        trace_collected = True

        if exit_code == 0:
            return {
                "success": True,
                "stdout": stdout,
                "stderr": "",
                "exit_code": 0,
                "bash_id": bash_id,
                "command_hash": command_hash,
                "mcp_trace": mcp_trace,
            }

        return {
            "success": False,
            "stdout": stdout,
            "stderr": "",  # runtime.exec() returns combined output in stdout only
            "exit_code": exit_code,
            "bash_id": bash_id,
            "command_hash": command_hash,
            "mcp_trace": mcp_trace,
        }

    except Exception as e:
        duration = time.time() - start_time
        safe_record(
            sandbox_execute_duration_ms,
            duration * 1000.0,
            {"success": "false", "kind": "bash"},
        )
        is_timeout, error_detail, stderr_msg = sandbox._classify_execution_error(
            e,
            duration,
            timeout,
            f"Command timed out after {duration:.0f}s (limit: {timeout}s). "
                "The command was killed before completion. "
                "Split into smaller steps or increase the timeout.",
        )

        logger.error(
            f"Failed to execute bash command: {e}",
            exc_info=True,
            extra={"is_timeout": is_timeout},
        )
        # Recover any MCP trace flushed before the failure (best-effort).
        recovered_trace: list[dict] = []
        if trace_path is not None:
            recovered_trace = await sandbox._collect_mcp_trace(trace_path)
            trace_collected = True
        return {
            "success": False,
            "stdout": "",
            "stderr": stderr_msg,
            "exit_code": -1,
            "bash_id": locals().get("bash_id"),
            "command_hash": None,
            "mcp_trace": recovered_trace,
            "failure": _failure_class(is_timeout, e),
        }
    finally:
        # asyncio.CancelledError (BaseException) skips the except above, so a
        # cancel after MCP_TRACE_FILE was set would leave the JSONL behind.
        # Delete it here when neither path collected. shield() so the rm still
        # runs even though this await is itself unwinding a cancel.
        if (
            trace_path is not None
            and not trace_collected
            and sandbox.runtime is not None
        ):
            try:
                await asyncio.shield(
                    sandbox._runtime_call(
                        sandbox.runtime.exec,
                        f"rm -f {shlex.quote(trace_path)}",
                        retry_policy=RetryPolicy.SAFE,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    "Failed to clean up bash MCP trace file on cancel",
                    path=trace_path,
                    error=str(e),
                )


def _build_trace_env_command(
    sandbox: "PTCSandbox",
    bash_id: str,
    full_command: str,
    call_id: str | None = None,
    trace_path: str | None = None,
    own_shell: bool = False,
    allowed_servers: frozenset[str] | None = None,
) -> tuple[str, str]:
    """Wrap a bash command with the MCP-provenance trace env.

        Returns ``(trace_path, command)``. ``command`` exports ``MCP_TRACE_FILE``
        plus the wrapper ``PYTHONPATH`` (prepended, preserving any existing value)
        before running ``full_command``, so a ``python script.py`` that imports the
        MCP wrappers records the same ``mcp_trace`` ExecuteCode does. Shared by the
        foreground and background bash paths so the two can't drift in how they
        build PYTHONPATH or quote the trace path.

        ``call_id`` tags the shell for the file mount, in the environment it
        starts with rather than an export inside it: the mount reads a
        process's starting environment, and the shell's own redirections are
        requests of the shell's pid. ``own_shell`` runs the command in a child
        shell even without one, for a session whose long-lived shell a
        command's ``exit`` would end before its exit code is recorded.
        """
    # Use the cached working dir (set on create/reconnect via
    # fetch_working_dir, and used by normalize_path on this same bash path) so
    # wrapping a command doesn't add a Daytona round-trip per bash invocation.
    layout = sandbox.layout
    pythonpath = ":".join(sandbox.workspace().pythonpath(layout))
    trace_path = trace_path or f"{layout.system_trace}/{bash_id}_{uuid.uuid4().hex}.jsonl"
    limit = (
        f"export {_ALLOWED_SERVERS_ENV}={shlex.quote(_allowed_servers_value(allowed_servers))} && "
        if allowed_servers is not None
        else ""
    )
    command = (
        f"export MCP_TRACE_FILE={shlex.quote(trace_path)} && "
        f"export PTC_WORKSPACE_CONFIG={shlex.quote(sandbox.workspace().mcp_client_config)} && "
            f"{limit}"
            f"export PYTHONPATH={shlex.quote(pythonpath)}"
            f"${{PYTHONPATH:+:$PYTHONPATH}} && "
            f"{full_command}"
    )
    if call_id:
        command = (
            f"env {livefs_protocol.CALL_ENV}={shlex.quote(call_id)} "
            f"bash -c {shlex.quote(command)}"
        )
    elif own_shell:
        command = f"bash -c {shlex.quote(command)}"
    return trace_path, command


