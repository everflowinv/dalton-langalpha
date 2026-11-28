"""Unit tests for Batch 1 Step 2: 3-layer sandbox credential isolation & legacy Feishu Vault scrubber.

Covers §3.2 Defense 1 (target & ${VAR} source env checks), Defense 2 (high-entropy
substring scrubbing >= 16), Defense 3 (Vault sync stripping, SandboxFeishuVaultScrubber,
and _lifecycle.py ready-point cleanup), controlled backend/gateway reads staying intact,
and zero plaintext leakage into logs, exceptions, or scrub status rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from ptc_agent.config.core import MCPServerConfig
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox import assets as sandbox_assets
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.sandbox.runtime import ExecResult, SandboxProvider
from ptc_agent.core.session import Session
from src.dalton import settings as dalton_settings
from src.dalton.app.router import router as dalton_router
from src.dalton.deployment_credentials import sandbox_scrub as scrub_mod
from src.dalton.deployment_credentials import sandbox_scrub_store as scrub_store
from src.dalton.deployment_credentials import snapshot as dc_snapshot
from src.dalton.services import channel_vault_credentials as cvc_mod
from src.dalton.services import report_delivery as rd_svc
from src.server.database import user_vault_secrets as vault_db
from src.server.models.computer import ComputerStatus
from src.server.models.mcp_server import McpServerInput
from src.server.services import mcp_command_check as mcp_cc
from src.server.services.computer_manager import ComputerManager
from src.server.services.computer_manager._lifecycle import SessionLifecycleMixin
from src.server.services.computer_manager._mcp import McpSecretsMixin
from src.server.services.computer_manager._provisioning import ProvisioningMixin
from src.server.services.computer_manager._types import ComputerBinding
from src.server.services.mcp_validation import Passed
from src.server.services.setup_settings import environment as setup_env
from src.server.utils.secret_redactor import SecretRedactor

_ALL_SERVER_ONLY_NAMES = sorted(dalton_settings.SERVER_ONLY_CREDENTIAL_ENV_NAMES)
_FAKE_FEISHU_ID = "cli_test_not_real_app_id_0001"
_FAKE_FEISHU_SECRET = "test-secret-not-real-feishu-0001"
_FAKE_ENCRYPT_KEY = "test-secret-not-real-encrypt-0002"
_FAKE_VERIFY_TOKEN = "test-secret-not-real-verify-0003"
_FAKE_NORMAL_SECRET = "test-normal-vault-value-0004"


class _TrustedProvider:
    @staticmethod
    def trusted_host(_config: Any) -> bool:
        return True


class _DummyMCPServer:
    def __init__(
        self,
        *,
        name: str,
        transport: str = "stdio",
        url: str | None = None,
        command: str = "python3",
        args: list[str] | None = None,
        headers: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.transport = transport
        self.url = url
        self.command = command
        self.args = list(args or [])
        self.headers = dict(headers or {})
        self.env = dict(env or {})
        self.enabled = enabled


class _InMemoryRuntime:
    """Minimal POSIX-like in-memory sandbox runtime implementing the SandboxRuntime surface."""

    def __init__(
        self,
        runtime_id: str,
        *,
        working_dir: str = "/home/workspace",
        initial_files: dict[str, bytes] | None = None,
        fail_first_exec: Exception | None = None,
        mutate_before_replace: Callable[[dict[str, bytes]], None] | None = None,
    ) -> None:
        self.id = runtime_id
        self.working_dir = working_dir
        self.files: dict[str, bytes] = dict(initial_files or {})
        self.exec_log: list[str] = []
        self.upload_log: list[tuple[str, bytes]] = []
        self.download_log: list[str] = []
        self._fail_first_exec = fail_first_exec
        self._mutate_before_replace = mutate_before_replace
        self.started = False
        self.stopped = False
        self.autostop_called = False

    async def start(self, timeout: int = 120) -> None:
        self.started = True

    async def stop(self, timeout: int = 60, *, force: bool = False) -> None:
        self.stopped = True

    async def set_autostop_interval(self, minutes: int) -> None:
        self.autostop_called = True

    async def upload_file(self, content: bytes, dest_path: str) -> None:
        self.upload_log.append((dest_path, content))
        self.files[dest_path] = bytes(content)

    async def download_file(self, path: str) -> bytes:
        self.download_log.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def exec(
        self,
        command: str,
        timeout: int = 60,
        *,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        import shlex

        self.exec_log.append(command)
        if self._fail_first_exec is not None:
            exc = self._fail_first_exec
            self._fail_first_exec = None
            raise exc

        if 'if [ "$cur" = ' in command:
            if self._mutate_before_replace is not None:
                cb = self._mutate_before_replace
                self._mutate_before_replace = None
                cb(self.files)
            part = command.split("[ ! -e ", 1)[1].split(" ];", 1)[0]
            vault_path = shlex.split(part)[0]
            expected_part = command.split('if [ "$cur" = ', 1)[1].split(" ];", 1)[0]
            expected_sha = shlex.split(expected_part)[0]
            tmp_path: str | None = None
            if "mv -f " in command:
                mv_part = command.split("mv -f ", 1)[1].split(";", 1)[0]
                tmp_path = shlex.split(mv_part)[0]
            if vault_path not in self.files:
                if tmp_path is not None:
                    self.files.pop(tmp_path, None)
                return ExecResult(
                    stdout="",
                    stderr="",
                    exit_code=scrub_mod._VAULT_SCRUB_CONFLICT_EXIT_CODE,
                )
            cur_sha = hashlib.sha256(self.files[vault_path]).hexdigest()
            if cur_sha != expected_sha:
                if tmp_path is not None:
                    self.files.pop(tmp_path, None)
                return ExecResult(
                    stdout="",
                    stderr="",
                    exit_code=scrub_mod._VAULT_SCRUB_CONFLICT_EXIT_CODE,
                )
            if tmp_path is not None:
                if tmp_path not in self.files:
                    return ExecResult(stdout="", stderr="missing src", exit_code=1)
                self.files[vault_path] = self.files.pop(tmp_path)
            else:
                self.files.pop(vault_path, None)
            return ExecResult(stdout="", stderr="", exit_code=0)

        if "then echo absent; else echo present; fi" in command:
            # Extract quoted path between "[ ! -e " and " ];"
            part = command.split("[ ! -e ", 1)[1].split(" ];", 1)[0]
            path = shlex.split(part)[0]
            return ExecResult(
                stdout="present\n" if path in self.files else "absent\n",
                stderr="",
                exit_code=0,
            )
        if "mv -f " in command:
            mv_part = command.split("mv -f ", 1)[1]
            src_path, dst_path = shlex.split(mv_part)[:2]
            if src_path not in self.files:
                return ExecResult(stdout="", stderr="missing src", exit_code=1)
            self.files[dst_path] = self.files.pop(src_path)
            return ExecResult(stdout="", stderr="", exit_code=0)
        if command.startswith("rm -f "):
            target = shlex.split(command[len("rm -f "):])[0]
            self.files.pop(target, None)
            return ExecResult(stdout="", stderr="", exit_code=0)
        if "sha256sum" in command:
            part = command.split("[ ! -e ", 1)[1].split(" ];", 1)[0]
            path = shlex.split(part)[0]
            if path not in self.files:
                return ExecResult(stdout="absent\n", stderr="", exit_code=0)
            digest = hashlib.sha256(self.files[path]).hexdigest()
            return ExecResult(stdout=f"{digest}  {path}\n", stderr="", exit_code=0)
        return ExecResult(stdout="", stderr="", exit_code=0)


def _make_ptc_sandbox_stub(servers: list[Any]) -> Any:
    sandbox_stub = create_autospec(PTCSandbox, instance=True)
    sandbox_stub.provider = _TrustedProvider()
    sandbox_stub._deployment_env_vars.side_effect = (
        lambda bindings: PTCSandbox._deployment_env_vars(sandbox_stub, bindings)
    )
    sandbox_stub.config = SimpleNamespace(sandbox=SimpleNamespace(provider="docker"))
    sandbox_stub._token_file_path = "/home/workspace/.ginlix_token"
    sandbox_stub._builtin_servers.return_value = servers
    return sandbox_stub


@pytest.fixture(autouse=True)
def _clean_isolation_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        *dalton_settings.SERVER_ONLY_CREDENTIAL_ENV_NAMES,
        "DALTON_TRANSCRIPTION_TICKET_SECRET",
        "DALTON_TRANSCRIPTION_TOKEN",
        "DALTON_RESEARCH_ADMIN_TOKEN",
        "DALTON_FEISHU_APP_ID",
        "DALTON_GOOGLE_OAUTH_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DALTON_ENABLED", "1")
    monkeypatch.setattr(setup_env, "_at_start", None)
    dc_snapshot.reset_active_snapshot_for_test()
    scrub_mod.SandboxFeishuVaultScrubber.reset_instance()
    yield
    dc_snapshot.reset_active_snapshot_for_test()
    scrub_mod.SandboxFeishuVaultScrubber.reset_instance()


# ---------------------------------------------------------------------------
# 1. Defense 1: Env-var dual check (target key & ${VAR} source variable name)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret_name", _ALL_SERVER_ONLY_NAMES)
def test_defense1_blocks_server_only_target_and_source_env_names(
    secret_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every SERVER_ONLY_CREDENTIAL_ENV_NAMES entry is blocked both as target key and ${VAR} source."""
    fake_val = f"test-secret-not-real-{secret_name.lower()}-0001"
    monkeypatch.setenv(secret_name, fake_val)
    monkeypatch.setenv("FMP_API_KEY", "test-fmp-public-key-ok")

    assert dalton_settings.is_server_only_credential_env_name(secret_name) is True
    assert dalton_settings.is_server_only_credential_env_name(f" {secret_name} ") is True
    assert dalton_settings.is_server_only_credential_env_name("FMP_API_KEY") is False

    servers = [
        MCPServerConfig(
            name="stdio_test_srv",
            transport="stdio",
            command="python3",
            env={
                secret_name: "literal-non-secret-value",
                "RENAMED_SOURCE_REF": f"${{{secret_name}}}",
                "FMP_API_KEY": "${FMP_API_KEY}",
            },
        )
    ]
    sandbox_stub = _make_ptc_sandbox_stub(servers)

    dep_env = PTCSandbox._deployment_env_vars(sandbox_stub, {})
    assert secret_name not in dep_env
    assert "RENAMED_SOURCE_REF" not in dep_env
    assert dep_env["FMP_API_KEY"] == "test-fmp-public-key-ok"

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        built_env = PTCSandbox._build_sandbox_env_vars(sandbox_stub, {})

    assert secret_name not in built_env
    assert "RENAMED_SOURCE_REF" not in built_env
    assert built_env["FMP_API_KEY"] == "test-fmp-public-key-ok"
    assert fake_val not in json.dumps(built_env)
    assert fake_val not in caplog.text


