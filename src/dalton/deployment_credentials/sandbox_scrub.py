"""Workspace sandbox Vault isolation and legacy Feishu credential scrubber (§3.2, §3.3).

Three-layer sandbox isolation architecture:
1. **Env-var dual check (target key & ``${VAR}`` source name)**:
   ``src/dalton/settings.py`` defines ``SERVER_ONLY_CREDENTIAL_ENV_NAMES``,
   ``is_server_only_credential_env_name()``, and key-name stripping in
   ``sanitize_sandbox_env_vars()``; ``src/ptc_agent/core/sandbox/ptc_sandbox.py``
   enforces both checks in ``_deployment_env_vars()`` before merging into
   ``_build_sandbox_env_vars()`` across Docker, Daytona, E2B, and SSH providers.
2. **High-entropy substring value scrubbing**:
   ``src/dalton/settings.py:managed_secret_token_values()`` includes configured
   server-only credential values meeting ``SERVER_ONLY_SECRET_MIN_SUBSTRING_LEN``
   (16), so renamed keys and embedded URL/header/arg leaks are scrubbed
   automatically by ``sanitize_sandbox_env_vars()``,
   ``sanitize_mcp_client_config_server()``, and ``redact_managed_tokens()``.
3. **Sandbox Vault sync stripping & one-time online cleanup**:
   ``strip_sandbox_excluded_vault_secrets()`` removes
   ``SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS`` at every sandbox-bound Vault delivery
   point (``src/server/services/computer_manager/_mcp.py`` and
   ``src/ptc_agent/core/sandbox/assets.py``). For pre-existing sandboxes whose
   on-disk ``_internal/.vault_secrets.json`` already holds legacy Feishu keys,
   ``SandboxFeishuVaultScrubber`` cleans running sandboxes on startup with a
   30-second jittered retry loop (mounted in ``src/server/app/setup.py``), and
   ``scrub_sandbox_on_ready()`` cleans stopped sandboxes when they next become
   ready in ``src/server/services/computer_manager/_lifecycle.py``, recording
   state in ``dalton_sandbox_feishu_vault_scrub``.

Why controlled backend and gateway reads remain unfiltered:
Filtering applies strictly on sandbox-bound paths so workspace sandboxes stay
isolated from server-only credentials. Database reads via
``get_user_secrets_decrypted()``, primary-admin reads via
``load_dedicated_feishu_vault_credentials()``, the internal gateway endpoint
``POST /api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials``,
report delivery membership verification (``report_delivery.py``), and output
redaction (``secret_redactor.py`` / ``LeakDetectionMiddleware``) continue
reading the intact Vault entries for Batch 1 read-only fallback, live key
rotation, and leak detection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shlex
import uuid
from collections.abc import Mapping
from typing import Any, Optional

from ptc_agent.core.paths import SandboxLayout

from src.dalton.deployment_credentials import sandbox_scrub_store as _store
from src.dalton.settings import is_dalton_enabled

logger = logging.getLogger(__name__)

SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "DALTON_FEISHU_ENCRYPT_KEY",
        "DALTON_FEISHU_VERIFICATION_TOKEN",
    }
)

SWEEP_INTERVAL_S = 30.0
SWEEP_BATCH_LIMIT = 25
STOP_GRACE = 30.0


class VaultScrubIOError(RuntimeError):
    """Sandbox file inspection or replacement failed (carries zero secret text)."""


class VaultScrubParseError(ValueError):
    """On-disk sandbox vault JSON could not be parsed safely (carries zero secret text)."""


class VaultScrubVerificationError(RuntimeError):
    """Post-scrub read-back failed to confirm excluded Feishu keys were removed."""


def strip_sandbox_excluded_vault_secrets(
    secrets: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Return a copy of ``secrets`` without ``SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS``.

    Preserves ``None`` unchanged so callers such as ``_vault_is_behind`` keep
    their distinction between "no vault opinion" and an empty vault.
    """
    if secrets is None:
        return None
    return {
        k: v
        for k, v in secrets.items()
        if k not in SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS
    }


def has_sandbox_excluded_vault_keys(secrets: Mapping[str, Any] | None) -> bool:
    """Return True when ``secrets`` contains any key in ``SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS``."""
    if not secrets:
        return False
    return any(k in SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS for k in secrets)


