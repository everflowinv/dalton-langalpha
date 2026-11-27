"""Unit tests for Batch 1 Step 2: 3-layer sandbox credential isolation & legacy Feishu Vault scrubber.

Covers §3.2 Defense 1 (target & ${VAR} source env checks), Defense 2 (high-entropy
substring scrubbing >= 16), Defense 3 (Vault sync stripping, SandboxFeishuVaultScrubber,
and _lifecycle.py ready-point cleanup), controlled backend/gateway reads staying intact,
and zero plaintext leakage into logs, exceptions, or scrub status rows.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
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
from src.dalton import settings as dalton_settings
from src.dalton.app.router import router as dalton_router
from src.dalton.deployment_credentials import sandbox_scrub as scrub_mod
from src.dalton.deployment_credentials import sandbox_scrub_store as scrub_store
from src.dalton.deployment_credentials import snapshot as dc_snapshot
from src.dalton.services import channel_vault_credentials as cvc_mod
from src.dalton.services import report_delivery as rd_svc
from src.server.database import user_vault_secrets as vault_db
from src.server.services.computer_manager._lifecycle import SessionLifecycleMixin
from src.server.services.computer_manager._mcp import McpSecretsMixin
from src.server.services.computer_manager._provisioning import ProvisioningMixin
from src.server.services.computer_manager._types import ComputerBinding
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
    ) -> None:
        self.id = runtime_id
        self.working_dir = working_dir
        self.files: dict[str, bytes] = dict(initial_files or {})
        self.exec_log: list[str] = []
        self.upload_log: list[tuple[str, bytes]] = []
        self.download_log: list[str] = []
        self._fail_first_exec = fail_first_exec
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
            return SimpleNamespace(sandbox=self._sb)

    sandbox_obj = SimpleNamespace(vault_secrets={})
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

    sandbox = SimpleNamespace(
        runtime=rt,
        layout=layout,
        _runtime_call=_runtime_call,
    )
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

    sandbox_obj = SimpleNamespace(
        runtime=rt,
        layout=layout,
        vault_secrets={},
        installs_browser=False,
        _tool_refresh_lock=asyncio.Lock(),
        _runtime_call=_runtime_call,
        sync_sandbox_assets=_fake_sync_sandbox_assets,
    )

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

    cached_sandbox_mixed = SimpleNamespace(
        sandbox_id="sb-mixed",
        vault_secrets={
            "USER_TOKEN": _FAKE_NORMAL_SECRET,
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
            "DALTON_FEISHU_APP_SECRET": _FAKE_FEISHU_SECRET,
        },
        _tool_refresh_lock=asyncio.Lock(),
    )
    cached_session_mixed = SimpleNamespace(sandbox=cached_sandbox_mixed)

    fake_manager = SimpleNamespace(
        _binding_from_computer=lambda ws_id, row: ComputerBinding(
            workspace_id=ws_id,
            computer_id=str(row["computer_id"]),
            kind=row["kind"],
            root_dir=row["root_dir"],
            provider_ref=row["provider_ref"],
        ),
        _provider_for=lambda _binding: _make_provider(),
        _cached_session=lambda cid: cached_session_mixed if cid == cid_mixed else None,
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

    # 2. Feishu-only file deleted via rm -f.
    assert vault_path not in runtimes["sb-only"].files
    assert any(cmd.startswith("rm -f ") for cmd in runtimes["sb-only"].exec_log)
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

    sandbox = SimpleNamespace(
        sandbox_id="sb-stopped-1",
        runtime=rt,
        layout=layout,
        vault_secrets={
            "USER_TOKEN": _FAKE_NORMAL_SECRET,
            "DALTON_FEISHU_APP_ID": _FAKE_FEISHU_ID,
        },
        _tool_refresh_lock=asyncio.Lock(),
        ensure_sandbox_ready=_ensure_ready,
    )
    session = SimpleNamespace(sandbox=sandbox, skills_signature="sig-same")
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

    # On scrub error during startup, startup still succeeds and status='failed' is recorded.
    failing_rt = _InMemoryRuntime(
        "sb-fail-start",
        initial_files={vault_path: legacy_bytes},
        fail_first_exec=TimeoutError(f"timeout {_FAKE_FEISHU_SECRET}"),
    )
    failing_sandbox = SimpleNamespace(
        sandbox_id="sb-fail-start",
        runtime=failing_rt,
        layout=layout,
        vault_secrets={},
        _tool_refresh_lock=asyncio.Lock(),
        ensure_sandbox_ready=_ensure_ready,
    )
    failing_session = SimpleNamespace(sandbox=failing_sandbox, skills_signature="sig-same")
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
    assert getattr(failing_session, "_dalton_feishu_vault_scrub_checked", None) is None
    assert _FAKE_FEISHU_SECRET not in caplog.text


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