@pytest.mark.parametrize("secret_name", _ALL_SERVER_ONLY_NAMES)
def test_defense1_sanitize_sandbox_env_vars_removes_by_key_and_disables_server(
    secret_name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sanitize_sandbox_env_vars strips server-only keys even when unset in os.environ and disables server."""
    srv = _DummyMCPServer(
        name="bad_builtin_srv",
        env={secret_name: "short-lit", "FMP_API_KEY": "ok-val"},
        enabled=True,
    )
    env_vars = {
        secret_name: "short-lit",
        "FMP_API_KEY": "ok-val",
    }

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        cleaned = dalton_settings.sanitize_sandbox_env_vars(env_vars, servers=[srv])

    assert secret_name not in cleaned
    assert cleaned == {"FMP_API_KEY": "ok-val"}
    assert srv.enabled is False
    assert "bad_builtin_srv" in caplog.text
    assert secret_name in caplog.text
    assert "short-lit" not in caplog.text


# ---------------------------------------------------------------------------
# 2. Defense 2: High-entropy value substring scrubbing (threshold >= 16)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name",
    [
        "BYOK_ENCRYPTION_KEY",
        "JWT_SIGNING_SECRET",
        "BOOTSTRAP_SETUP_TOKEN",
        "DALTON_DISCORD_BOT_TOKEN",
        "AUTOMATION_WEBHOOK_SECRET",
        "PLUGIN_MARKETPLACE_TOKEN",
        "APNS_PRIVATE_KEY",
    ],
)
def test_defense2_high_entropy_server_only_secret_scrubbed_by_value(
    env_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Configured server-only secrets with length >= 16 are scrubbed across env, URL, args, headers, and logs."""
    high_entropy_val = f"test-secret-not-real-high-entropy-{env_name.lower()}"
    assert len(high_entropy_val) >= dalton_settings.SERVER_ONLY_SECRET_MIN_SUBSTRING_LEN
    monkeypatch.setenv(env_name, high_entropy_val)

    assert high_entropy_val in dalton_settings.managed_secret_token_values()

    srv = _DummyMCPServer(
        name="renamed_leak_srv",
        url=f"https://mcp.example.com/{high_entropy_val}/rpc",
        args=["--token", high_entropy_val],
        headers={"Authorization": f"Bearer {high_entropy_val}"},
        env={"CUSTOM_X": high_entropy_val},
        enabled=True,
    )
    env_vars = {"CUSTOM_X": high_entropy_val, "SAFE_VAR": "keep-me"}
    entry = {
        "url": srv.url,
        "args": list(srv.args),
        "headers": dict(srv.headers),
        "env": dict(srv.env),
        "env_keys": ["CUSTOM_X"],
    }

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        cleaned_env = dalton_settings.sanitize_sandbox_env_vars(env_vars, servers=[srv])
        cleaned_entry = dalton_settings.sanitize_mcp_client_config_server(srv, entry)

    assert cleaned_env == {"SAFE_VAR": "keep-me"}
    assert srv.enabled is False
    assert cleaned_entry["enabled"] is False
    assert cleaned_entry["available"] is False
    assert high_entropy_val not in json.dumps(cleaned_entry)
    assert high_entropy_val not in caplog.text

    redacted = dalton_settings.redact_managed_tokens(f"leak={high_entropy_val} ok")
    assert redacted == "leak=*** ok"
    assert high_entropy_val not in redacted


def test_defense2_len_15_server_only_secret_does_not_trigger_substring_scrub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 15-character server-only secret stays below threshold 16 so unrelated env vars are unaffected."""
    short_15 = "short-sec-12345"
    assert len(short_15) == 15
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", short_15)

    assert short_15 not in dalton_settings.managed_secret_token_values()

    srv = _DummyMCPServer(
        name="normal_srv",
        url=f"https://api.example.com/{short_15}",
        env={"CUSTOM_LABEL": short_15},
        enabled=True,
    )
    env_vars = {"CUSTOM_LABEL": short_15, "OTHER": "safe"}
    cleaned = dalton_settings.sanitize_sandbox_env_vars(dict(env_vars), servers=[srv])
    assert cleaned == env_vars
    assert srv.enabled is True


# ---------------------------------------------------------------------------
# 3. Defense 3 (Part 1 & 2): Vault stripping on all sandbox sync paths
# ---------------------------------------------------------------------------


def test_strip_sandbox_excluded_vault_secrets_semantics() -> None:
    assert scrub_mod.strip_sandbox_excluded_vault_secrets(None) is None
    assert scrub_mod.has_sandbox_excluded_vault_keys(None) is False
    assert scrub_mod.has_sandbox_excluded_vault_keys({"NORMAL": "1"}) is False

    raw = {
        "NORMAL_KEY": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        "DALTON_FEISHU_ENCRYPT_KEY": _FAKE_ENCRYPT_KEY,
        "DALTON_FEISHU_VERIFICATION_TOKEN": _FAKE_VERIFY_TOKEN,
    }
    raw_copy = dict(raw)
    assert scrub_mod.has_sandbox_excluded_vault_keys(raw) is True
    stripped = scrub_mod.strip_sandbox_excluded_vault_secrets(raw)
    assert stripped == {"NORMAL_KEY": _FAKE_NORMAL_SECRET}
    assert raw == raw_copy  # input dict is never mutated


@pytest.mark.asyncio
async def test_mcp_vault_snapshot_settle_and_push_strip_feishu_keys_preserve_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_vault_snapshot, _settle_vault, and push_user_vault strip Feishu keys while preserving DB fingerprint."""
    mixed_v1 = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        "DALTON_FEISHU_ENCRYPT_KEY": _FAKE_ENCRYPT_KEY,
        "DALTON_FEISHU_VERIFICATION_TOKEN": _FAKE_VERIFY_TOKEN,
    }
    only_feishu_v2 = {
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
    }

    snapshot_fn = create_autospec(
        vault_db.get_user_vault_snapshot,
        side_effect=[(mixed_v1, "fp-1"), (only_feishu_v2, "fp-2"), (mixed_v1, "fp-3")],
    )
    fingerprint_fn = create_autospec(
        vault_db.get_user_vault_fingerprint,
        side_effect=["fp-2", "fp-2", "fp-3"],
    )
    published_calls: list[dict[str, str]] = []

    async def _fake_publish(_sandbox: Any, secrets: dict[str, str]) -> bool:
        published_calls.append(dict(secrets))
        return True

    publish_mock = create_autospec(
        sandbox_assets.publish_vault_secrets, side_effect=_fake_publish
    )
    monkeypatch.setattr(vault_db, "get_user_vault_snapshot", snapshot_fn)
    monkeypatch.setattr(vault_db, "get_user_vault_fingerprint", fingerprint_fn)
    monkeypatch.setattr(sandbox_assets, "publish_vault_secrets", publish_mock)

    class _DummyManager(McpSecretsMixin):
        def __init__(self, sandbox: Any) -> None:
            self._sb = sandbox

        def _live_session_computer(self, _ws_id: str) -> str:
            return "comp-1"

        async def get_session_if_ready_for_work(self, *_args: Any, **_kwargs: Any) -> Any:
            sess = create_autospec(Session, instance=True)
            sess.sandbox = self._sb
            return sess

    sandbox_obj = create_autospec(PTCSandbox, instance=True)
    sandbox_obj.vault_secrets = {}
    mgr = _DummyManager(sandbox_obj)

    # 1. _vault_snapshot strips Feishu keys and keeps DB fingerprint untouched.
    owner, snap_secrets, fp = await mgr._vault_snapshot("ws-1", "user-1")
    assert owner == "user-1"
    assert fp == "fp-1"
    assert snap_secrets == {"USER_TOKEN": _FAKE_NORMAL_SECRET}

    # 2. _settle_vault strips initial & re-read snapshots (fp-1 -> fp-2 -> only Feishu -> {}).
    settled = await mgr._settle_vault("user-1", sandbox_obj, dict(mixed_v1), "fp-1")
    assert settled == {}
    assert published_calls == [{"USER_TOKEN": _FAKE_NORMAL_SECRET}, {}]

    # 3. push_user_vault strips before settle and writes stripped dict to sandbox.vault_secrets.
    import src.server.services.computer_manager._mcp as mcp_mod

    monkeypatch.setattr(
        mcp_mod,
        "db_get_workspace_identity",
        create_autospec(
            mcp_mod.db_get_workspace_identity,
            return_value={"sandbox_id": "sb-1", "computer_kind": "docker"},
        ),
    )
    published_calls.clear()
    reached = await mgr.push_user_vault("user-1", ["ws-1"])
    assert reached == 1
    assert published_calls == [{"USER_TOKEN": _FAKE_NORMAL_SECRET}]
    assert sandbox_obj.vault_secrets == {"USER_TOKEN": _FAKE_NORMAL_SECRET}


