"""Container inner wrapper process for OC-1 deterministic data tasks (§2.1.2, §2.3.1, §4.4.3, §5.1.3).

Responsibilities:
- Load injected Vault secrets from ``/run/dalton-secrets`` (plus Dalton managed tokens) into an
  in-memory redaction table;
- Export ``DALTON_NO_DIRECT_SEND=1``, ``DALTON_NOTIFY_DRY_RUN=1|0``, ``DALTON_COS_UPLOAD_MODE``,
  ``DALTON_OUTBOX_FILE`` (pointing to container-internal ``tmpfs`` at ``/run/dalton-outbox/outbox.json``),
  ``HOME=/Users/everflow``, ``TZ=America/New_York``, and ``PATH`` while stripping ``/app`` from
  ``PYTHONPATH`` for the business child process;
- Stream and redact child stdout/stderr BEFORE writing to the wrapper's own stdout/stderr;
- Enforce total timeout (default 1800s when unset) and no-output timeout (when configured),
  exiting with distinct exit codes:
  * ``EXIT_CODE_TOTAL_TIMEOUT = 124`` when total wall-clock timeout is exceeded;
  * ``EXIT_CODE_NO_OUTPUT_TIMEOUT = 125`` when no-output timeout is exceeded;
- Read the raw outbox file from ``tmpfs`` (or stdout payload per §5.1.3), redact in memory,
  and only then atomically write ``.dalton-runs/<run_id>/outbox.json`` on the persistent data volume.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
from typing import Any, TextIO

from src.dalton.data_jobs.manifest import CONTAINER_PYTHON_BIN, DEFAULT_TIMEOUT_SECONDS
from src.dalton.data_jobs.safe_fs import (
    SafeFsError,
    _open_verified_dir_chain,
    safe_atomic_write_bytes,
    safe_unlink_contained_regular_file,
)
from src.dalton.settings import managed_secret_token_values, redact_managed_tokens

DEFAULT_WORKSPACE_DIR = "/Users/everflow/.openclaw/workspace"
DEFAULT_SECRETS_DIR = "/run/dalton-secrets"
DEFAULT_TMPFS_OUTBOX_DIR = "/run/dalton-outbox"
CHILD_SITE_DIR = Path(__file__).resolve().parent / "child_site"
DEFAULT_TMPFS_OUTBOX_FILE = f"{DEFAULT_TMPFS_OUTBOX_DIR}/outbox.json"
MIN_SECRET_LEN = 4
REDACTED_PLACEHOLDER = "[REDACTED]"

# Distinct wrapper exit codes for timeout classification (§2.3.1, §3.4):
# - 124: total execution timeout exceeded (timeout_seconds)
# - 125: no-output stall timeout exceeded (no_output_timeout_seconds)
EXIT_CODE_TOTAL_TIMEOUT = 124
EXIT_CODE_NO_OUTPUT_TIMEOUT = 125
TIMEOUT_EXIT_CODE = EXIT_CODE_TOTAL_TIMEOUT

# Notification payload source per task (§5.1.3):
# - "outbox_file": child script writes structured card/items JSON to $DALTON_OUTBOX_FILE (tmpfs)
# - "stdout_json": child script prints structured JSON object to stdout
# - "stdout_markdown": child script prints Markdown report to stdout (T27)
# - "stdout_text": child script prints plain-text report to stdout (T32)
# - "none": silent data refresh task (notify_enabled=False)
TASK_NOTIFY_PAYLOAD_SOURCES: dict[str, str] = {
    "bbg-data-pull-asia": "none",  # T1
    "cn-leverage-daily": "none",  # T5
    "jp-leverage-daily": "none",  # T7
    "kr-leverage-daily": "none",  # T8
    "index-crowding-html-daily": "none",  # T10
    "sentiment-dashboard-digest": "outbox_file",  # T13
    "megacap-relative-signal-monitor": "stdout_json",  # T18
    "us-bottom-monitor": "stdout_json",  # T21
    "us-top-monitor": "stdout_json",  # T22
    "sentiment-dashboard-refresh": "none",  # T25
    "us-topbottom-dm-v23": "outbox_file",  # T26
    "asia-leverage-crossview-daily": "stdout_markdown",  # T27
    "tw-leverage-daily": "none",  # T28
    "bbg-data-pull-us": "none",  # T29
    "quantum-ew-monitor": "stdout_text",  # T32
    "ah-premium-am": "stdout_json",  # T40
    "ah-premium-pm": "stdout_json",  # T44
    "softbank-nav-monitor": "outbox_file",  # T52
    "tsmc-adr-premium": "outbox_file",  # T54
}

_TASK_ID_PAYLOAD_SOURCE_ALIASES: dict[str, str] = {
    "T27": "stdout_markdown",
    "T32": "stdout_text",
    "T18": "stdout_json",
    "T21": "stdout_json",
    "T22": "stdout_json",
    "T40": "stdout_json",
    "T44": "stdout_json",
    "T13": "outbox_file",
    "T26": "outbox_file",
    "T52": "outbox_file",
    "T54": "outbox_file",
}


def resolve_task_payload_source(job_key: str) -> str:
    """Return the notification payload source configured for ``job_key`` per §5.1.3."""
    key = str(job_key or "").strip()
    if key in TASK_NOTIFY_PAYLOAD_SOURCES:
        return TASK_NOTIFY_PAYLOAD_SOURCES[key]
    if key in _TASK_ID_PAYLOAD_SOURCE_ALIASES:
        return _TASK_ID_PAYLOAD_SOURCE_ALIASES[key]
    return "outbox_file"


def _collect_secret_strings(value: Any, out: set[str]) -> None:
    """Recursively collect non-empty string values of length >= 4 (including JSON secret fields)."""
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) >= MIN_SECRET_LEN:
            out.add(stripped)
        if len(value) >= MIN_SECRET_LEN:
            out.add(value)
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except Exception:
                return
            _collect_secret_strings(parsed, out)
    elif isinstance(value, Mapping):
        for v in value.values():
            _collect_secret_strings(v, out)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _collect_secret_strings(item, out)


class SecretRedactor:
    """In-memory redactor for Vault secrets and Dalton management tokens (§4.4.3)."""

    def __init__(self, secret_values: Sequence[str] = ()) -> None:
        collected: set[str] = set()
        for raw in secret_values:
            _collect_secret_strings(raw, collected)
        for token in managed_secret_token_values():
            if token:
                collected.add(token)
        # Longest first so partial overlaps never leave secret suffixes exposed.
        self._patterns: tuple[str, ...] = tuple(
            sorted((s for s in collected if s), key=len, reverse=True)
        )

    @property
    def patterns(self) -> tuple[str, ...]:
        return self._patterns

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        out = text
        for pattern in self._patterns:
            if pattern in out:
                out = out.replace(pattern, REDACTED_PLACEHOLDER)
        out = str(redact_managed_tokens(out))
        return out

    def redact_value(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, Mapping):
            return {
                (self.redact_text(k) if isinstance(k, str) else k): self.redact_value(v)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self.redact_value(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact_value(item) for item in obj)
        return obj


def load_secrets_from_dir(
    secrets_dir: Path | str | None = DEFAULT_SECRETS_DIR,
) -> tuple[dict[str, str], SecretRedactor]:
    """Read secret files from ``/run/dalton-secrets`` and build a ``SecretRedactor``."""
    env_secrets: dict[str, str] = {}
    secret_values: list[str] = []
    if secrets_dir is not None:
        base = Path(secrets_dir)
        if base.is_dir():
            for entry in sorted(base.iterdir()):
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                try:
                    raw = entry.read_text(encoding="utf-8")
                except Exception:
                    continue
                val = raw.rstrip("\r\n")
                env_secrets[entry.name] = val
                secret_values.append(raw)
                secret_values.append(val)
    return env_secrets, SecretRedactor(secret_values)


def atomic_write_text(
    target_path: Path,
    content: str,
    *,
    anchor_dir: Path | None = None,
) -> None:
    """Atomically write ``target_path`` via verified ``dir_fd``s + unpredictable temp file + ``os.replace`` (§4.2)."""
    safe_atomic_write_bytes(
        target_path.parent,
        target_path.name,
        content.encode("utf-8"),
        anchor_dir=anchor_dir,
        reject_base_symlink=True,
        mode=0o600,
    )


def atomic_write_json(
    target_path: Path,
    payload: Any,
    *,
    anchor_dir: Path | None = None,
) -> None:
    """Atomically serialize ``payload`` as UTF-8 JSON to ``target_path`` (§4.2)."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write_text(target_path, serialized, anchor_dir=anchor_dir)