def _canonical_vault_json_bytes(secrets: Mapping[str, str]) -> bytes:
    """Serialize stripped vault secrets identically to ``assets._canonical_vault_json``."""
    return json.dumps(
        dict(secrets), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


async def _read_vault_file_bytes(runtime: Any, vault_path: str) -> bytes | None:
    """Read raw bytes of ``vault_path`` from ``runtime``, or ``None`` when absent.

    Explicitly confirms file absence before returning ``None`` so transport or
    execution failures raise and get retried rather than marking clean.
    """
    quoted = shlex.quote(vault_path)
    check = await runtime.exec(
        f"if [ ! -e {quoted} ]; then echo absent; else echo present; fi"
    )
    if getattr(check, "exit_code", 1) != 0:
        raise VaultScrubIOError("Failed to inspect sandbox vault file")
    marker = (getattr(check, "stdout", "") or "").strip()
    if marker == "absent":
        return None
    if marker != "present":
        raise VaultScrubIOError("Unexpected sandbox vault inspection marker")
    try:
        return await runtime.download_file(vault_path)
    except FileNotFoundError:
        recheck = await runtime.exec(
            f"if [ ! -e {quoted} ]; then echo absent; else echo present; fi"
        )
        if getattr(recheck, "exit_code", 1) == 0 and (
            getattr(recheck, "stdout", "") or ""
        ).strip() == "absent":
            return None
        raise VaultScrubIOError("Sandbox vault file download failed") from None


def _parse_vault_json(raw: bytes) -> dict[str, str]:
    """Decode and parse on-disk vault JSON without embedding raw content in errors."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise VaultScrubParseError("Invalid JSON in sandbox vault file") from None
    if not isinstance(parsed, dict):
        raise VaultScrubParseError("Sandbox vault file must contain a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}


async def scrub_vault_file_on_runtime(
    runtime: Any,
    vault_path: str,
) -> tuple[bool, dict[str, str]]:
    """Remove ``SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS`` from ``vault_path`` on ``runtime``.

    Returns ``(changed, remaining_secrets)``:
    - Absent file or file without excluded keys: leaves disk untouched and returns ``False``.
    - Remaining non-empty secrets: uploads canonical JSON to a same-directory temp
      file and atomically replaces ``vault_path`` via ``mv -f``.
    - Remaining empty secrets: removes ``vault_path`` via ``rm -f``.
    - Always reads back the target path to verify zero excluded keys remain.
    """
    raw = await _read_vault_file_bytes(runtime, vault_path)
    if raw is None:
        return False, {}

    existing = _parse_vault_json(raw)
    if not has_sandbox_excluded_vault_keys(existing):
        return False, existing

    cleaned = strip_sandbox_excluded_vault_secrets(existing) or {}
    quoted_path = shlex.quote(vault_path)

    if cleaned:
        payload = _canonical_vault_json_bytes(cleaned)
        tmp_path = f"{vault_path}.scrub.{uuid.uuid4().hex[:12]}.tmp"
        quoted_tmp = shlex.quote(tmp_path)
        try:
            await runtime.upload_file(payload, tmp_path)
            mv_res = await runtime.exec(
                f"chmod 600 {quoted_tmp} 2>/dev/null || true; "
                f"mv -f {quoted_tmp} {quoted_path}"
            )
            if getattr(mv_res, "exit_code", 1) != 0:
                raise VaultScrubIOError("Failed to replace sandbox vault file")
        except BaseException:
            try:
                await runtime.exec(f"rm -f {quoted_tmp}")
            except Exception:
                pass
            raise
    else:
        rm_res = await runtime.exec(f"rm -f {quoted_path}")
        if getattr(rm_res, "exit_code", 1) != 0:
            raise VaultScrubIOError("Failed to remove empty sandbox vault file")

    verify_raw = await _read_vault_file_bytes(runtime, vault_path)
    if verify_raw is not None:
        try:
            verify_parsed = _parse_vault_json(verify_raw)
        except VaultScrubParseError:
            raise VaultScrubVerificationError(
                "Sandbox vault read-back failed JSON validation"
            ) from None
        if has_sandbox_excluded_vault_keys(verify_parsed):
            raise VaultScrubVerificationError(
                "Excluded Feishu vault keys remained on disk after scrub"
            )
        return True, verify_parsed
    return True, {}


def _sync_in_memory_vault_secrets(
    sandbox: Any,
    *,
    changed: bool,
    remaining: Mapping[str, str],
) -> None:
    """Keep ``sandbox.vault_secrets`` aligned with the scrubbed on-disk file."""
    if sandbox is None:
        return
    current = getattr(sandbox, "vault_secrets", None)
    if changed:
        sandbox.vault_secrets = dict(remaining)
    elif isinstance(current, Mapping) and has_sandbox_excluded_vault_keys(current):
        sandbox.vault_secrets = strip_sandbox_excluded_vault_secrets(current) or {}


async def _scrub_runtime_under_optional_lock(
    runtime: Any,
    vault_path: str,
    sandbox: Any = None,
) -> tuple[bool, dict[str, str]]:
    """Run ``scrub_vault_file_on_runtime`` serialized with ``sandbox._tool_refresh_lock`` when present."""
    refresh_lock = getattr(sandbox, "_tool_refresh_lock", None) if sandbox is not None else None
    if refresh_lock is not None:
        async with refresh_lock:
            changed, remaining = await scrub_vault_file_on_runtime(runtime, vault_path)
            _sync_in_memory_vault_secrets(sandbox, changed=changed, remaining=remaining)
            return changed, remaining
    changed, remaining = await scrub_vault_file_on_runtime(runtime, vault_path)
    _sync_in_memory_vault_secrets(sandbox, changed=changed, remaining=remaining)
    return changed, remaining


async def scrub_sandbox_on_ready(binding: Any, session: Any) -> bool:
    """Ensure legacy Feishu Vault keys are removed when a sandbox becomes ready (§3.2).

    Invoked from ``ComputerManager._complete_phase2_sync`` right after
    ``session.sandbox.ensure_sandbox_ready()`` regardless of ``mcp_changed`` or
    ``skills_changed``. Caches the verified ``provider_ref`` on the ``session``
    object itself to keep repeat warm checks off the database, uses
    ``sandbox._tool_refresh_lock`` (rather than ``computer_advisory_key``) so
    the request path stays serialized with in-worker asset sync without pinning
    a PostgreSQL session lock across sandbox I/O, and swallows all errors after
    recording ``status='failed'`` so startup always succeeds.
    """
    if not is_dalton_enabled():
        return False

    computer_id = str(getattr(binding, "computer_id", "") or "").strip()
    sandbox = getattr(session, "sandbox", None)
    if not computer_id or sandbox is None:
        return False

    runtime = getattr(sandbox, "runtime", None)
    raw_ref = (
        getattr(sandbox, "sandbox_id", None)
        or getattr(binding, "provider_ref", None)
        or (getattr(runtime, "id", None) if runtime is not None else None)
    )
    provider_ref = str(raw_ref or "").strip()
    if not provider_ref:
        return False

    if getattr(session, "_dalton_feishu_vault_scrub_checked", None) == provider_ref:
        return False

    try:
        state = await _store.get_scrub_state(computer_id)
        if (
            state is not None
            and state.get("status") == "cleaned"
            and state.get("provider_ref") == provider_ref
        ):
            session._dalton_feishu_vault_scrub_checked = provider_ref
            return False

        if runtime is None:
            return False

        layout = getattr(sandbox, "layout", None)
        vault_path = (
            layout.vault_secrets
            if layout is not None and getattr(layout, "vault_secrets", None)
            else SandboxLayout.for_root(getattr(binding, "root_dir", None)).vault_secrets
        )

        await _scrub_runtime_under_optional_lock(runtime, vault_path, sandbox=sandbox)
        await _store.mark_cleaned(computer_id, provider_ref)
        session._dalton_feishu_vault_scrub_checked = provider_ref
        return True
    except Exception as exc:
        exc_type = type(exc).__name__
        logger.warning(
            "[SandboxFeishuVaultScrubber] ready-hook scrub failed computer_id=%s error_type=%s",
            computer_id,
            exc_type,
        )
        try:
            await _store.mark_failed(computer_id, provider_ref, exc_type)
        except Exception as store_exc:
            logger.warning(
                "[SandboxFeishuVaultScrubber] failed to record ready-hook failure computer_id=%s error_type=%s",
                computer_id,
                type(store_exc).__name__,
            )
        return False


class SandboxFeishuVaultScrubber:
    """Periodic background scrubber clearing legacy Feishu Vault keys from running sandboxes.

    Mirrors ``PlatformSecretSweeper``'s lifecycle and per-computer advisory lock
    pattern while keeping running containers alive and idle timers untouched.
    """

    _instance: Optional["SandboxFeishuVaultScrubber"] = None

    def __init__(self, *, interval: float = SWEEP_INTERVAL_S) -> None:
        self._interval = interval
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._cursor: Optional[str] = None
        self._deferred_marked: bool = False

    @classmethod
    def get_instance(cls) -> "SandboxFeishuVaultScrubber":
        if cls._instance is None:
            cls._instance = SandboxFeishuVaultScrubber()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the background scrub loop when Dalton mode is enabled."""
        if not is_dalton_enabled():
            return
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(
            self._loop(), name="sandbox-feishu-vault-scrubber"
        )
        logger.info(
            "[SandboxFeishuVaultScrubber] started (interval=%.0fs)",
            self._interval,
        )

    async def stop(self) -> None:
        if self._loop_task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._loop_task, timeout=STOP_GRACE)
        except TimeoutError:
            logger.warning(
                "[SandboxFeishuVaultScrubber] scrub loop exceeded stop grace; cancelling"
            )
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception as exc:
            logger.warning(
                "[SandboxFeishuVaultScrubber] scrub loop ended with error at shutdown error_type=%s",
                type(exc).__name__,
            )
        self._loop_task = None
        self._stop_event = asyncio.Event()

    async def _loop(self) -> None:
        # Run immediately on startup to converge running sandboxes right after deploy.
        while not self._stop_event.is_set():
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[SandboxFeishuVaultScrubber] sweep pass failed error_type=%s",
                    type(exc).__name__,
                )
            jitter = self._interval * (0.8 + 0.4 * random.random())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=jitter)
                return
            except TimeoutError:
                pass

    # ---------------------------------------------------------------- sweep

    async def _mark_initial_deferred_stopped(self) -> None:
        if self._deferred_marked:
            return
        non_running_ids = await _store.list_non_running_uncleaned_computer_ids()
        if non_running_ids:
            await _store.mark_deferred_stopped(non_running_ids)
        self._deferred_marked = True

    async def sweep_once(self) -> int:
        """Run one bounded batch over running uncleaned computers; return cleaned count."""
        if not is_dalton_enabled():
            return 0

        from src.server.services.computer_manager import ComputerManager

        await self._mark_initial_deferred_stopped()

        rows = await _store.list_running_uncleaned(
            limit=SWEEP_BATCH_LIMIT, after=self._cursor
        )
        if not rows and self._cursor is not None:
            rows = await _store.list_running_uncleaned(limit=SWEEP_BATCH_LIMIT)
        self._cursor = (
            str(rows[-1]["computer_id"]) if len(rows) == SWEEP_BATCH_LIMIT else None
        )
        if not rows:
            return 0

        try:
            manager = ComputerManager.get_instance()
        except Exception as exc:
            logger.warning(
                "[SandboxFeishuVaultScrubber] ComputerManager unavailable error_type=%s",
                type(exc).__name__,
            )
            return 0

        cleaned = 0
        for row in rows:
            if self._stop_event.is_set():
                break
            if await self._scrub_one(manager, row):
                cleaned += 1
        return cleaned

    async def _scrub_one(self, manager: Any, row: Mapping[str, Any]) -> bool:
        from src.server.database.computer import computer_advisory_key
        from src.server.database.session_lock import try_session_lock

        computer_id = str(row["computer_id"])
        provider_ref = str(row["provider_ref"])

        async with try_session_lock(computer_advisory_key(computer_id)) as held:
            if not held:
                return False

            provider = None
            try:
                current_state = await _store.get_scrub_state(computer_id)
                if (
                    current_state is not None
                    and current_state.get("status") == "cleaned"
                    and current_state.get("provider_ref") == provider_ref
                ):
                    return False

                binding = manager._binding_from_computer("", dict(row))
                provider = manager._provider_for(binding)
                runtime = await provider.get(provider_ref)

                cached_session = manager._cached_session(computer_id)
                cached_sandbox = (
                    getattr(cached_session, "sandbox", None)
                    if cached_session is not None
                    else None
                )
                matching_sandbox = (
                    cached_sandbox
                    if cached_sandbox is not None
                    and getattr(cached_sandbox, "sandbox_id", None) in (None, provider_ref)
                    else None
                )
                root_dir = getattr(binding, "root_dir", None) or row.get("root_dir")
                vault_path = SandboxLayout.for_root(root_dir).vault_secrets

                await _scrub_runtime_under_optional_lock(
                    runtime, vault_path, sandbox=matching_sandbox
                )
                await _store.mark_cleaned(computer_id, provider_ref)
                if cached_session is not None and matching_sandbox is not None:
                    cached_session._dalton_feishu_vault_scrub_checked = provider_ref
                return True
            except Exception as exc:
                exc_type = type(exc).__name__
                logger.warning(
                    "[SandboxFeishuVaultScrubber] scrub failed computer_id=%s error_type=%s",
                    computer_id,
                    exc_type,
                )
                try:
                    await _store.mark_failed(computer_id, provider_ref, exc_type)
                except Exception as store_exc:
                    logger.warning(
                        "[SandboxFeishuVaultScrubber] failed to record scrub failure computer_id=%s error_type=%s",
                        computer_id,
                        type(store_exc).__name__,
                    )
                return False
            finally:
                if provider is not None:
                    try:
                        await provider.close()
                    except Exception as close_exc:
                        logger.warning(
                            "[SandboxFeishuVaultScrubber] provider close failed computer_id=%s error_type=%s",
                            computer_id,
                            type(close_exc).__name__,
                        )


__all__ = [
    "SANDBOX_EXCLUDED_FEISHU_VAULT_KEYS",
    "STOP_GRACE",
    "SWEEP_BATCH_LIMIT",
    "SWEEP_INTERVAL_S",
    "SandboxFeishuVaultScrubber",
    "VaultScrubIOError",
    "VaultScrubParseError",
    "VaultScrubVerificationError",
    "has_sandbox_excluded_vault_keys",
    "scrub_sandbox_on_ready",
    "scrub_vault_file_on_runtime",
    "strip_sandbox_excluded_vault_secrets",
]