@pytest.mark.asyncio
async def test_assets_vault_is_behind_and_publish_changed_vault_defense_in_depth() -> None:
    """_vault_is_behind and _publish_changed_vault strip Feishu keys, handle None, and rm -f when empty."""
    legacy_mixed = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
    }
    legacy_mixed_digest = hashlib.sha256(
        sandbox_assets._canonical_vault_json(legacy_mixed)
    ).hexdigest()

    # None preserves "no vault opinion" semantics.
    assert sandbox_assets._vault_is_behind(None, legacy_mixed_digest) is False

    # Old disk file containing Feishu keys differs from stripped target -> behind=True.
    assert sandbox_assets._vault_is_behind(legacy_mixed, legacy_mixed_digest) is True

    # Publishing mixed secrets uploads ONLY stripped normal keys.
    rt = _InMemoryRuntime("sb-assets")
    layout = SandboxLayout.for_root("/home/workspace")

    async def _runtime_call(fn: Any, *args: Any, **_kwargs: Any) -> Any:
        return await fn(*args)

    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.runtime = rt
    sandbox.layout = layout
    sandbox._runtime_call.side_effect = _runtime_call
    did_write = await sandbox_assets._publish_changed_vault(
        sandbox, legacy_mixed, legacy_mixed_digest
    )
    assert did_write is True
    on_disk = json.loads(rt.files[layout.vault_secrets].decode("utf-8"))
    assert on_disk == {"USER_TOKEN": _FAKE_NORMAL_SECRET}

    # Publishing Feishu-only vault removes the file with rm -f.
    feishu_only = {
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
    }
    current_digest = hashlib.sha256(rt.files[layout.vault_secrets]).hexdigest()
    assert sandbox_assets._vault_is_behind(feishu_only, current_digest) is True
    did_rm = await sandbox_assets._publish_changed_vault(
        sandbox, feishu_only, current_digest
    )
    assert did_rm is True
    assert layout.vault_secrets not in rt.files
    assert any(cmd.startswith("rm -f ") for cmd in rt.exec_log)

    # None input is a no-op.
    assert await sandbox_assets._publish_changed_vault(sandbox, None, None) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reusing_sandbox", [False, True])