def build_child_env(
    *,
    base_env: Mapping[str, str] | None = None,
    workspace_dir: str = DEFAULT_WORKSPACE_DIR,
    run_id: str,
    job_key: str = "",
    notify_dry_run: bool = True,
    cos_mode: str = "skip",
    secret_env: Mapping[str, str] | None = None,
    outbox_file: Path | str | None = None,
    outbox_tmp_dir: Path | str = DEFAULT_TMPFS_OUTBOX_DIR,
) -> dict[str, str]:
    """Construct the child subprocess environment per §2.1.2, §2.3.1, §4.3.2, and §5.1.3.

    ``DALTON_OUTBOX_FILE`` passed to the child subprocess always points to the container-internal
    ``tmpfs`` directory (default ``/run/dalton-outbox/outbox.json``) rather than the persistent data
    volume so unredacted outbox content never lands on disk before wrapper redaction.
    """
    env = dict(base_env if base_env is not None else os.environ)

    # §2.1.2: wrapper runs with PYTHONPATH=/app:/app/src, but child task scripts must NOT
    # inherit either on PYTHONPATH to avoid shadowing script-local module imports.
    raw_pythonpath = env.get("PYTHONPATH", "")
    if raw_pythonpath:
        kept_parts = [
            p for p in raw_pythonpath.split(os.pathsep) if p and Path(p).resolve() not in (Path("/app"), Path("/app/src"))
        ]
    else:
        kept_parts = []
    # The child still gets ``child_site`` first: its sitecustomize gives matplotlib a
    # Chinese glyph fallback, since the scripts name macOS fonts the container lacks.
    child_site = str(CHILD_SITE_DIR)
    env["PYTHONPATH"] = os.pathsep.join([child_site, *[p for p in kept_parts if p != child_site]])

    venv_bin = "/app/.venv/bin"
    cur_path = env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    path_parts = [p for p in cur_path.split(os.pathsep) if p]
    if not path_parts or path_parts[0] != venv_bin:
        path_parts = [venv_bin, *[p for p in path_parts if p != venv_bin]]
    env["PATH"] = os.pathsep.join(path_parts)

    resolved_outbox = (
        str(outbox_file)
        if outbox_file is not None
        else str(Path(outbox_tmp_dir) / "outbox.json")
    )

    env["HOME"] = "/Users/everflow"
    env["OPENCLAW_WORKSPACE"] = str(workspace_dir)
    env["WORKDIR"] = str(workspace_dir)
    env["TZ"] = "America/New_York"
    env["DALTON_PYTHON_BIN"] = CONTAINER_PYTHON_BIN
    env["DALTON_NO_DIRECT_SEND"] = "1"
    env["DALTON_NOTIFY_DRY_RUN"] = "1" if notify_dry_run else "0"
    env["DALTON_COS_UPLOAD_MODE"] = str(cos_mode or "skip")
    env["DALTON_OUTBOX_FILE"] = resolved_outbox
    env["DALTON_RUN_ID"] = str(run_id)
    if job_key:
        env["DALTON_JOB_KEY"] = str(job_key)

    if secret_env:
        for k, v in secret_env.items():
            env[str(k)] = str(v)

    return env


