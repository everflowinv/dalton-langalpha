"""SYNC10 (#831): deployment kill switch for upstream's built-in browser.

Under ``APP_ENV=dalton`` (``config.dalton.yaml`` sets
``features.browser.enabled: false``) plus ``DALTON-HOOK(browser-kill-switch)``:
1. Feature flags resolve ``browser`` off even if a user previously opted in,
   omitting it from ``list_user_features`` (Settings) and ``get_skill_registry()``.
2. Sandbox upload helpers skip ``browser_runtime`` and ``la/bin/agent-browser``.
3. ``workspace_browser`` routes return unavailable without touching a sandbox,
   and ``browser_available`` returns ``False``.
4. ``browser_quality`` sync, ``ComputerManager._keep_browser_quality``, and
   ``POST /api/v1/computers/{id}/browser-quality`` are skipped / refused.
5. ``Dockerfile.sandbox`` preserves upstream default ``INSTALL_AGENT_BROWSER=1``
   while gating installation so Dalton builds can pass ``INSTALL_AGENT_BROWSER=0``,
   and ``DockerProvider._ensure_image`` passes ``INSTALL_AGENT_BROWSER=0`` under
   Dalton vs ``1`` without the overlay.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from httpx import ASGITransport, AsyncClient

from ptc_agent.agent.middleware.skills.registry import get_skill_registry
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox._shared import (
    _internal_package_files,
    _internal_package_uploads,
)
from ptc_agent.core.sandbox.browser_runtime import protocol
from ptc_agent.core.sandbox.providers import provider_class
from ptc_agent.core.sandbox.providers.docker import DockerProvider
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.sandbox.runtime import ExecResult, SandboxRuntime
from src.config.core import clear_config_cache
from src.config.features import (
    default_feature_enabled,
    effective_flags,
    is_feature_enabled_system,
    system_flag,
)
from src.server.app import computers as computers_module
from src.server.app import workspace_browser as workspace_browser_module
from src.server.database import computer as computer_db
from src.server.services import account as account_service
from src.server.services import browser_quality
from src.server.services import browser_view as browser_view_module
from src.server.services import data_token_sweep
from src.server.services import features as features_service
from src.server.services.account_contract import NO_ORG
from src.server.services.computer_manager import ComputerManager
from src.server.services.workspace_files import (
    acquire_sandbox,
    reached_workspace_on_computer,
    sandbox_if_running,
)
from tests.conftest import create_test_app

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_DIR = _REPO_ROOT / "src"
_DOCKERFILE = _REPO_ROOT / "Dockerfile.sandbox"
_USER_ID = "test-user-123"
_COMPUTER_ID = "comp-browser-off"
_WS_ID = "ws-browser-off"


@pytest.fixture()
def dalton_overlay(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "dalton")
    clear_config_cache()
    try:
        yield
    finally:
        clear_config_cache()


@pytest.fixture()
def upstream_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    clear_config_cache()
    try:
        yield
    finally:
        clear_config_cache()


def _computer(kind: str = "docker", quality: object = "sharp") -> dict:
    return {
        "computer_id": _COMPUTER_ID,
        "user_id": _USER_ID,
        "name": "primary",
        "kind": kind,
        "status": "running",
        "provider_ref": "ctr-1",
        "config": {browser_quality.CONFIG_KEY: quality, browser_quality.REVISION_KEY: "r1"},
        "is_primary": False,
        "is_always_on": False,
        "resource_tier": None,
        "root_dir": None,
        "origin_workspace_id": None,
        "host_unavailable_since": None,
        "created_at": None,
        "updated_at": None,
    }


def _sandbox_double():
    runtime = create_autospec(SandboxRuntime, instance=True)
    runtime.exec.return_value = ExecResult(stdout=json.dumps({"done": True}), stderr="", exit_code=0)
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.sandbox_id = "sb-dalton-off"
    sandbox.runtime = runtime
    sandbox.layout = SandboxLayout("/home/workspace")
    return sandbox, runtime


# -- 1. Feature flag, Settings listing & model skill --------------------------


def test_dalton_overlay_disables_browser_feature_and_skill(dalton_overlay):
    assert system_flag("browser").enabled is False
    assert is_feature_enabled_system("browser") is False
    assert default_feature_enabled("browser") is False
    # Kill switch beats a previously stored opt-in override.
    assert effective_flags({"browser": True})["browser"] is False
    assert "browser" not in get_skill_registry()


@pytest.mark.asyncio
async def test_dalton_overlay_omits_browser_from_user_feature_listing(
    dalton_overlay, monkeypatch: pytest.MonkeyPatch
):
    get_prefs = create_autospec(
        features_service.get_user_preferences,
        return_value={"user_id": _USER_ID, "other_preference": {"feature_overrides": {"browser": True}}},
    )
    account_for = create_autospec(
        account_service.account_for,
        return_value=NO_ORG,
    )
    monkeypatch.setattr(features_service, "get_user_preferences", get_prefs)
    monkeypatch.setattr(features_service.accounts, "account_for", account_for)

    listed = await features_service.list_user_features(_USER_ID)
    assert all(item.key != "browser" for item in listed)
    assert await features_service.user_feature_enabled(_USER_ID, "browser") is False


def test_upstream_default_keeps_browser_enabled(upstream_defaults):
    assert system_flag("browser").enabled is True
    assert is_feature_enabled_system("browser") is True


# -- 2. Sandbox wrapper & runtime upload gate ---------------------------------


def test_dalton_overlay_skips_uploading_browser_runtime_and_wrapper(dalton_overlay):
    docker_cls = provider_class("docker")
    uploads = _internal_package_uploads(_SRC_DIR, docker_cls)
    files = _internal_package_files(_SRC_DIR, docker_cls)

    upload_rels = {str(rel) for _, rel, _ in uploads}
    file_rels = {str(rel) for _, rel in files}
    assert upload_rels == file_rels
    assert not any(rel.startswith(f"{protocol.PACKAGE_NAME}/") for rel in upload_rels)
    assert not any(rel.endswith("agent-browser") for rel in upload_rels)


def test_upstream_default_still_ships_browser_runtime_and_wrapper(upstream_defaults):
    docker_cls = provider_class("docker")
    file_rels = {str(rel) for _, rel in _internal_package_files(_SRC_DIR, docker_cls)}
    assert f"{protocol.PACKAGE_NAME}/protocol.py" in file_rels
    assert any(rel.endswith("agent-browser") for rel in file_rels)


# -- 3. Backend browser-* endpoints & browser_available -----------------------


@pytest.mark.asyncio
async def test_browser_routes_return_unavailable_under_dalton_overlay(
    dalton_overlay, monkeypatch: pytest.MonkeyPatch
):
    reach = create_autospec(
        reached_workspace_on_computer,
        return_value=({"workspace_id": _WS_ID, "user_id": _USER_ID}, "owner"),
    )
    acquire = create_autospec(acquire_sandbox)
    if_running = create_autospec(sandbox_if_running)
    get_prefs = create_autospec(
        features_service.get_user_preferences,
        return_value={"user_id": _USER_ID, "other_preference": {"feature_overrides": {"browser": True}}},
    )
    account_for = create_autospec(
        account_service.account_for,
        return_value=NO_ORG,
    )
    get_comp_ws = create_autospec(
        browser_view_module.get_computer_for_workspace,
        return_value={"kind": "docker"},
    )

    monkeypatch.setattr(workspace_browser_module, "reached_workspace_on_computer", reach)
    monkeypatch.setattr(workspace_browser_module, "acquire_sandbox", acquire)
    monkeypatch.setattr(workspace_browser_module, "sandbox_if_running", if_running)
    monkeypatch.setattr(features_service, "get_user_preferences", get_prefs)
    monkeypatch.setattr(features_service.accounts, "account_for", account_for)
    monkeypatch.setattr(browser_view_module, "get_computer_for_workspace", get_comp_ws)

    assert await browser_view_module.browser_available(_WS_ID) is False

    app = create_test_app(workspace_browser_module.router)
    base = f"/api/v1/workspaces/{_WS_ID}/sandbox"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        view_no_start = await client.post(f"{base}/browser-view", json={"start": False})
        view_start = await client.post(f"{base}/browser-view", json={"start": True})
        control = await client.post(
            f"{base}/browser-control", json={"holder": "view-1", "action": "take"}
        )
        navigate = await client.post(
            f"{base}/browser-navigate", json={"holder": "view-1", "action": "back"}
        )

    assert view_no_start.status_code == 200
    assert view_no_start.json() == {"state": "unavailable", "url": None, "expires_in": None}
    assert view_start.status_code == 200
    assert view_start.json() == {"state": "unavailable", "url": None, "expires_in": None}
    assert control.status_code == 200
    assert control.json() == {"held": False}
    assert navigate.status_code == 200
    assert navigate.json() == {"done": False, "reason": "not_running"}
    acquire.assert_not_awaited()
    if_running.assert_not_awaited()


# -- 4. Quality sync & quality endpoint skipped -------------------------------


@pytest.mark.asyncio
async def test_browser_quality_sync_and_endpoint_skipped_under_dalton_overlay(
    dalton_overlay, monkeypatch: pytest.MonkeyPatch
):
    sandbox, runtime = _sandbox_double()
    await browser_quality.keep(sandbox, kind="docker", quality="sharp", revision="r1")
    runtime.exec.assert_not_awaited()

    get_comp = create_autospec(computer_db.get_computer, return_value=_computer())
    running_sb = create_autospec(data_token_sweep.running_sandbox)
    monkeypatch.setattr(computer_db, "get_computer", get_comp)
    monkeypatch.setattr(data_token_sweep, "running_sandbox", running_sb)

    await browser_quality.apply_now(_computer())
    assert await browser_quality._apply(_computer()) is False
    get_comp.assert_not_awaited()
    running_sb.assert_not_called()

    keep_stub = create_autospec(browser_quality.keep)
    monkeypatch.setattr(browser_quality, "keep", keep_stub)
    binding = ComputerManager._binding_from_computer(_WS_ID, _computer())
    await ComputerManager._keep_browser_quality(binding, sandbox)
    keep_stub.assert_not_awaited()

    comp = _computer(kind="daytona")
    monkeypatch.setattr(
        computers_module,
        "get_computer",
        create_autospec(computers_module.get_computer, return_value=comp),
    )
    write_settings = create_autospec(computers_module.set_computer_settings, return_value=True)
    apply_now_stub = create_autospec(browser_quality.apply_now)
    monkeypatch.setattr(computers_module, "set_computer_settings", write_settings)
    monkeypatch.setattr(browser_quality, "apply_now", apply_now_stub)

    app = create_test_app(computers_module.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/computers/{_COMPUTER_ID}/browser-quality",
            json={"quality": "light"},
        )

    assert resp.status_code == 409
    write_settings.assert_not_awaited()
    apply_now_stub.assert_not_awaited()


# -- 5. Sandbox Dockerfile build arg & DockerProvider._ensure_image -----------


def test_dockerfile_keeps_upstream_default_and_gates_agent_browser_arg():
    text = _DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(r"^ARG\s+INSTALL_AGENT_BROWSER=(\S+)", text, re.MULTILINE)
    assert m is not None and m.group(1) == "1"
    assert 'if [ "${INSTALL_AGENT_BROWSER}" = "1" ]; then' in text


@pytest.mark.parametrize(
    ("app_env", "expected_arg"),
    [("dalton", "0"), (None, "1")],
    ids=["dalton-off", "upstream-on"],
)
@pytest.mark.asyncio
async def test_docker_provider_ensure_image_passes_install_browser_buildarg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_env: str | None, expected_arg: str
):
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    clear_config_cache()
    try:
        # Resolve config.yaml + config.dalton.yaml from repo root before chdir(tmp_path).
        assert is_feature_enabled_system("browser") is (expected_arg == "1")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "Dockerfile.sandbox").write_text("FROM scratch\n", encoding="utf-8")

        import aiodocker
        import aiodocker.images
        from ptc_agent.config.core import DockerConfig

        provider = DockerProvider(
            DockerConfig(image="test-sandbox:latest"), working_dir="/home/workspace"
        )
        mock_images = create_autospec(aiodocker.images.DockerImages, instance=True)
        mock_images.inspect.side_effect = Exception("missing image")

        captured: dict = {}

        async def _build_iter(**kwargs):
            captured.update(kwargs)
            yield {"stream": "ok"}

        mock_images.build = _build_iter
        mock_client = create_autospec(aiodocker.Docker, instance=True)
        mock_client.images = mock_images

        await provider._ensure_image(mock_client)
        assert captured["buildargs"] == {"INSTALL_AGENT_BROWSER": expected_arg}
    finally:
        clear_config_cache()