async def test_sync_project_assets_strips_feishu_on_create_and_resync(
    reusing_sandbox: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both initial create (reusing_sandbox=False) and resync (True) upload stripped vault secrets."""
    import src.server.services.computer_manager._provisioning as prov_mod

    mixed_vault = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        "DALTON_FEISHU_ENCRYPT_KEY": _FAKE_ENCRYPT_KEY,
        "DALTON_FEISHU_VERIFICATION_TOKEN": _FAKE_VERIFY_TOKEN,
    }
    monkeypatch.setattr(
        vault_db,
        "get_user_vault_snapshot",
        create_autospec(
            vault_db.get_user_vault_snapshot, return_value=(mixed_vault, "fp-sync")
        ),
    )
    monkeypatch.setattr(
        vault_db,
        "get_user_vault_fingerprint",
        create_autospec(vault_db.get_user_vault_fingerprint, return_value="fp-sync"),
    )
    monkeypatch.setattr(
        prov_mod,
        "sandbox_skill_sync_params",
        create_autospec(prov_mod.sandbox_skill_sync_params, return_value={}),
    )
    monkeypatch.setattr(
        prov_mod,
        "get_workspace_dir_names_for_computer",
        create_autospec(
            prov_mod.get_workspace_dir_names_for_computer, return_value=["proj"]
        ),
    )
    monkeypatch.setattr(
        prov_mod,
        "workspace_status",
        create_autospec(prov_mod.workspace_status, return_value="running"),
    )

    @asynccontextmanager
    async def _noop_sync_turn(_cid: str):
        yield

    monkeypatch.setattr(prov_mod, "sync_turn", _noop_sync_turn)

    layout = SandboxLayout.for_root("/home/workspace")
    rt = _InMemoryRuntime(
        "sb-sync",
        initial_files=(
            {
                layout.vault_secrets: sandbox_assets._canonical_vault_json(mixed_vault),
            }
            if reusing_sandbox
            else {}
        ),
    )

    async def _runtime_call(fn: Any, *args: Any, **_kwargs: Any) -> Any:
        return await fn(*args)

    captured_ctx_vault: list[Any] = []

    async def _fake_sync_sandbox_assets(**_kwargs: Any) -> Any:
        bound = sandbox_assets._ASSET_SYNC_CONTEXT.get()
        assert bound is not None
        captured_ctx_vault.append(bound[2])
        on_disk = await sandbox_assets._read_vault_digest(sandbox_obj)
        await sandbox_assets._publish_changed_vault(sandbox_obj, bound[2], on_disk)
        return SimpleNamespace(layout_version=2)

    sandbox_obj = create_autospec(PTCSandbox, instance=True)
    sandbox_obj.runtime = rt
    sandbox_obj.layout = layout
    sandbox_obj.vault_secrets = {}
    sandbox_obj.installs_browser = False
    sandbox_obj._tool_refresh_lock = asyncio.Lock()
    sandbox_obj._runtime_call.side_effect = _runtime_call
    sandbox_obj.sync_sandbox_assets.side_effect = _fake_sync_sandbox_assets

    class _DummyProvManager(ProvisioningMixin, McpSecretsMixin):
        config = SimpleNamespace(skills=SimpleNamespace(enabled=False))

        def _machine(self, _cid: str) -> Any:
            return SimpleNamespace(tool_views={})

        @asynccontextmanager
        async def _held_workspace_folder(self, _ws_id: str):
            yield "proj"

        async def _layout_root_owner_dir(self, _binding: Any) -> str:
            return "proj"

        async def _stamp_layout_version(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def _stamp_mcp_config_version(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def _keep_computer_tokens(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    mgr = _DummyProvManager()
    binding = ComputerBinding(
        workspace_id="ws-1",
        computer_id="comp-1",
        kind="docker",
        root_dir="/home/workspace",
        provider_ref="sb-sync",
    )

    await mgr._sync_project_assets(
        binding, "user-1", sandbox_obj, reusing_sandbox=reusing_sandbox
    )

    assert captured_ctx_vault == [{"USER_TOKEN": _FAKE_NORMAL_SECRET}]
    assert sandbox_obj.vault_secrets == {"USER_TOKEN": _FAKE_NORMAL_SECRET}
    disk_payload = json.loads(rt.files[layout.vault_secrets].decode("utf-8"))
    assert disk_payload == {"USER_TOKEN": _FAKE_NORMAL_SECRET}
    assert not scrub_mod.has_sandbox_excluded_vault_keys(disk_payload)


# ---------------------------------------------------------------------------
# 4. Defense 3 (Part 3): SandboxFeishuVaultScrubber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scrubber_online_cleanup_retry_deferred_lock_and_cache_sync(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full lifecycle of SandboxFeishuVaultScrubber on running/stopped sandboxes with zero plaintext leakage."""
    from src.server.database import session_lock as session_lock_mod
    from src.server.services import computer_manager as cm_mod

    layout = SandboxLayout.for_root("/home/workspace")
    vault_path = layout.vault_secrets

    mixed_bytes = sandbox_assets._canonical_vault_json(
        {
            "USER_TOKEN": _FAKE_NORMAL_SECRET,
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
            "DALTON_FEISHU_ENCRYPT_KEY": _FAKE_ENCRYPT_KEY,
            "DALTON_FEISHU_VERIFICATION_TOKEN": _FAKE_VERIFY_TOKEN,
        }
    )
    feishu_only_bytes = sandbox_assets._canonical_vault_json(
        {
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        }
    )

    # Initial fleet state:
    # - comp-mixed (running, sb-mixed): mixed keys on disk + cached session in this worker
    # - comp-only (running, sb-only): Feishu-only keys on disk
    # - comp-flaky (running, sb-flaky): fails transiently on pass 1, succeeds on pass 2
    # - comp-locked (running, sb-locked): advisory lock held by sibling worker -> skipped
    # - comp-clean (running, sb-clean): already cleaned for sb-clean -> never connected
    # - comp-stopped (stopped, sb-stopped): marked deferred_stopped on pass 1
    runtimes: dict[str, _InMemoryRuntime] = {
        "sb-mixed": _InMemoryRuntime("sb-mixed", initial_files={vault_path: mixed_bytes}),
        "sb-only": _InMemoryRuntime("sb-only", initial_files={vault_path: feishu_only_bytes}),
        "sb-flaky": _InMemoryRuntime(
            "sb-flaky",
            initial_files={vault_path: mixed_bytes},
            fail_first_exec=ConnectionError(f"boom {_FAKE_FEISHU_SECRET}"),
        ),
        "sb-locked": _InMemoryRuntime("sb-locked", initial_files={vault_path: mixed_bytes}),
        "sb-clean": _InMemoryRuntime("sb-clean", initial_files={vault_path: mixed_bytes}),
        "sb-clean-v2": _InMemoryRuntime("sb-clean-v2", initial_files={vault_path: feishu_only_bytes}),
    }

    computers: dict[str, dict[str, Any]] = {
        "00000000-0000-4000-8000-000000000001": {
            "computer_id": "00000000-0000-4000-8000-000000000001",
            "status": "running",
            "provider_ref": "sb-mixed",
            "kind": "docker",
            "root_dir": "/home/workspace",
        },
        "00000000-0000-4000-8000-000000000002": {
            "computer_id": "00000000-0000-4000-8000-000000000002",
            "status": "running",
            "provider_ref": "sb-only",
            "kind": "docker",
            "root_dir": "/home/workspace",
        },
        "00000000-0000-4000-8000-000000000003": {
            "computer_id": "00000000-0000-4000-8000-000000000003",
            "status": "running",
            "provider_ref": "sb-flaky",
            "kind": "docker",
            "root_dir": "/home/workspace",
        },
        "00000000-0000-4000-8000-000000000004": {
            "computer_id": "00000000-0000-4000-8000-000000000004",
            "status": "running",
            "provider_ref": "sb-locked",
            "kind": "docker",
            "root_dir": "/home/workspace",
        },
        "00000000-0000-4000-8000-000000000005": {
            "computer_id": "00000000-0000-4000-8000-000000000005",
            "status": "running",
            "provider_ref": "sb-clean",
            "kind": "docker",
            "root_dir": "/home/workspace",
        },
        "00000000-0000-4000-8000-000000000006": {
            "computer_id": "00000000-0000-4000-8000-000000000006",
            "status": "stopped",
            "provider_ref": "sb-stopped",
            "kind": "docker",
            "root_dir": "/home/workspace",
        },
    }

    cid_mixed = "00000000-0000-4000-8000-000000000001"
    cid_only = "00000000-0000-4000-8000-000000000002"
    cid_flaky = "00000000-0000-4000-8000-000000000003"
    cid_locked = "00000000-0000-4000-8000-000000000004"
    cid_clean = "00000000-0000-4000-8000-000000000005"
    cid_stopped = "00000000-0000-4000-8000-000000000006"

    # In-memory scrub table mirroring dalton_sandbox_feishu_vault_scrub semantics.
    scrub_rows: dict[str, dict[str, Any]] = {
        cid_clean: {
            "computer_id": cid_clean,
            "provider_ref": "sb-clean",
            "status": "cleaned",
            "attempt_count": 1,
            "last_error": None,
        }
    }

    async def _fake_get_state(cid: str, *, conn: Any = None) -> dict[str, Any] | None:
        return dict(scrub_rows[cid]) if cid in scrub_rows else None

    async def _fake_mark_cleaned(
        cid: str, provider_ref: str | None, *, conn: Any = None
    ) -> None:
        prev = scrub_rows.get(cid, {})
        scrub_rows[cid] = {
            "computer_id": cid,
            "provider_ref": provider_ref,
            "status": "cleaned",
            "attempt_count": int(prev.get("attempt_count", 0)) + 1,
            "last_error": None,
        }

    async def _fake_mark_failed(
        cid: str, provider_ref: str | None, exc_type: str, *, conn: Any = None
    ) -> None:
        prev = scrub_rows.get(cid, {})
        scrub_rows[cid] = {
            "computer_id": cid,
            "provider_ref": provider_ref,
            "status": "failed",
            "attempt_count": int(prev.get("attempt_count", 0)) + 1,
            "last_error": scrub_store._sanitize_error_type(exc_type),
        }

    async def _fake_list_non_running(*, conn: Any = None) -> list[str]:
        return [
            cid
            for cid, c in sorted(computers.items())
            if c["status"] != "running"
            and c["status"] != "deleted"
            and (
                cid not in scrub_rows
                or scrub_rows[cid]["status"] not in ("deferred_stopped", "cleaned")
            )
        ]

    async def _fake_mark_deferred(cids: list[str], *, conn: Any = None) -> int:
        for cid in cids:
            scrub_rows[cid] = {
                "computer_id": cid,
                "provider_ref": computers[cid]["provider_ref"],
                "status": "deferred_stopped",
                "attempt_count": 0,
                "last_error": None,
            }
        return len(cids)

    async def _fake_list_running(
        limit: int = 25, after: str | None = None, *, conn: Any = None
    ) -> list[dict[str, Any]]:
        out = []
        for cid, c in sorted(computers.items()):
            if c["status"] != "running" or not c.get("provider_ref"):
                continue
            if after is not None and cid <= after:
                continue
            st = scrub_rows.get(cid)
            if st and st["status"] == "cleaned" and st["provider_ref"] == c["provider_ref"]:
                continue
            out.append(dict(c))
        return out[:limit]

    monkeypatch.setattr(
        scrub_store, "get_scrub_state", create_autospec(scrub_store.get_scrub_state, side_effect=_fake_get_state)
    )
    monkeypatch.setattr(
        scrub_store, "mark_cleaned", create_autospec(scrub_store.mark_cleaned, side_effect=_fake_mark_cleaned)
    )
    monkeypatch.setattr(
        scrub_store, "mark_failed", create_autospec(scrub_store.mark_failed, side_effect=_fake_mark_failed)
    )
    monkeypatch.setattr(
        scrub_store,
        "list_non_running_uncleaned_computer_ids",
        create_autospec(
            scrub_store.list_non_running_uncleaned_computer_ids,
            side_effect=_fake_list_non_running,
        ),
    )
    monkeypatch.setattr(
        scrub_store,
        "mark_deferred_stopped",
        create_autospec(scrub_store.mark_deferred_stopped, side_effect=_fake_mark_deferred),
    )
    monkeypatch.setattr(
        scrub_store,
        "list_running_uncleaned",
        create_autospec(scrub_store.list_running_uncleaned, side_effect=_fake_list_running),
    )

    from src.server.database.computer import computer_advisory_key

    locked_key = computer_advisory_key(cid_locked)

    @asynccontextmanager
    async def _fake_try_lock(key: int | str):
        yield key != locked_key

    monkeypatch.setattr(session_lock_mod, "try_session_lock", _fake_try_lock)

    connected_refs: list[str] = []
    closed_count = 0

    def _make_provider() -> Any:
        prov = create_autospec(SandboxProvider, instance=True)

        async def _get(ref: str) -> Any:
            connected_refs.append(ref)
            return runtimes[ref]

        async def _close() -> None:
            nonlocal closed_count
            closed_count += 1

        prov.get.side_effect = _get
        prov.close.side_effect = _close
        return prov

    cached_sandbox_mixed = create_autospec(PTCSandbox, instance=True)
    cached_sandbox_mixed.sandbox_id = "sb-mixed"
    cached_sandbox_mixed.vault_secrets = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
    }
    cached_sandbox_mixed._tool_refresh_lock = asyncio.Lock()
    cached_session_mixed = create_autospec(Session, instance=True)
    cached_session_mixed.sandbox = cached_sandbox_mixed

    fake_manager = create_autospec(ComputerManager, instance=True)
    fake_manager._binding_from_computer.side_effect = lambda ws_id, row: ComputerBinding(
        workspace_id=ws_id,
        computer_id=str(row["computer_id"]),
        kind=row["kind"],
        root_dir=row["root_dir"],
        provider_ref=row["provider_ref"],
    )
    fake_manager._provider_for.side_effect = lambda _binding: _make_provider()
    fake_manager._cached_session.side_effect = (
        lambda cid: cached_session_mixed if cid == cid_mixed else None
    )
    monkeypatch.setattr(
        cm_mod.ComputerManager,
        "get_instance",
        classmethod(lambda cls: fake_manager),
    )

    scrubber = scrub_mod.SandboxFeishuVaultScrubber.get_instance()

    # Pass 1:
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        cleaned_p1 = await scrubber.sweep_once()

    assert cleaned_p1 == 2  # sb-mixed + sb-only
    # 1. Mixed file atomically replaced (temp upload + mv -f) with normal keys only.
    assert json.loads(runtimes["sb-mixed"].files[vault_path].decode("utf-8")) == {
        "USER_TOKEN": _FAKE_NORMAL_SECRET
    }
    assert any("mv -f " in cmd for cmd in runtimes["sb-mixed"].exec_log)
    assert scrub_rows[cid_mixed]["status"] == "cleaned"
    # In-memory cached session vault_secrets synchronized too.
    assert cached_sandbox_mixed.vault_secrets == {"USER_TOKEN": _FAKE_NORMAL_SECRET}
    assert getattr(cached_session_mixed, "_dalton_feishu_vault_scrub_checked") == "sb-mixed"
    # No container restart or idle timer mutation.
    assert runtimes["sb-mixed"].started is False
    assert runtimes["sb-mixed"].stopped is False
    assert runtimes["sb-mixed"].autostop_called is False

    # 2. Feishu-only file deleted via compare-and-remove rm -f.
    assert vault_path not in runtimes["sb-only"].files
    assert any("rm -f " in cmd for cmd in runtimes["sb-only"].exec_log)
    assert scrub_rows[cid_only]["status"] == "cleaned"

    # 3. Stopped sandbox marked deferred_stopped.
    assert scrub_rows[cid_stopped]["status"] == "deferred_stopped"

    # 4. Flaky sandbox recorded failed with attempt_count=1 and exc type name only (zero secret plaintext).
    assert scrub_rows[cid_flaky]["status"] == "failed"
    assert scrub_rows[cid_flaky]["attempt_count"] == 1
    assert scrub_rows[cid_flaky]["last_error"] == "ConnectionError"
    assert _FAKE_FEISHU_SECRET not in caplog.text
    assert _FAKE_FEISHU_SECRET not in str(scrub_rows)

    # 5. Locked computer skipped, already-cleaned computer never connected.
    assert "sb-locked" not in connected_refs
    assert "sb-clean" not in connected_refs
    assert closed_count == 3  # sb-mixed, sb-only, sb-flaky

    # Pass 2: flaky sandbox retries automatically and succeeds.
    cleaned_p2 = await scrubber.sweep_once()
    assert cleaned_p2 == 1
    assert scrub_rows[cid_flaky]["status"] == "cleaned"
    assert scrub_rows[cid_flaky]["attempt_count"] == 2
    assert scrub_rows[cid_flaky]["last_error"] is None
    assert json.loads(runtimes["sb-flaky"].files[vault_path].decode("utf-8")) == {
        "USER_TOKEN": _FAKE_NORMAL_SECRET
    }

    # Recreated sandbox (provider_ref changed on cid_clean) gets re-verified and cleaned.
    computers[cid_clean]["provider_ref"] = "sb-clean-v2"
    cleaned_p3 = await scrubber.sweep_once()
    assert cleaned_p3 == 1
    assert "sb-clean-v2" in connected_refs
    assert vault_path not in runtimes["sb-clean-v2"].files
    assert scrub_rows[cid_clean]["status"] == "cleaned"
    assert scrub_rows[cid_clean]["provider_ref"] == "sb-clean-v2"