def extract_last_json_object(text: str) -> dict[str, Any] | None:
    """Parse the last complete JSON dict from ``text`` as a whole string (§3.4.3)."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    last_obj: dict[str, Any] | None = None
    idx = 0
    length = len(text)
    while idx < length:
        brace_pos = text.find("{", idx)
        if brace_pos == -1:
            break
        try:
            obj, end_pos = decoder.raw_decode(text, brace_pos)
        except json.JSONDecodeError:
            idx = brace_pos + 1
            continue
        if isinstance(obj, dict):
            last_obj = obj
        idx = max(brace_pos + 1, end_pos)
    return last_obj


def persist_redacted_outbox(
    *,
    outbox_path: Path,
    redactor: SecretRedactor,
    staging_outbox_path: Path | None = None,
    job_key: str = "",
    stdout_text: str = "",
    explicit_payload: Any = None,
    workspace_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """Redact in memory and atomically write the outbox payload to ``outbox_path`` (§4.4.3, §5.1.3).

    The child subprocess writes its unredacted outbox to ``staging_outbox_path`` on ``tmpfs``
    (``/run/dalton-outbox/outbox.json``). This function reads from ``staging_outbox_path`` (or
    falls back to ``outbox_path`` when invoked directly in unit tests), deletes the staging file,
    redacts all secrets in memory, and only then atomically writes ``.dalton-runs/<run_id>/outbox.json``.
    For stdout-driven tasks per §5.1.3 (JSON tasks T18/T21/T22/T40/T44, Markdown task T27, and
    plain-text task T32), it extracts the payload from ``stdout_text`` when no staging file was written.
    """
    raw_payload: Any = explicit_payload
    source_file = staging_outbox_path if staging_outbox_path is not None else outbox_path
    if raw_payload is None and not source_file.is_symlink() and source_file.is_file():
        try:
            raw_text = source_file.read_text(encoding="utf-8")
            try:
                raw_payload = json.loads(raw_text)
            except json.JSONDecodeError:
                raw_payload = {"text": raw_text}
        except Exception:
            raw_payload = None
        finally:
            if staging_outbox_path is not None and staging_outbox_path != outbox_path:
                try:
                    staging_outbox_path.unlink(missing_ok=True)
                except Exception:
                    pass

    payload_source = resolve_task_payload_source(job_key)
    stripped_stdout = stdout_text.strip()
    if raw_payload is None and stripped_stdout:
        parsed = extract_last_json_object(stdout_text)
        if isinstance(parsed, dict) and (
            payload_source == "stdout_json"
            or any(
                k in parsed
                for k in ("feishu_message", "feishu_target", "report_text", "report", "card")
            )
        ):
            raw_payload = parsed
        elif payload_source in ("stdout_markdown", "stdout_text"):
            fmt = "markdown" if payload_source == "stdout_markdown" else "text"
            raw_payload = {
                "job_key": str(job_key),
                "format": fmt,
                "text": stripped_stdout,
                "feishu_message": stripped_stdout,
            }

    if raw_payload is None:
        return None

    if str(job_key) == "asia-leverage-crossview-daily" and workspace_dir is not None:
        from src.dalton.data_jobs.notification_payloads import (
            is_t27_outbox_prepared,
            prepare_t27_run_outbox,
        )

        if isinstance(raw_payload, dict) and not is_t27_outbox_prepared(raw_payload):
            md_source = str(
                raw_payload.get("text")
                or raw_payload.get("feishu_message")
                or stripped_stdout
            )
            raw_payload = prepare_t27_run_outbox(
                md_source,
                workspace_dir=Path(workspace_dir),
                run_artifacts_dir=outbox_path.parent,
                redactor=redactor,
            )

    redacted = redactor.redact_value(raw_payload)
    if not isinstance(redacted, dict):
        redacted = {"payload": redacted}
    atomic_write_json(outbox_path, redacted)
    return redacted


@dataclass
class WrapperExecutionResult:
    """Result of running a single task command inside the container wrapper."""

    exit_code: int
    timed_out: bool = False
    timeout_kind: str | None = None
    redacted_stdout: str = ""
    redacted_stderr: str = ""
    outbox_payload: dict[str, Any] | None = None
    outbox_path: Path | None = None
    staging_outbox_path: Path | None = None
    env_exported: dict[str, str] = field(default_factory=dict)


async def _terminate_process_group(proc: asyncio.subprocess.Process, grace_seconds: float = 5.0) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        pass


def _prepare_staging_outbox_dir(
    outbox_tmp_dir: Path | str | None,
    run_id: str,
) -> tuple[Path, bool]:
    """Resolve the tmpfs directory for child outbox staging.

    In containers, ``/run/dalton-outbox`` is mounted as ``tmpfs``. In non-root local test
    environments where ``/run/dalton-outbox`` is not writable and no ``outbox_tmp_dir`` was
    explicitly passed, fall back to an OS temp directory outside ``workspace_dir``.
    """
    if outbox_tmp_dir is not None:
        target = Path(outbox_tmp_dir)
        target.mkdir(parents=True, exist_ok=True)
        return target, False
    default_dir = Path(DEFAULT_TMPFS_OUTBOX_DIR)
    try:
        default_dir.mkdir(parents=True, exist_ok=True)
        if os.access(default_dir, os.W_OK):
            return default_dir, False
    except OSError:
        pass
    fallback = Path(tempfile.mkdtemp(prefix=f"dalton-outbox-{run_id}-"))
    return fallback, True


async def run_wrapped_command(
    command_argv: Sequence[str],
    *,
    run_id: str,
    job_key: str = "",
    workdir: str = DEFAULT_WORKSPACE_DIR,
    workspace_dir: str = DEFAULT_WORKSPACE_DIR,
    secrets_dir: Path | str | None = DEFAULT_SECRETS_DIR,
    outbox_tmp_dir: Path | str | None = None,
    extra_secrets: Sequence[str] = (),
    notify_dry_run: bool = True,
    cos_mode: str = "skip",
    timeout_seconds: float | None = DEFAULT_TIMEOUT_SECONDS,
    no_output_timeout_seconds: float | None = None,
    poll_interval_seconds: float = 0.05,
    stdout_sink: TextIO | None = None,
    stderr_sink: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    base_env: Mapping[str, str] | None = None,
) -> WrapperExecutionResult:
    """Execute ``command_argv`` with pre-output stream redaction, tmpfs outbox redaction, and dual timeouts."""
    if not command_argv:
        raise ValueError("command_argv must not be empty")

    secret_env, base_redactor = load_secrets_from_dir(secrets_dir)
    combined_secrets = [*base_redactor.patterns, *secret_env.values(), *extra_secrets]
    redactor = SecretRedactor(combined_secrets)

    outbox_path = Path(workspace_dir) / ".dalton-runs" / str(run_id) / "outbox.json"
    try:
        pre_chain = _open_verified_dir_chain(
            outbox_path.parent,
            (),
            anchor_dir=Path(workspace_dir),
            create_missing=True,
        )
        if pre_chain is not None:
            pre_chain.close()
    except SafeFsError:
        pass

    staging_dir, cleanup_staging_dir = _prepare_staging_outbox_dir(outbox_tmp_dir, str(run_id))
    staging_outbox_path = staging_dir / "outbox.json"
    try:
        staging_outbox_path.unlink(missing_ok=True)
    except Exception:
        pass

    child_env = build_child_env(
        base_env=base_env,
        workspace_dir=workspace_dir,
        run_id=run_id,
        job_key=job_key,
        notify_dry_run=notify_dry_run,
        cos_mode=cos_mode,
        secret_env=secret_env,
        outbox_file=staging_outbox_path,
        outbox_tmp_dir=staging_dir,
    )

    out_stream = stdout_sink if stdout_sink is not None else sys.stdout
    err_stream = stderr_sink if stderr_sink is not None else sys.stderr

    effective_timeout = (
        float(timeout_seconds) if timeout_seconds is not None else float(DEFAULT_TIMEOUT_SECONDS)
    )
    effective_no_output = (
        float(no_output_timeout_seconds) if no_output_timeout_seconds is not None else None
    )

    start_mono = clock()
    last_output_mono = start_mono

    try:
        proc = await asyncio.create_subprocess_exec(
            *command_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir if Path(workdir).is_dir() else None,
            env=child_env,
            start_new_session=True,
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        async def _pump_stream(
            reader: asyncio.StreamReader | None,
            sink: TextIO,
            collector: list[str],
        ) -> None:
            nonlocal last_output_mono
            if reader is None:
                return
            while True:
                line = await reader.readline()
                if not line:
                    break
                last_output_mono = clock()
                decoded = line.decode("utf-8", errors="replace")
                redacted_line = redactor.redact_text(decoded)
                collector.append(redacted_line)
                sink.write(redacted_line)
                sink.flush()

        pump_out = asyncio.create_task(_pump_stream(proc.stdout, out_stream, stdout_chunks))
        pump_err = asyncio.create_task(_pump_stream(proc.stderr, err_stream, stderr_chunks))

        timed_out = False
        timeout_kind: str | None = None

        try:
            while proc.returncode is None:
                now_mono = clock()
                if now_mono - start_mono > effective_timeout:
                    timed_out = True
                    timeout_kind = "total"
                    await _terminate_process_group(proc)
                    break
                if effective_no_output is not None and (now_mono - last_output_mono > effective_no_output):
                    timed_out = True
                    timeout_kind = "no_output"
                    await _terminate_process_group(proc)
                    break
                try:
                    await asyncio.wait_for(proc.wait(), timeout=poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue
        finally:
            await asyncio.gather(pump_out, pump_err, return_exceptions=True)

        if timed_out:
            exit_code = (
                EXIT_CODE_NO_OUTPUT_TIMEOUT
                if timeout_kind == "no_output"
                else EXIT_CODE_TOTAL_TIMEOUT
            )
        else:
            exit_code = int(proc.returncode or 0)
        redacted_stdout = "".join(stdout_chunks)
        redacted_stderr = "".join(stderr_chunks)

        from src.dalton.data_jobs.notification_payloads import DataTaskNotificationPrepError

        try:
            outbox_payload = persist_redacted_outbox(
                outbox_path=outbox_path,
                staging_outbox_path=staging_outbox_path,
                job_key=job_key,
                redactor=redactor,
                stdout_text=redacted_stdout,
                workspace_dir=workspace_dir if exit_code == 0 else None,
            )
        except (DataTaskNotificationPrepError, SafeFsError) as prep_exc:
            redacted_err = redactor.redact_text(
                f"runner_wrapper: report preparation failed: {prep_exc}\n"
            )
            err_stream.write(redacted_err)
            err_stream.flush()
            redacted_stderr = f"{redacted_stderr}{redacted_err}"
            outbox_payload = None
            safe_unlink_contained_regular_file(
                outbox_path.parent,
                outbox_path.name,
                anchor_dir=Path(workspace_dir),
            )
            if exit_code == 0:
                exit_code = 1
    finally:
        try:
            staging_outbox_path.unlink(missing_ok=True)
        except Exception:
            pass
        if cleanup_staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return WrapperExecutionResult(
        exit_code=exit_code,
        timed_out=timed_out,
        timeout_kind=timeout_kind,
        redacted_stdout=redacted_stdout,
        redacted_stderr=redacted_stderr,
        outbox_payload=outbox_payload,
        outbox_path=outbox_path,
        staging_outbox_path=staging_outbox_path,
        env_exported=child_env,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dalton OC-1 data task container wrapper")
    parser.add_argument("--run-id", required=True, help="UUID of the current data task run")
    parser.add_argument("--job-key", required=True, help="Task job_key from dalton_data_task_defs")
    parser.add_argument("--workdir", default=DEFAULT_WORKSPACE_DIR)
    parser.add_argument("--workspace-dir", default=DEFAULT_WORKSPACE_DIR)
    parser.add_argument("--secrets-dir", default=DEFAULT_SECRETS_DIR)
    parser.add_argument("--outbox-tmp-dir", default=DEFAULT_TMPFS_OUTBOX_DIR)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--no-output-timeout-seconds", type=float, default=None)
    parser.add_argument(
        "--notify-dry-run",
        choices=("0", "1"),
        default="1",
        help="1 in shadow-run mode, 0 after cutover",
    )
    parser.add_argument("--cos-mode", choices=("skip", "prod"), default="skip")
    parser.add_argument(
        "command_argv",
        nargs=argparse.REMAINDER,
        help="Command and arguments to execute (after '--')",
    )
    return parser


def _prewarm_yfinance_cache() -> None:
    """Create yfinance's SQLite caches before the task starts its download threads.

    Every task runs in a fresh container, so the caches under
    ``~/.cache/py-yfinance`` do not exist yet; ``yf.download(threads=True)``
    then creates them from several threads at once and the losers fail with
    ``database is locked`` (T18's first production trial, 11-08). Creating
    them here, single-threaded, removes the race. Best effort: a task that
    does not use yfinance is unaffected, and a failure here only means the
    task meets the original behaviour.
    """
    try:
        import yfinance.cache as yf_cache
    except Exception:
        return
    try:
        yf_cache.get_tz_cache().lookup("QQQ")
        yf_cache.get_cookie_cache().lookup("basic")
    except Exception as exc:  # pragma: no cover - logged and ignored
        print(f"runner_wrapper: yfinance cache prewarm skipped: {exc}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    cmd = list(args.command_argv)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        parser.error("Missing command_argv after '--'")

    _prewarm_yfinance_cache()
    result = asyncio.run(
        run_wrapped_command(
            cmd,
            run_id=args.run_id,
            job_key=args.job_key,
            workdir=args.workdir,
            workspace_dir=args.workspace_dir,
            secrets_dir=args.secrets_dir,
            outbox_tmp_dir=args.outbox_tmp_dir,
            notify_dry_run=(args.notify_dry_run == "1"),
            cos_mode=args.cos_mode,
            timeout_seconds=args.timeout_seconds,
            no_output_timeout_seconds=args.no_output_timeout_seconds,
        )
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