def test_scrubber_start_is_inert_when_dalton_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SandboxFeishuVaultScrubber.start() does not schedule a task on non-Dalton deployments."""
    monkeypatch.setenv("DALTON_ENABLED", "0")
    scrubber = scrub_mod.SandboxFeishuVaultScrubber.get_instance()
    scrubber.start()
    assert scrubber._loop_task is None


# ---------------------------------------------------------------------------
# 5. Defense 3 (Part 4): _lifecycle.py ready-point hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_hook_cleans_stopped_sandbox_caches_on_session_and_survives_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stopped sandbox starting with mcp_changed=False, skills_changed=False is scrubbed at ready point."""
    layout = SandboxLayout.for_root("/home/workspace")
    vault_path = layout.vault_secrets
    legacy_bytes = sandbox_assets._canonical_vault_json(
        {
            "USER_TOKEN": _FAKE_NORMAL_SECRET,
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        }
    )

    rt = _InMemoryRuntime("sb-stopped-1", initial_files={vault_path: legacy_bytes})
    cid = "00000000-0000-4000-8000-000000000099"
    state_row: dict[str, Any] = {
        "computer_id": cid,
        "provider_ref": "sb-stopped-1",
        "status": "deferred_stopped",
        "attempt_count": 0,
        "last_error": None,
    }

    get_state_mock = create_autospec(
        scrub_store.get_scrub_state, side_effect=lambda _cid, **kw: dict(state_row)
    )
    mark_cleaned_mock = create_autospec(
        scrub_store.mark_cleaned,
        side_effect=lambda _cid, ref, **kw: state_row.update(
            status="cleaned", provider_ref=ref, last_error=None
        ),
    )
    mark_failed_mock = create_autospec(
        scrub_store.mark_failed,
        side_effect=lambda _cid, ref, err, **kw: state_row.update(
            status="failed",
            provider_ref=ref,
            last_error=scrub_store._sanitize_error_type(err),
        ),
    )
    monkeypatch.setattr(scrub_store, "get_scrub_state", get_state_mock)
    monkeypatch.setattr(scrub_store, "mark_cleaned", mark_cleaned_mock)
    monkeypatch.setattr(scrub_store, "mark_failed", mark_failed_mock)

    # Exercise through real ComputerManager._complete_phase2_sync with mcp_changed=False, skills_changed=False.
    async def _ensure_ready() -> None:
        return None

    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.sandbox_id = "sb-stopped-1"
    sandbox.runtime = rt
    sandbox.layout = layout
    sandbox.vault_secrets = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
    }
    sandbox._tool_refresh_lock = asyncio.Lock()
    sandbox.ensure_sandbox_ready.side_effect = _ensure_ready

    session = create_autospec(Session, instance=True)
    session.sandbox = sandbox
    session.skills_signature = "sig-same"
    binding = ComputerBinding(
        workspace_id="ws-stopped",
        computer_id=cid,
        kind="docker",
        root_dir="/home/workspace",
        provider_ref="sb-stopped-1",
    )

    class _DummyLifecycleManager(SessionLifecycleMixin):
        start_wait_timeout = 5.0

        def _machine(self, _cid: str) -> Any:
            return SimpleNamespace(
                pending_tier_recheck=False,
                pending_lazy_sync=False,
                phase2_event=None,
            )

        async def _apply_session_platform_secret(self, *_a: Any, **_kw: Any) -> None:
            return None

        async def _apply_session_mcp(self, *_a: Any, **_kw: Any) -> Any:
            return None  # mcp_changed = False

        def _freeze_tool_view(self, *_a: Any, **_kw: Any) -> None:
            return None

        def _catches_up_later(self, *_a: Any, **_kw: Any) -> bool:
            return False

        def _sync_owed(self, *_a: Any, **_kw: Any) -> bool:
            return False

        def _record_sync(self, *_a: Any, **_kw: Any) -> None:
            return None

        def _release_phase2_gate(self, *_a: Any, **_kw: Any) -> None:
            return None

    mgr = _DummyLifecycleManager()
    returned = await mgr._complete_phase2_sync(
        binding,
        session,
        workspace_user_id="user-1",
        needs_sync=True,
        needs_deferred_sync=False,
        phase2_owner=True,
        phase2_event=asyncio.Event(),
        mark=lambda _name: None,
        skills_signature="sig-same",  # skills_changed = False
    )
    assert returned is session
    assert json.loads(rt.files[vault_path].decode("utf-8")) == {
        "USER_TOKEN": _FAKE_NORMAL_SECRET
    }
    assert sandbox.vault_secrets == {"USER_TOKEN": _FAKE_NORMAL_SECRET}
    assert state_row["status"] == "cleaned"
    assert get_state_mock.call_count == 1
    assert mark_cleaned_mock.call_count == 1

    # Second entry on the same Session does NOT hit the DB state table again.
    await mgr._complete_phase2_sync(
        binding,
        session,
        workspace_user_id="user-1",
        needs_sync=True,
        needs_deferred_sync=False,
        phase2_owner=True,
        phase2_event=asyncio.Event(),
        mark=lambda _name: None,
        skills_signature="sig-same",
    )
    assert get_state_mock.call_count == 1

    # On scrub error during startup, startup still succeeds, status='failed' is recorded,
    # and the session caches the checked ref so repeat ready entries skip DB queries.
    failing_rt = _InMemoryRuntime(
        "sb-fail-start",
        initial_files={vault_path: legacy_bytes},
        fail_first_exec=TimeoutError(f"timeout {_FAKE_FEISHU_SECRET}"),
    )
    failing_sandbox = create_autospec(PTCSandbox, instance=True)
    failing_sandbox.sandbox_id = "sb-fail-start"
    failing_sandbox.runtime = failing_rt
    failing_sandbox.layout = layout
    failing_sandbox.vault_secrets = {}
    failing_sandbox._tool_refresh_lock = asyncio.Lock()
    failing_sandbox.ensure_sandbox_ready.side_effect = _ensure_ready

    failing_session = create_autospec(Session, instance=True)
    failing_session.sandbox = failing_sandbox
    failing_session.skills_signature = "sig-same"
    failing_binding = ComputerBinding(
        workspace_id="ws-fail",
        computer_id=cid,
        kind="docker",
        root_dir="/home/workspace",
        provider_ref="sb-fail-start",
    )
    state_row["status"] = "deferred_stopped"
    state_row["provider_ref"] = "sb-fail-start"

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        res_fail = await mgr._complete_phase2_sync(
            failing_binding,
            failing_session,
            workspace_user_id="user-1",
            needs_sync=True,
            needs_deferred_sync=False,
            phase2_owner=True,
            phase2_event=asyncio.Event(),
            mark=lambda _name: None,
            skills_signature="sig-same",
        )
    assert res_fail is failing_session
    assert state_row["status"] == "failed"
    assert state_row["last_error"] == "TimeoutError"
    assert getattr(failing_session, "_dalton_feishu_vault_scrub_checked", None) == "sb-fail-start"
    assert _FAKE_FEISHU_SECRET not in caplog.text

    # Second entry on the same failed Session does NOT re-query get_scrub_state (background scrubber retries).
    calls_after_first_fail = get_state_mock.call_count
    await mgr._complete_phase2_sync(
        failing_binding,
        failing_session,
        workspace_user_id="user-1",
        needs_sync=True,
        needs_deferred_sync=False,
        phase2_owner=True,
        phase2_event=asyncio.Event(),
        mark=lambda _name: None,
        skills_signature="sig-same",
    )
    assert get_state_mock.call_count == calls_after_first_fail

    # runtime is None branch also caches _dalton_feishu_vault_scrub_checked on the session.
    no_rt_sandbox = create_autospec(PTCSandbox, instance=True)
    no_rt_sandbox.sandbox_id = "sb-no-rt"
    no_rt_sandbox.runtime = None
    no_rt_sandbox.layout = layout
    no_rt_session = create_autospec(Session, instance=True)
    no_rt_session.sandbox = no_rt_sandbox
    no_rt_binding = ComputerBinding(
        workspace_id="ws-no-rt",
        computer_id=cid,
        kind="docker",
        root_dir="/home/workspace",
        provider_ref="sb-no-rt",
    )
    state_row["status"] = "deferred_stopped"
    state_row["provider_ref"] = "sb-no-rt"
    calls_before_no_rt = get_state_mock.call_count
    assert await scrub_mod.scrub_sandbox_on_ready(no_rt_binding, no_rt_session) is False
    assert get_state_mock.call_count == calls_before_no_rt + 1
    assert getattr(no_rt_session, "_dalton_feishu_vault_scrub_checked", None) == "sb-no-rt"
    assert await scrub_mod.scrub_sandbox_on_ready(no_rt_binding, no_rt_session) is False
    assert get_state_mock.call_count == calls_before_no_rt + 1


# ---------------------------------------------------------------------------
# 6. Controlled backend and gateway reads remain completely unfiltered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_controlled_backend_and_gateway_reads_keep_feishu_credentials_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_user_secrets_decrypted, load_dedicated_feishu_vault_credentials, /feishu-credentials,
    report_delivery, and SecretRedactor all retain full Feishu credentials."""
    full_vault = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        "DALTON_FEISHU_ENCRYPT_KEY": _FAKE_ENCRYPT_KEY,
        "DALTON_FEISHU_VERIFICATION_TOKEN": _FAKE_VERIFY_TOKEN,
    }

    # 1. Database layer: get_user_secrets_decrypted returns intact Feishu keys.
    class _FakeCursor:
        async def __aenter__(self) -> "_FakeCursor":
            return self

        async def __aexit__(self, *_args: Any) -> bool:
            return False

        async def execute(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        async def fetchall(self) -> list[dict[str, str]]:
            return [{"name": k, "plaintext": v} for k, v in full_vault.items()]

    class _FakeConn:
        def cursor(self, **_kwargs: Any) -> _FakeCursor:
            return _FakeCursor()

    @asynccontextmanager
    async def _fake_db_conn(*_args: Any, **_kwargs: Any):
        yield _FakeConn()

    monkeypatch.setattr(vault_db, "get_db_connection", _fake_db_conn)
    monkeypatch.setattr(
        vault_db,
        "encryption_configured",
        create_autospec(vault_db.encryption_configured, return_value=True),
    )
    monkeypatch.setattr(
        vault_db,
        "_get_encryption_key",
        create_autospec(vault_db._get_encryption_key, return_value="test-enc-key-not-real-0001"),
    )
    decrypted = await vault_db.get_user_secrets_decrypted("admin-1")
    assert decrypted["DALTON_FEISHU_APP_ID"] == _FAKE_FEISHU_ID
    assert decrypted["DALTON_FEISHU_APP_SECRET"] == _FAKE_FEISHU_SECRET
    assert decrypted["DALTON_FEISHU_ENCRYPT_KEY"] == _FAKE_ENCRYPT_KEY
    assert decrypted["DALTON_FEISHU_VERIFICATION_TOKEN"] == _FAKE_VERIFY_TOKEN

    # 2. load_dedicated_feishu_vault_credentials returns both dedicated Feishu keys.
    import src.dalton.access_tiers as access_tiers_mod

    monkeypatch.setattr(
        access_tiers_mod,
        "get_primary_admin_user_id",
        create_autospec(access_tiers_mod.get_primary_admin_user_id, return_value="admin-1"),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_primary_admin_user_id_strict",
        create_autospec(access_tiers_mod.get_primary_admin_user_id_strict, return_value="admin-1"),
    )
    loaded = await cvc_mod.load_dedicated_feishu_vault_credentials(
        get_secrets_fn=create_autospec(
            vault_db.get_user_secrets_decrypted, return_value=full_vault
        )
    )
    assert loaded == {
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
    }

    # 3. Internal gateway compatibility endpoint POST .../feishu-credentials stays intact.
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-internal-svc-token-not-real-0001")
    monkeypatch.setenv("DALTON_PROFILE", "everflow")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_GATEWAY", "true")
    monkeypatch.setenv("DALTON_CAP_CHANNEL_FEISHU", "true")
    monkeypatch.setattr(
        dc_snapshot,
        "load_dedicated_feishu_vault_credentials",
        create_autospec(
            cvc_mod.load_dedicated_feishu_vault_credentials,
            return_value={
                "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
                "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
            },
        ),
    )

    app = FastAPI()
    app.include_router(dalton_router)
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials",
            headers={"X-Service-Token": "test-internal-svc-token-not-real-0001"},
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "credentials": {
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        }
    }

    # 4. report_delivery.py still receives Feishu credentials and verifies membership.
    client_mock = create_autospec(httpx.AsyncClient, instance=True)
    client_mock.__aenter__.return_value = client_mock
    client_mock.__aexit__.return_value = False
    tok_resp = create_autospec(httpx.Response, instance=True)
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"code": 0, "tenant_access_token": "test-tenant-tok-0001"}
    members_resp = create_autospec(httpx.Response, instance=True)
    members_resp.status_code = 200
    members_resp.json.return_value = {
        "code": 0,
        "data": {"has_more": False, "items": [{"member_id": "ou_member_1"}]},
    }
    client_mock.post.return_value = tok_resp
    client_mock.get.return_value = members_resp

    ok, err = await rd_svc.verify_feishu_chat_member(
        "oc_group_1",
        "ou_member_1",
        get_credentials_fn=create_autospec(
            rd_svc.load_dedicated_feishu_vault_credentials,
            return_value=loaded,
        ),
        http_client_factory=lambda **_kw: client_mock,
    )
    assert ok is True and err is None

    # 5. Output redactor (SecretRedactor) still redacts Feishu secrets from model output.
    redactor = SecretRedactor()
    redacted_out = redactor.redact(
        f"secret={_FAKE_FEISHU_SECRET} normal={_FAKE_NORMAL_SECRET}",
        vault_secrets=decrypted,
    )
    assert _FAKE_FEISHU_SECRET not in redacted_out
    assert "[REDACTED:DALTON_FEISHU_APP_SECRET]" in redacted_out


# ---------------------------------------------------------------------------
# 7. r2: MCP command check entry guard (env, args, and extra_secrets)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_map", "args_list", "extra_secrets", "expected_key"),
    [
        (
            {"APP_SEC": "${vault:DALTON_FEISHU_APP_SECRET}"},
            ["-y", "@acme/mcp@1.0.0"],
            None,
            "DALTON_FEISHU_APP_SECRET",
        ),
        (
            {},
            ["-y", "@acme/mcp@1.0.0", "--encrypt=${vault:DALTON_FEISHU_ENCRYPT_KEY}"],
            None,
            "DALTON_FEISHU_ENCRYPT_KEY",
        ),
        (
            {"FEISHU_ID": "${vault:DALTON_FEISHU_APP_ID}"},
            ["-y", "@acme/mcp@1.0.0"],
            {
                "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
                "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
            },
            "DALTON_FEISHU_APP_ID",
        ),
        (
            {},
            ["-y", "@acme/mcp@1.0.0", "--verify=${vault:DALTON_FEISHU_VERIFICATION_TOKEN}"],
            {"DALTON_FEISHU_VERIFICATION_TOKEN": _FAKE_VERIFY_TOKEN},
            "DALTON_FEISHU_VERIFICATION_TOKEN",
        ),
    ],
)
async def test_mcp_command_check_blocks_feishu_vault_refs_in_env_args_and_extra_secrets(
    env_map: dict[str, str],
    args_list: list[str],
    extra_secrets: dict[str, str] | None,
    expected_key: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """check_command_server refuses Feishu Vault refs before decrypting, waking, or running commands."""
    decrypt_mock = create_autospec(mcp_cc.get_user_secrets_decrypted)
    sandbox_mock = create_autospec(PTCSandbox, instance=True)
    session_mock = create_autospec(Session, instance=True)
    session_mock.sandbox = sandbox_mock
    manager_mock = create_autospec(ComputerManager, instance=True)
    manager_mock.wake_computer.return_value = session_mock

    monkeypatch.setattr(mcp_cc, "get_user_secrets_decrypted", decrypt_mock)
    monkeypatch.setattr(
        mcp_cc.ComputerManager,
        "current",
        classmethod(lambda cls: manager_mock),
    )

    server = McpServerInput(
        name="feishu_probe",
        transport="stdio",
        command="npx",
        args=args_list,
        env=env_map,
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        result = await mcp_cc.check_command_server(
            "admin-1",
            server,
            extra_secrets=extra_secrets,
        )

    assert result.verdict == "command_failed"
    assert expected_key in result.reason
    assert "Feishu" in result.reason
    decrypt_mock.assert_not_called()
    manager_mock.pick_for_user.assert_not_called()
    manager_mock.wake_computer.assert_not_called()
    sandbox_mock.check_user_mcp_server.assert_not_called()
    for fake_val in (_FAKE_FEISHU_ID, _FAKE_FEISHU_SECRET, _FAKE_ENCRYPT_KEY, _FAKE_VERIFY_TOKEN):
        assert fake_val not in result.reason
        assert fake_val not in caplog.text


@pytest.mark.asyncio
async def test_mcp_command_check_allows_normal_vault_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal non-Feishu Vault refs continue through check_command_server unchanged."""
    decrypt_mock = create_autospec(
        mcp_cc.get_user_secrets_decrypted,
        return_value={"ACME_KEY": _FAKE_NORMAL_SECRET},
    )
    sandbox_mock = create_autospec(PTCSandbox, instance=True)
    sandbox_mock.check_user_mcp_server.return_value = {"status": "ok", "tools": []}
    session_mock = create_autospec(Session, instance=True)
    session_mock.sandbox = sandbox_mock
    manager_mock = create_autospec(ComputerManager, instance=True)
    manager_mock.pick_for_user.return_value = {
        "computer_id": "comp-1",
        "status": "running",
        "provider_ref": "sb-1",
    }
    manager_mock.wake_computer.return_value = session_mock

    monkeypatch.setattr(mcp_cc, "get_user_secrets_decrypted", decrypt_mock)
    monkeypatch.setattr(
        mcp_cc.ComputerManager,
        "current",
        classmethod(lambda cls: manager_mock),
    )

    server = McpServerInput(
        name="acme",
        transport="stdio",
        command="npx",
        args=["-y", "@acme/mcp@1.0.0"],
        env={"ACME_KEY": "${vault:ACME_KEY}"},
    )
    result = await mcp_cc.check_command_server("user-1", server)

    assert result == Passed("acme")
    decrypt_mock.assert_called_once_with("user-1", ["ACME_KEY"])
    sandbox_mock.check_user_mcp_server.assert_called_once()
    assert sandbox_mock.check_user_mcp_server.call_args.args[1] == {
        "ACME_KEY": _FAKE_NORMAL_SECRET
    }


# ---------------------------------------------------------------------------
# 8. r2: Explicit computer start (_machines.py) ready-point scrub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_computer_start_scrubs_legacy_feishu_vault_and_survives_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Explicit machine start (_machines.py) scrubs legacy Feishu Vault keys before publishing running."""
    from src.server.services import browser_settings
    from src.server.services.computer_manager import _machines as machines_mod

    ComputerManager.reset_instance()
    layout = SandboxLayout.for_root("/home/workspace")
    vault_path = layout.vault_secrets
    legacy_bytes = sandbox_assets._canonical_vault_json(
        {
            "USER_TOKEN": _FAKE_NORMAL_SECRET,
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        }
    )

    cid = "00000000-0000-4000-8000-000000000077"
    computer_row: dict[str, Any] = {
        "computer_id": cid,
        "user_id": "user-1",
        "kind": "docker",
        "status": "starting",
        "provider_ref": "sb-explicit-1",
        "resource_tier": "standard",
        "is_always_on": False,
        "root_dir": "/home/workspace",
        "config": {},
    }

    events: list[str] = []
    state_row: dict[str, Any] = {
        "computer_id": cid,
        "provider_ref": "sb-explicit-1",
        "status": "deferred_stopped",
        "attempt_count": 0,
        "last_error": None,
    }

    async def _get_state(_cid: str, **_kw: Any) -> dict[str, Any]:
        return dict(state_row)

    async def _mark_cleaned(_cid: str, ref: str | None, **_kw: Any) -> None:
        events.append("mark_cleaned")
        state_row.update(status="cleaned", provider_ref=ref, last_error=None)

    async def _mark_failed(_cid: str, ref: str | None, err: str, **_kw: Any) -> None:
        events.append("mark_failed")
        state_row.update(
            status="failed",
            provider_ref=ref,
            last_error=scrub_store._sanitize_error_type(err),
        )

    monkeypatch.setattr(
        scrub_store,
        "get_scrub_state",
        create_autospec(scrub_store.get_scrub_state, side_effect=_get_state),
    )
    monkeypatch.setattr(
        scrub_store,
        "mark_cleaned",
        create_autospec(scrub_store.mark_cleaned, side_effect=_mark_cleaned),
    )
    monkeypatch.setattr(
        scrub_store,
        "mark_failed",
        create_autospec(scrub_store.mark_failed, side_effect=_mark_failed),
    )

    async def _publish_status(_cid: str, status: Any, **_kw: Any) -> dict[str, Any]:
        if status == ComputerStatus.RUNNING:
            events.append("publish_running")
        return {**computer_row, "status": "running"}

    update_status_mock = create_autospec(
        machines_mod.update_computer_status,
        side_effect=_publish_status,
    )
    monkeypatch.setattr(machines_mod, "update_computer_status", update_status_mock)
    get_computer_mock = create_autospec(
        machines_mod.get_computer, return_value=dict(computer_row)
    )
    monkeypatch.setattr(machines_mod, "get_computer", get_computer_mock)
    monkeypatch.setattr(
        machines_mod,
        "update_computer_activity",
        create_autospec(machines_mod.update_computer_activity, return_value=None),
    )
    monkeypatch.setattr(
        browser_settings,
        "keep",
        create_autospec(browser_settings.keep, return_value=None),
    )

    def _make_manager(sandbox_id: str) -> ComputerManager:
        cfg = SimpleNamespace(
            sandbox=SimpleNamespace(provider="docker"),
            filesystem=SimpleNamespace(working_directory="/home/workspace"),
            skills=SimpleNamespace(enabled=False),
        )
        mgr = ComputerManager(config=cfg)
        mgr._entitled_tier = create_autospec(mgr._entitled_tier, return_value="standard")
        mgr._entitled_always_on = create_autospec(mgr._entitled_always_on, return_value=False)
        mgr._core_config_for = create_autospec(
            mgr._core_config_for,
            return_value=SimpleNamespace(sandbox=SimpleNamespace(provider="docker")),
        )
        mgr._host_unavailable_policy = create_autospec(
            mgr._host_unavailable_policy, return_value=None
        )
        mgr._keep_computer_tokens = create_autospec(mgr._keep_computer_tokens, return_value=None)
        mgr._sync_machine_assets = create_autospec(mgr._sync_machine_assets, return_value=None)
        mgr._serve_livefs = create_autospec(
            mgr._serve_livefs, return_value=SimpleNamespace(settled=True)
        )
        mgr._link_livefs_soon = create_autospec(mgr._link_livefs_soon, return_value=None)
        mgr._apply_autostop_for_always_on = create_autospec(
            mgr._apply_autostop_for_always_on, return_value=None
        )
        mgr._on_session_attached = create_autospec(mgr._on_session_attached, return_value=None)
        mgr._record_sync = create_autospec(mgr._record_sync, return_value=None)
        mgr._put_session = create_autospec(mgr._put_session, return_value=None)
        mgr._read_machine_facts_soon = create_autospec(
            mgr._read_machine_facts_soon, return_value=None
        )
        mgr._retire_session = create_autospec(mgr._retire_session, return_value=None)
        mgr._session_sandbox_id = create_autospec(
            mgr._session_sandbox_id, return_value=sandbox_id
        )
        return mgr

    # Case 1: Reconnected computer with legacy Feishu keys -> scrubbed BEFORE publishing running.
    rt_ok = _InMemoryRuntime("sb-explicit-1", initial_files={vault_path: legacy_bytes})
    sb_ok = create_autospec(PTCSandbox, instance=True)
    sb_ok.sandbox_id = "sb-explicit-1"
    sb_ok.runtime = rt_ok
    sb_ok.layout = layout
    sb_ok.vault_secrets = {
        "USER_TOKEN": _FAKE_NORMAL_SECRET,
        "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
    }
    sb_ok._tool_refresh_lock = asyncio.Lock()
    sess_ok = create_autospec(Session, instance=True)
    sess_ok.sandbox = sb_ok
    sess_ok.initialize.return_value = None

    monkeypatch.setattr(
        machines_mod.SessionManager,
        "get_cached_session",
        create_autospec(machines_mod.SessionManager.get_cached_session, return_value=None),
    )
    get_session_mock = create_autospec(
        machines_mod.SessionManager.get_session, return_value=sess_ok
    )
    monkeypatch.setattr(machines_mod.SessionManager, "get_session", get_session_mock)

    mgr = _make_manager("sb-explicit-1")
    returned = await mgr._build_machine_session(
        dict(computer_row), user_id="user-1", on_state_observed=None
    )

    assert returned is sess_ok
    assert json.loads(rt_ok.files[vault_path].decode("utf-8")) == {
        "USER_TOKEN": _FAKE_NORMAL_SECRET
    }
    assert sb_ok.vault_secrets == {"USER_TOKEN": _FAKE_NORMAL_SECRET}
    assert state_row["status"] == "cleaned"
    assert events == ["mark_cleaned", "publish_running"]

    # Case 2: Scrub raises during explicit start -> startup still publishes running and marks failed.
    events.clear()
    state_row.update(status="deferred_stopped", provider_ref="sb-explicit-err", last_error=None)
    err_computer = {**computer_row, "provider_ref": "sb-explicit-err"}
    get_computer_mock.return_value = dict(err_computer)

    rt_err = _InMemoryRuntime(
        "sb-explicit-err",
        initial_files={vault_path: legacy_bytes},
        fail_first_exec=TimeoutError(f"boom {_FAKE_FEISHU_SECRET}"),
    )
    sb_err = create_autospec(PTCSandbox, instance=True)
    sb_err.sandbox_id = "sb-explicit-err"
    sb_err.runtime = rt_err
    sb_err.layout = layout
    sb_err.vault_secrets = {}
    sb_err._tool_refresh_lock = asyncio.Lock()
    sess_err = create_autospec(Session, instance=True)
    sess_err.sandbox = sb_err
    sess_err.initialize.return_value = None

    get_session_mock.return_value = sess_err

    mgr_err = _make_manager("sb-explicit-err")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        returned_err = await mgr_err._build_machine_session(
            dict(err_computer), user_id="user-1", on_state_observed=None
        )

    assert returned_err is sess_err
    assert state_row["status"] == "failed"
    assert state_row["last_error"] == "TimeoutError"
    assert events == ["mark_failed", "publish_running"]
    assert _FAKE_FEISHU_SECRET not in caplog.text


# ---------------------------------------------------------------------------
# 9. r2: Compare-and-replace conflict safety & real POSIX /bin/sh verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_mode", ["mixed", "feishu_only"])
async def test_scrubber_compare_and_replace_preserves_concurrent_rotation_and_retries(
    initial_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If another worker rotates normal keys between read and replace, scrub preserves them, marks failed, and succeeds next pass."""
    from src.server.database import session_lock as session_lock_mod
    from src.server.services import computer_manager as cm_mod

    layout = SandboxLayout.for_root("/home/workspace")
    vault_path = layout.vault_secrets
    cid = "00000000-0000-4000-8000-000000000088"
    provider_ref = f"sb-race-{initial_mode}"

    initial_dict = (
        {
            "USER_TOKEN": "old-normal-token",
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        }
        if initial_mode == "mixed"
        else {"DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET}
    )
    rotated_bytes = sandbox_assets._canonical_vault_json({"USER_TOKEN": "rotated-new-token"})

    def _concurrent_push(files: dict[str, bytes]) -> None:
        files[vault_path] = rotated_bytes

    rt = _InMemoryRuntime(
        provider_ref,
        initial_files={vault_path: sandbox_assets._canonical_vault_json(initial_dict)},
        mutate_before_replace=_concurrent_push,
    )

    scrub_rows: dict[str, dict[str, Any]] = {}
    computer_row = {
        "computer_id": cid,
        "status": "running",
        "provider_ref": provider_ref,
        "kind": "docker",
        "root_dir": "/home/workspace",
    }

    async def _get_state(c: str, **_kw: Any) -> dict[str, Any] | None:
        return dict(scrub_rows[c]) if c in scrub_rows else None

    async def _mark_cleaned(c: str, ref: str | None, **_kw: Any) -> None:
        prev = scrub_rows.get(c, {})
        scrub_rows[c] = {
            "computer_id": c,
            "provider_ref": ref,
            "status": "cleaned",
            "attempt_count": int(prev.get("attempt_count", 0)) + 1,
            "last_error": None,
        }

    async def _mark_failed(c: str, ref: str | None, err: str, **_kw: Any) -> None:
        prev = scrub_rows.get(c, {})
        scrub_rows[c] = {
            "computer_id": c,
            "provider_ref": ref,
            "status": "failed",
            "attempt_count": int(prev.get("attempt_count", 0)) + 1,
            "last_error": scrub_store._sanitize_error_type(err),
        }

    async def _list_running(
        limit: int = 25, after: str | None = None, **_kw: Any
    ) -> list[dict[str, Any]]:
        st = scrub_rows.get(cid)
        if st and st["status"] == "cleaned" and st["provider_ref"] == provider_ref:
            return []
        return [dict(computer_row)]

    monkeypatch.setattr(
        scrub_store,
        "get_scrub_state",
        create_autospec(scrub_store.get_scrub_state, side_effect=_get_state),
    )
    monkeypatch.setattr(
        scrub_store,
        "mark_cleaned",
        create_autospec(scrub_store.mark_cleaned, side_effect=_mark_cleaned),
    )
    monkeypatch.setattr(
        scrub_store,
        "mark_failed",
        create_autospec(scrub_store.mark_failed, side_effect=_mark_failed),
    )
    monkeypatch.setattr(
        scrub_store,
        "list_non_running_uncleaned_computer_ids",
        create_autospec(scrub_store.list_non_running_uncleaned_computer_ids, return_value=[]),
    )
    monkeypatch.setattr(
        scrub_store,
        "list_running_uncleaned",
        create_autospec(scrub_store.list_running_uncleaned, side_effect=_list_running),
    )

    @asynccontextmanager
    async def _always_lock(_key: int | str):
        yield True

    monkeypatch.setattr(session_lock_mod, "try_session_lock", _always_lock)

    def _make_provider() -> Any:
        prov = create_autospec(SandboxProvider, instance=True)
        prov.get.return_value = rt
        prov.close.return_value = None
        return prov

    fake_manager = create_autospec(ComputerManager, instance=True)
    fake_manager._binding_from_computer.side_effect = lambda ws_id, row: ComputerBinding(
        workspace_id=ws_id,
        computer_id=str(row["computer_id"]),
        kind=row["kind"],
        root_dir=row["root_dir"],
        provider_ref=row["provider_ref"],
    )
    fake_manager._provider_for.side_effect = lambda _binding: _make_provider()
    fake_manager._cached_session.return_value = None
    monkeypatch.setattr(
        cm_mod.ComputerManager,
        "get_instance",
        classmethod(lambda cls: fake_manager),
    )

    scrubber = scrub_mod.SandboxFeishuVaultScrubber.get_instance()

    # Pass 1: concurrent write changes digest -> compare-and-replace aborts with VaultScrubConflictError.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        cleaned_p1 = await scrubber.sweep_once()

    assert cleaned_p1 == 0
    assert json.loads(rt.files[vault_path].decode("utf-8")) == {
        "USER_TOKEN": "rotated-new-token"
    }
    assert list(rt.files.keys()) == [vault_path]  # no stray temp file left behind
    assert scrub_rows[cid]["status"] == "failed"
    assert scrub_rows[cid]["last_error"] == "VaultScrubConflictError"
    assert scrub_rows[cid]["attempt_count"] == 1
    assert _FAKE_FEISHU_SECRET not in caplog.text
    assert "rotated-new-token" not in caplog.text

    # Pass 2: rotated file has no Feishu keys -> marked cleaned without rewriting disk.
    uploads_before = len(rt.upload_log)
    cleaned_p2 = await scrubber.sweep_once()
    assert cleaned_p2 == 1
    assert len(rt.upload_log) == uploads_before
    assert json.loads(rt.files[vault_path].decode("utf-8")) == {
        "USER_TOKEN": "rotated-new-token"
    }
    assert scrub_rows[cid]["status"] == "cleaned"
    assert scrub_rows[cid]["attempt_count"] == 2
    assert scrub_rows[cid]["last_error"] is None


def test_compare_and_replace_shell_command_real_posix_sh(tmp_path: Path) -> None:
    """Execute _build_compare_and_replace_cmd under real /bin/sh for match, mismatch, and removal."""
    vault_file = tmp_path / ".vault_secrets.json"
    tmp_file = tmp_path / ".vault_secrets.json.scrub.tmp"

    orig_bytes = b'{"DALTON_FEISHU_APP_SECRET":"x","USER_TOKEN":"old"}'
    new_bytes = b'{"USER_TOKEN":"old"}'
    rotated_bytes = b'{"USER_TOKEN":"rotated"}'
    orig_sha = hashlib.sha256(orig_bytes).hexdigest()

    # 1. Mismatch preserves rotated file, deletes tmp file, exits with conflict code 42.
    vault_file.write_bytes(rotated_bytes)
    tmp_file.write_bytes(new_bytes)
    cmd_mismatch = scrub_mod._build_compare_and_replace_cmd(
        vault_path=str(vault_file),
        expected_sha256=orig_sha,
        tmp_path=str(tmp_file),
    )
    res_mismatch = subprocess.run(["/bin/sh", "-c", cmd_mismatch], capture_output=True, text=True)
    assert res_mismatch.returncode == scrub_mod._VAULT_SCRUB_CONFLICT_EXIT_CODE
    assert vault_file.read_bytes() == rotated_bytes
    assert not tmp_file.exists()

    # 2. Match replaces vault_file with tmp_file and exits 0.
    vault_file.write_bytes(orig_bytes)
    tmp_file.write_bytes(new_bytes)
    cmd_match = scrub_mod._build_compare_and_replace_cmd(
        vault_path=str(vault_file),
        expected_sha256=orig_sha,
        tmp_path=str(tmp_file),
    )
    res_match = subprocess.run(["/bin/sh", "-c", cmd_match], capture_output=True, text=True)
    assert res_match.returncode == 0
    assert vault_file.read_bytes() == new_bytes
    assert not tmp_file.exists()

    # 3. Empty-result compare-and-remove deletes on match and preserves on mismatch.
    feishu_only = b'{"DALTON_FEISHU_APP_SECRET":"x"}'
    feishu_sha = hashlib.sha256(feishu_only).hexdigest()
    vault_file.write_bytes(rotated_bytes)
    cmd_rm_mismatch = scrub_mod._build_compare_and_replace_cmd(
        vault_path=str(vault_file),
        expected_sha256=feishu_sha,
        tmp_path=None,
    )
    res_rm_mismatch = subprocess.run(
        ["/bin/sh", "-c", cmd_rm_mismatch], capture_output=True, text=True
    )
    assert res_rm_mismatch.returncode == scrub_mod._VAULT_SCRUB_CONFLICT_EXIT_CODE
    assert vault_file.read_bytes() == rotated_bytes

    vault_file.write_bytes(feishu_only)
    res_rm_match = subprocess.run(
        ["/bin/sh", "-c", cmd_rm_mismatch], capture_output=True, text=True
    )
    assert res_rm_match.returncode == 0
    assert not vault_file.exists()


# ---------------------------------------------------------------------------
# 10. r2: Static guard ensuring shipped configs never reference server-only names
# ---------------------------------------------------------------------------


def test_shipped_agent_and_plugin_configs_never_reference_server_only_env_names() -> None:
    """Shipped agent and plugin MCP config files must not contain ${VAR} refs to SERVER_ONLY_CREDENTIAL_ENV_NAMES."""
    repo_root = Path(__file__).resolve().parents[3]
    glob_patterns = (
        "agent_config*.yaml",
        "agent_config*.yml",
        "config*.yaml",
        "config*.yml",
        "src/dalton/*.yaml",
        "src/dalton/*.yml",
        "plugins/**/*.json",
        "plugins/**/*.yaml",
        "plugins/**/*.yml",
    )
    config_files = sorted(
        {
            path
            for pattern in glob_patterns
            for path in repo_root.glob(pattern)
            if path.is_file()
        }
    )
    rel_names = {str(p.relative_to(repo_root)) for p in config_files}
    assert "agent_config.dalton.yaml" in rel_names
    assert "agent_config.core.yaml" in rel_names
    assert any(name.endswith("mcp.json") for name in rel_names)

    env_ref_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:?+-][^}]*)?\}")
    violations: list[tuple[str, str]] = []
    for path in config_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in env_ref_re.finditer(text):
            var_name = match.group(1)
            if var_name in dalton_settings.SERVER_ONLY_CREDENTIAL_ENV_NAMES:
                violations.append((str(path.relative_to(repo_root)), match.group(0)))

    assert violations == []
