"""Unit tests for the `dalton_monitor` agent tool and its prompt integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException

from ptc_agent.agent.prompts import guidance_template_vars, init_loader
from ptc_agent.core.sandbox.runtime import ExecResult, SandboxRuntime
from src.dalton import tools as dalton_tools_pkg
from src.dalton.monitors import db as monitor_db, service as monitor_service
from src.dalton.monitors.models import (
    MonitorRecord,
    PublishMonitorRequest,
    RepublishMonitorRequest,
    UpdateMonitorRequest,
)
from src.dalton.settings import DaltonConfig
from src.dalton.tools import build_dalton_tools, monitor_tools
from src.dalton.tools.monitor_tools import (
    DALTON_DISABLED_MESSAGE,
    EXTERNAL_SCOPE_FORBIDDEN_MESSAGE,
    PUBLIC_LINK_FORBIDDEN_MESSAGE,
    PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE,
    PUBLISHING_DISABLED_MESSAGE,
    SCOPE_CONFIRMATION_REQUIRED_MESSAGE,
    build_dalton_monitor_tool,
)
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts import Artifact, VersionFile
from src.server.database.artifacts.versions import PublishResult
from src.server.services.artifacts import snapshot as artifact_snapshot
from src.server.services.artifacts.manifest import MAX_BYTES, MAX_FILES, ManifestEntry
from src.server.services.persistence import file as persistence_file
from src.server.services.persistence.file import FilePersistenceService

SWITCH_ENV = "DALTON_ARTIFACT_PUBLISHING_ENABLED"
WS_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "user-internal-1"
THREAD_ID = "thread-monitor-1"
TIMEOUT_S = 5.0

_ORIG_OWNED_WS = monitor_tools.owned_workspace_on_computer
_ORIG_GET_FILE_TREE = persistence_file.get_file_tree
_ORIG_LIST_SANDBOX_FILES = FilePersistenceService.list_sandbox_files
_ORIG_ENSURE_ARTIFACT = artifacts_db.ensure_artifact
_ORIG_GET_ARTIFACT = artifacts_db.get_artifact
_ORIG_DB_GET_WORKSPACE = monitor_tools.db_get_workspace
_ORIG_SNAPSHOT_STATUS = artifact_snapshot.status
_ORIG_GET_MONITOR_BY_ARTIFACT = monitor_service.get_monitor_by_artifact


async def _invoke(
    tool: Any, args: dict[str, Any], *, config: dict[str, Any]
) -> dict[str, Any]:
    raw = await asyncio.wait_for(tool.ainvoke(args, config=config), timeout=TIMEOUT_S)
    return json.loads(raw)


def _run_config(
    *,
    user_id: str | None = USER_ID,
    workspace_id: str | None = WS_ID,
    thread_id: str | None = THREAD_ID,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {}
    if user_id is not None:
        configurable["user_id"] = user_id
    if workspace_id is not None:
        configurable["workspace_id"] = workspace_id
    if thread_id is not None:
        configurable["thread_id"] = thread_id
    return {"configurable": configurable}


def _detail_payload(
    *,
    slug: str = "portfolio-beta",
    title: str = "Portfolio Beta",
    visibility: str = "own",
    category: str = "portfolio",
    published_seq: int = 1,
    keep_updated: bool = False,
    cut: bool | None = True,
    member_user_ids: list[str] | None = None,
    owner_user_id: str = USER_ID,
    workspace_id: str = WS_ID,
    artifact_id: str = "22222222-2222-4222-8222-222222222222",
    entry_path: str = "dashboard.html",
    source_kind: str = "workspace",
    can_manage: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "monitor": {
            "monitor_id": "60000000-0000-4000-8000-000000000001",
            "slug": slug,
            "artifact_id": artifact_id,
            "workspace_id": workspace_id,
            "owner_user_id": owner_user_id,
            "entry_path": entry_path,
            "source_kind": source_kind,
            "title": title,
            "visibility": visibility,
            "category": category,
            "member_user_ids": list(member_user_ids or []),
            "published_seq": published_seq,
            "keep_updated": keep_updated,
            "can_manage": can_manage,
            "updated_at": "2026-11-17T08:00:00+00:00",
        },
        "versions": [
            {
                "seq": published_seq,
                "published_at": "2026-11-17T08:00:00+00:00",
            }
        ],
    }
    if cut is not None:
        payload["cut"] = cut
    return payload


def _make_artifact(
    *,
    artifact_id: str = "22222222-2222-4222-8222-222222222222",
    workspace_id: str = WS_ID,
    path: str = "dashboard.html",
    loaded_files: tuple[str, ...] = (),
    left_out_files: tuple[str, ...] = (),
) -> Artifact:
    return Artifact(kind="file", 
        artifact_id=artifact_id,
        code="abc123",
        workspace_id=workspace_id,
        path=path,
        created_thread_id=None,
        bound_thread_id=None,
        keep_updated=False,
        latest_seq=0,
        shared_at=None,
        files=None,
        revision=1,
        created_at=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),
        loaded_files=loaded_files,
        left_out_files=left_out_files,
    )


def _make_status(paths: list[str], *, contents: dict[str, bytes] | None = None) -> artifact_snapshot.Status:
    entries: list[ManifestEntry] = []
    files: list[VersionFile] = []
    for idx, p in enumerate(paths):
        data = (contents or {}).get(p, f"body:{p}".encode("utf-8"))
        sha = hashlib.sha256(data).hexdigest()
        entries.append(
            ManifestEntry(path=p, size=len(data), reason="entry" if idx == 0 else "loaded")
        )
        files.append(
            VersionFile(
                path=p,
                sha256=sha,
                byte_len=len(data),
                content_type="text/html" if p.endswith(".html") else "application/json",
                content=data,
            )
        )
    snap = artifact_snapshot.Snapshot(
        entries=entries,
        left_out=[],
        files=files,
        blobs={},
        title="Mock Monitor",
    )
    return artifact_snapshot.Status(snapshot=snap, latest_seq=0, changed=None, drift=None)


@pytest.fixture(autouse=True)
def _default_env_and_workspace_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH_ENV, "1")

    async def _owned_ws(workspace_id: str, user_id: str, *, need: str | None = None) -> dict[str, Any]:
        _ = need
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "status": "stopped",
            "dir_name": "ws-dir",
            "computer_root_dir": "/workspace",
        }

    async def _file_tree(workspace_id: str) -> list[dict[str, Any]]:
        _ = workspace_id
        return [
            {"path": "dashboards/beta/index.html", "size": 512},
            {"path": "dashboards/beta/data/a.json", "size": 128},
            {"path": "tracker.html", "size": 256},
            {"path": "dashboard.html", "size": 256},
        ]

    async def _ensure_artifact(workspace_id: str, path: str, **_kw: Any) -> Artifact:
        return _make_artifact(workspace_id=workspace_id, path=path)

    async def _get_artifact(artifact_id: str, *, user_id: str) -> Artifact:
        _ = user_id
        return _make_artifact(artifact_id=artifact_id)

    async def _db_get_ws(workspace_id: str) -> dict[str, Any]:
        return {"workspace_id": workspace_id, "user_id": USER_ID}

    async def _snapshot_status(
        artifact: Artifact,
        *,
        user_id: str,
        loaded: tuple[str, ...],
        earlier: tuple[str, ...] = (),
    ) -> artifact_snapshot.Status:
        _ = (user_id, earlier)
        paths = list(dict.fromkeys([artifact.path, *loaded]))
        return _make_status(paths)

    monkeypatch.setattr(
        monitor_tools,
        "owned_workspace_on_computer",
        create_autospec(_ORIG_OWNED_WS, side_effect=_owned_ws),
    )
    monkeypatch.setattr(
        FilePersistenceService,
        "get_file_tree",
        create_autospec(_ORIG_GET_FILE_TREE, side_effect=_file_tree),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(_ORIG_ENSURE_ARTIFACT, side_effect=_ensure_artifact),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_artifact",
        create_autospec(_ORIG_GET_ARTIFACT, side_effect=_get_artifact),
    )
    monkeypatch.setattr(
        monitor_tools,
        "db_get_workspace",
        create_autospec(_ORIG_DB_GET_WORKSPACE, side_effect=_db_get_ws),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "status",
        create_autospec(_ORIG_SNAPSHOT_STATUS, side_effect=_snapshot_status),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_by_artifact",
        create_autospec(_ORIG_GET_MONITOR_BY_ARTIFACT, return_value=None),
    )


# ---------------------------------------------------------------------------
# 1. Registration, Feature Switch & Router Gate Parity (Items 6, 8)
# ---------------------------------------------------------------------------


def test_module_level_singleton_removed() -> None:
    assert not hasattr(monitor_tools, "dalton_monitor")


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
def test_switch_off_does_not_register_dalton_monitor(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SWITCH_ENV, "0")
    names = {t.name for t in build_dalton_tools(role)}
    assert "dalton_monitor" not in names


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
def test_dalton_disabled_does_not_register_dalton_monitor_even_when_publishing_flag_on(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SWITCH_ENV, "1")
    disabled_cfg = DaltonConfig(enabled=False)
    monkeypatch.setattr(
        dalton_tools_pkg,
        "get_dalton_settings",
        create_autospec(dalton_tools_pkg.get_dalton_settings, return_value=disabled_cfg),
    )
    monkeypatch.setattr(
        monitor_tools,
        "get_dalton_settings",
        create_autospec(monitor_tools.get_dalton_settings, return_value=disabled_cfg),
    )

    names = {t.name for t in build_dalton_tools(role)}
    assert "dalton_monitor" not in names


@pytest.mark.asyncio
async def test_dalton_disabled_direct_call_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled_cfg = DaltonConfig(enabled=False)
    monkeypatch.setattr(
        monitor_tools,
        "get_dalton_settings",
        create_autospec(monitor_tools.get_dalton_settings, return_value=disabled_cfg),
    )
    publish_spy = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_spy)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool, {"action": "publish", "path": "dashboard.html"}, config=_run_config()
    )
    assert res["ok"] is False
    assert res["error"] == DALTON_DISABLED_MESSAGE
    publish_spy.assert_not_called()


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
@pytest.mark.parametrize("tier", ["internal", "admin", "external"])
def test_switch_on_registers_dalton_monitor_for_both_roles_and_tiers(
    role: str, tier: str
) -> None:
    names = {t.name for t in build_dalton_tools(role, user_tier=tier)}
    assert "dalton_monitor" in names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"action": "publish", "path": "dashboard.html"},
        {"action": "update", "slug": "beta-board", "title": "New"},
        {"action": "republish", "slug": "beta-board"},
        {"action": "list"},
    ],
)
async def test_switch_off_direct_call_returns_disabled_message_for_all_actions(
    args: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SWITCH_ENV, "0")
    publish_spy = create_autospec(monitor_service.publish_workspace_monitor)
    update_spy = create_autospec(monitor_service.update_monitor_settings)
    republish_spy = create_autospec(monitor_service.republish_workspace_monitor)
    list_spy = create_autospec(monitor_service.list_monitors_for_user)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_spy)
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_spy)
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_spy)
    monkeypatch.setattr(monitor_service, "list_monitors_for_user", list_spy)

    tool = build_dalton_monitor_tool()
    res = await _invoke(tool, args, config=_run_config())
    assert res["ok"] is False
    assert res["error"] == PUBLISHING_DISABLED_MESSAGE
    publish_spy.assert_not_called()
    update_spy.assert_not_called()
    republish_spy.assert_not_called()
    list_spy.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Docstring Contract (Item 5)
# ---------------------------------------------------------------------------


def test_tool_docstring_meets_direct_tool_contract() -> None:
    tool = build_dalton_monitor_tool()
    desc = tool.description
    assert len(desc) <= 600
    assert "`publish`=发布新面板或用文件夹重新打包" in desc
    assert "`republish`=用工作区当前文件出新版本" in desc
    assert "`update`=只改设置" in desc
    assert "`list`=列出可管理面板" in desc
    assert "文件夹发布会带上文件夹内所有可被页面加载的非隐藏文件" in desc
    assert "页面运行时按变量拼路径读取数据文件时，把页面和数据放进一个文件夹再发布该文件夹" in desc
    assert "publish_report" not in desc
    returns_part = desc.split("Returns:", 1)[1].strip()
    assert "\n" not in returns_part
    assert "candidates" in returns_part
    assert "files_changed" in returns_part


# ---------------------------------------------------------------------------
# 3. Default `own`, Folder vs Single HTML, `page_sha256`, Home resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_folder_collects_non_hidden_files_and_page_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _folder_tree(workspace_id: str) -> list[dict[str, Any]]:
        _ = workspace_id
        return [
            {"path": "dashboards/beta/index.html", "size": 500},
            {"path": "dashboards/beta/data/q1.json", "size": 200},
            {"path": "dashboards/beta/data/q2.json", "size": 300},
            {"path": "dashboards/beta/.DS_Store", "size": 64},
            {"path": "dashboards/beta/.hidden/secret.json", "size": 100},
            {"path": "dashboards/other/ignore.json", "size": 100},
        ]

    monkeypatch.setattr(
        FilePersistenceService,
        "get_file_tree",
        create_autospec(_ORIG_GET_FILE_TREE, side_effect=_folder_tree),
    )

    expected_paths = [
        "dashboards/beta/index.html",
        "dashboards/beta/data/q1.json",
        "dashboards/beta/data/q2.json",
    ]
    expected_status = _make_status(expected_paths)
    assert expected_status.snapshot is not None
    expected_sha = expected_status.snapshot.page_sha256

    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(
            slug="beta-board",
            title="Beta Board",
            visibility="own",
            published_seq=1,
        ),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/beta", "title": "Beta Board"},
        config=_run_config(),
    )
    assert res["ok"] is True
    assert res["url"] == "/monitors/beta-board"
    assert res["version"] == 1

    req = publish_mock.await_args.args[0]
    assert isinstance(req, PublishMonitorRequest)
    assert req.files == expected_paths
    assert req.loaded == ["dashboards/beta/data/q1.json", "dashboards/beta/data/q2.json"]
    assert req.page_sha256 == expected_sha
    assert req.visibility == "own"


@pytest.mark.asyncio
async def test_publish_running_folder_uses_sandbox_files_and_rejects_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _running_ws(workspace_id: str, user_id: str, *, need: str | None = None) -> dict[str, Any]:
        _ = need
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "status": "running",
            "dir_name": "ws-dir",
            "computer_root_dir": "/workspace",
        }

    entries = [["dashboards/big/index.html", 100]]
    for i in range(MAX_FILES):
        entries.append([f"dashboards/big/data/f_{i:03d}.json", 10])

    runtime_mock = create_autospec(SandboxRuntime, instance=True)
    runtime_mock.exec.return_value = ExecResult(
        stdout=json.dumps({"is_dir": True, "files": entries}),
        stderr="",
        exit_code=0,
    )

    class _FakeSandbox:
        runtime = runtime_mock

    class _Ctx:
        async def __aenter__(self) -> tuple[object, dict[str, Any]]:
            return _FakeSandbox(), {
                "workspace_id": WS_ID,
                "user_id": USER_ID,
                "dir_name": "ws-dir",
                "computer_root_dir": "/workspace",
            }

        async def __aexit__(self, *args: Any) -> None:
            return None

    list_sandbox_spy = create_autospec(_ORIG_LIST_SANDBOX_FILES)
    monkeypatch.setattr(
        monitor_tools,
        "owned_workspace_on_computer",
        create_autospec(_ORIG_OWNED_WS, side_effect=_running_ws),
    )
    monkeypatch.setattr(
        monitor_tools,
        "acquire_sandbox_to_change",
        create_autospec(monitor_tools.acquire_sandbox_to_change, return_value=_Ctx()),
    )
    monkeypatch.setattr(
        FilePersistenceService,
        "list_sandbox_files",
        list_sandbox_spy,
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/big"},
        config=_run_config(),
    )
    assert res["ok"] is False
    assert f"最多 {MAX_FILES} 个" in res["error"]
    assert "改发入口 HTML 文件（只带页面实际引用的文件），或把页面需要的文件单独放进一个文件夹" in res["error"]
    list_sandbox_spy.assert_not_called()
    publish_mock.assert_not_called()


@pytest.mark.asyncio
async def test_publish_folder_over_byte_cap_returns_explicit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _big_tree(workspace_id: str) -> list[dict[str, Any]]:
        _ = workspace_id
        return [
            {"path": "dashboards/heavy/index.html", "size": 100},
            {"path": "dashboards/heavy/data/huge.json", "size": MAX_BYTES},
        ]

    monkeypatch.setattr(
        FilePersistenceService,
        "get_file_tree",
        create_autospec(_ORIG_GET_FILE_TREE, side_effect=_big_tree),
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/heavy"},
        config=_run_config(),
    )
    assert res["ok"] is False
    assert f"最多 {MAX_BYTES} 字节" in res["error"]
    assert "改发入口 HTML 文件（只带页面实际引用的文件），或把页面需要的文件单独放进一个文件夹" in res["error"]
    publish_mock.assert_not_called()


@pytest.mark.asyncio
async def test_publish_single_html_keeps_static_scan_and_sends_page_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    tree_spy = create_autospec(_ORIG_GET_FILE_TREE)
    monkeypatch.setattr(FilePersistenceService, "get_file_tree", tree_spy)

    expected_status = _make_status(["tracker.html"])
    assert expected_status.snapshot is not None
    expected_sha = expected_status.snapshot.page_sha256

    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(slug="tracker", title="Tracker"),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "tracker.html"},
        config=_run_config(),
    )
    assert res["ok"] is True
    tree_spy.assert_not_called()
    req = publish_mock.await_args.args[0]
    assert req.files == ["tracker.html"]
    assert req.loaded == []
    assert req.page_sha256 == expected_sha


@pytest.mark.asyncio
async def test_chief_of_staff_resolves_home_workspace_when_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    resolve_home = create_autospec(
        monitor_tools.resolve_caller_home_workspace_id,
        return_value="ws-home-resolved",
    )
    monkeypatch.setattr(monitor_tools, "resolve_caller_home_workspace_id", resolve_home)
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(slug="home-tracker", title="Home Tracker"),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool(allow_home_workspace=True)
    res = await _invoke(
        tool,
        {"action": "publish", "path": "tracker.html"},
        config=_run_config(workspace_id="home"),
    )
    assert res["ok"] is True
    assert res["url"] == "/monitors/home-tracker"
    resolve_home.assert_awaited_once_with(USER_ID)
    req = publish_mock.await_args.args[0]
    assert req.workspace_id == "ws-home-resolved"


@pytest.mark.asyncio
async def test_chief_of_staff_real_workspace_uuid_skips_home_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    resolve_home = create_autospec(monitor_tools.resolve_caller_home_workspace_id)
    monkeypatch.setattr(monitor_tools, "resolve_caller_home_workspace_id", resolve_home)
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(slug="ws-tracker", title="WS Tracker"),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool(allow_home_workspace=True)
    res = await _invoke(
        tool,
        {"action": "publish", "path": "tracker.html"},
        config=_run_config(workspace_id=WS_ID),
    )
    assert res["ok"] is True
    resolve_home.assert_not_called()
    assert publish_mock.await_args.args[0].workspace_id == WS_ID


@pytest.mark.asyncio
async def test_analyst_passing_home_workspace_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_home = create_autospec(monitor_tools.resolve_caller_home_workspace_id)
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_tools, "resolve_caller_home_workspace_id", resolve_home)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool(allow_home_workspace=False)
    res = await _invoke(
        tool,
        {"action": "publish", "path": "tracker.html"},
        config=_run_config(workspace_id="home"),
    )
    assert res["ok"] is False
    assert "workspace_id" in res["error"]
    resolve_home.assert_not_called()
    publish_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Visibility Rules, Republish Public Block, External Guards (Items 1, 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["members", "company"])
async def test_publish_rejects_members_and_company_without_user_confirmation(
    visibility: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    args: dict[str, Any] = {
        "action": "publish",
        "path": "dashboard.html",
        "visibility": visibility,
    }
    if visibility == "members":
        args["member_names_or_emails"] = ["alice@co.com"]

    res = await _invoke(tool, args, config=_run_config())
    assert res["ok"] is False
    assert res["error"] == SCOPE_CONFIRMATION_REQUIRED_MESSAGE
    publish_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["members", "company"])
async def test_update_rejects_members_and_company_without_user_confirmation(
    visibility: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    update_mock = create_autospec(monitor_service.update_monitor_settings)
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    args: dict[str, Any] = {
        "action": "update",
        "slug": "beta-board",
        "visibility": visibility,
    }
    if visibility == "members":
        args["member_names_or_emails"] = ["alice@co.com"]

    res = await _invoke(tool, args, config=_run_config())
    assert res["ok"] is False
    assert res["error"] == SCOPE_CONFIRMATION_REQUIRED_MESSAGE
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_narrowing_to_own_requires_no_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(slug="beta-board", visibility="company"),
        ),
    )
    update_mock = create_autospec(
        monitor_service.update_monitor_settings,
        return_value=_detail_payload(slug="beta-board", visibility="own", cut=None),
    )
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {
            "action": "update",
            "slug": "beta-board",
            "visibility": "own",
            "user_confirmed_scope": False,
        },
        config=_run_config(),
    )
    assert res["ok"] is True
    assert res["visibility"] == "own"
    req = update_mock.await_args.args[1]
    assert req.visibility == "own"
    assert req.member_user_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["publish", "update"])
async def test_public_visibility_is_always_rejected_even_for_admin(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="admin"),
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    update_mock = create_autospec(monitor_service.update_monitor_settings)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    args: dict[str, Any] = {
        "action": action,
        "visibility": "public",
        "user_confirmed_scope": True,
    }
    if action == "publish":
        args["path"] = "dashboard.html"
    else:
        args["slug"] = "beta-board"

    res = await _invoke(tool, args, config=_run_config(user_id="admin-1"))
    assert res["ok"] is False
    assert res["error"] == PUBLIC_LINK_FORBIDDEN_MESSAGE
    publish_mock.assert_not_called()
    update_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["internal", "admin"])
async def test_republish_rejects_public_monitor_even_for_admin(
    tier: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value=tier),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(slug="pub-board", visibility="public"),
        ),
    )
    republish_mock = create_autospec(monitor_service.republish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "republish", "slug": "pub-board"},
        config=_run_config(user_id="admin-1"),
    )
    assert res["ok"] is False
    assert res["error"] == PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE
    republish_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["company", "members"])
@pytest.mark.parametrize("action", ["republish", "update"])
async def test_external_member_cannot_republish_or_update_non_own_monitor(
    action: str, visibility: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="external"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(
                slug="shared-board",
                visibility=visibility,
                owner_user_id="ext-1",
            ),
        ),
    )
    republish_mock = create_autospec(monitor_service.republish_workspace_monitor)
    update_mock = create_autospec(monitor_service.update_monitor_settings)
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_mock)
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    args: dict[str, Any] = {"action": action, "slug": "shared-board"}
    if action == "update":
        args["title"] = "Updated Title"

    res = await _invoke(tool, args, config=_run_config(user_id="ext-1"))
    assert res["ok"] is False
    assert res["error"] == EXTERNAL_SCOPE_FORBIDDEN_MESSAGE
    republish_mock.assert_not_called()
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_external_member_can_publish_and_republish_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="external"),
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(slug="ext-board", visibility="own"),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(
                slug="ext-board", visibility="own", owner_user_id="ext-1"
            ),
        ),
    )
    republish_mock = create_autospec(
        monitor_service.republish_workspace_monitor,
        return_value=_detail_payload(slug="ext-board", visibility="own", published_seq=2),
    )
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_mock)

    tool = build_dalton_monitor_tool()
    pub_res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboard.html"},
        config=_run_config(user_id="ext-1"),
    )
    assert pub_res["ok"] is True
    assert pub_res["visibility"] == "own"
    publish_mock.assert_awaited_once()

    rep_res = await _invoke(
        tool,
        {"action": "republish", "slug": "ext-board"},
        config=_run_config(user_id="ext-1"),
    )
    assert rep_res["ok"] is True
    assert rep_res["version"] == 2
    republish_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Member Resolution & `update` to `members` without list (Item 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_member_resolution_duplicate_name_returns_candidates_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    members_mock = create_autospec(
        monitor_service.list_company_members_for_user,
        return_value=[
            {"user_id": "u-zs-1", "name": "张三", "email": "zhangsan1@co.com", "tier": "internal"},
            {"user_id": "u-zs-2", "name": "张三", "email": "zhangsan2@co.com", "tier": "admin"},
            {"user_id": "u-ls-1", "name": "李四", "email": "lisi@co.com", "tier": "internal"},
        ],
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "list_company_members_for_user", members_mock)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {
            "action": "publish",
            "path": "dashboard.html",
            "visibility": "members",
            "member_names_or_emails": ["张三"],
            "user_confirmed_scope": True,
        },
        config=_run_config(),
    )
    assert res["ok"] is False
    assert "重名" in res["error"]
    assert len(res["candidates"]) == 2
    assert {c["user_id"] for c in res["candidates"]} == {"u-zs-1", "u-zs-2"}
    publish_mock.assert_not_called()


@pytest.mark.asyncio
async def test_member_resolution_by_raw_user_id_email_and_unique_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    members_mock = create_autospec(
        monitor_service.list_company_members_for_user,
        return_value=[
            {"user_id": "u-zs-1", "name": "张三", "email": "zhangsan1@co.com", "tier": "internal"},
            {"user_id": "u-zs-2", "name": "张三", "email": "zhangsan2@co.com", "tier": "admin"},
            {"user_id": "u-ls-1", "name": "李四", "email": "lisi@co.com", "tier": "internal"},
        ],
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(
            slug="team-board",
            title="Team Board",
            visibility="members",
            published_seq=2,
        ),
    )
    monkeypatch.setattr(monitor_service, "list_company_members_for_user", members_mock)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {
            "action": "publish",
            "path": "dashboard.html",
            "title": "Team Board",
            "category": "stocks",
            "visibility": "members",
            "member_names_or_emails": ["u-zs-2", "李四"],
            "keep_updated": True,
            "user_confirmed_scope": True,
        },
        config=_run_config(),
    )
    assert res["ok"] is True
    assert res["visibility"] == "members"
    req = publish_mock.await_args.args[0]
    assert req.member_user_ids == ["u-zs-2", "u-ls-1"]


@pytest.mark.asyncio
async def test_member_resolution_unmatched_partial_returns_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(slug="beta-board", visibility="own"),
        ),
    )
    members_mock = create_autospec(
        monitor_service.list_company_members_for_user,
        return_value=[
            {"user_id": "u-alice", "name": "Alice Chen", "email": "alice@co.com", "tier": "internal"},
        ],
    )
    update_mock = create_autospec(monitor_service.update_monitor_settings)
    monkeypatch.setattr(monitor_service, "list_company_members_for_user", members_mock)
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {
            "action": "update",
            "slug": "beta-board",
            "visibility": "members",
            "member_names_or_emails": ["Alice"],
            "user_confirmed_scope": True,
        },
        config=_run_config(),
    )
    assert res["ok"] is False
    assert "候选" in res["error"]
    assert [c["user_id"] for c in res["candidates"]] == ["u-alice"]
    update_mock.assert_not_called()


@pytest.mark.asyncio
async def test_update_to_members_without_list_preserves_existing_when_already_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(
                slug="team-board",
                visibility="members",
                member_user_ids=["u-1", "u-2"],
            ),
        ),
    )
    update_mock = create_autospec(
        monitor_service.update_monitor_settings,
        return_value=_detail_payload(
            slug="team-board",
            title="Renamed Team Board",
            visibility="members",
            member_user_ids=["u-1", "u-2"],
            cut=None,
        ),
    )
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {
            "action": "update",
            "slug": "team-board",
            "title": "Renamed Team Board",
            "visibility": "members",
            "user_confirmed_scope": True,
        },
        config=_run_config(),
    )
    assert res["ok"] is True
    assert "沿用原名单（2 人）" in res["note"]
    req = update_mock.await_args.args[1]
    assert isinstance(req, UpdateMonitorRequest)
    assert req.visibility == "members"
    assert req.member_user_ids is None


@pytest.mark.asyncio
@pytest.mark.parametrize("current_visibility", ["own", "company"])
async def test_update_to_members_without_list_from_non_members_requires_list(
    current_visibility: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(slug="beta-board", visibility=current_visibility),
        ),
    )
    update_mock = create_autospec(monitor_service.update_monitor_settings)
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {
            "action": "update",
            "slug": "beta-board",
            "visibility": "members",
            "user_confirmed_scope": True,
        },
        config=_run_config(),
    )
    assert res["ok"] is False
    assert "member_names_or_emails" in res["error"]
    update_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Error Translation (`files_changed`, 403, unexpected exceptions) (Items 4, 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_path", ["drafts/report.md", "data/model.xlsx", "chart.png", "run.py"])
async def test_non_html_path_is_rejected(
    bad_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool, {"action": "publish", "path": bad_path}, config=_run_config()
    )
    assert res["ok"] is False
    assert "HTML" in res["error"]
    publish_mock.assert_not_called()


@pytest.mark.asyncio
async def test_files_changed_translates_to_wait_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        side_effect=HTTPException(status_code=409, detail={"code": "files_changed"}),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboard.html"},
        config=_run_config(),
    )
    assert res["ok"] is False
    assert "文件还在变化，等生成完成后再发布" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update", "republish"])
async def test_unmanageable_slug_403_is_translated(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(
                slug="other-board",
                visibility="company",
                owner_user_id="other-owner",
                can_manage=False,
            ),
        ),
    )
    update_mock = create_autospec(
        monitor_service.update_monitor_settings,
        side_effect=HTTPException(status_code=403, detail="只有面板所有者或管理员可修改设置"),
    )
    republish_mock = create_autospec(
        monitor_service.republish_workspace_monitor,
        side_effect=HTTPException(status_code=403, detail="无权重新发布此监控面板"),
    )
    monkeypatch.setattr(monitor_service, "update_monitor_settings", update_mock)
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_mock)

    tool = build_dalton_monitor_tool()
    args: dict[str, Any] = {"action": action, "slug": "other-board"}
    if action == "update":
        args["title"] = "Attempt"
    res = await _invoke(tool, args, config=_run_config())
    assert res["ok"] is False
    assert res["error"].startswith("权限不足：")


@pytest.mark.asyncio
async def test_unexpected_non_http_exception_returns_chinese_error_and_logs_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    list_mock = create_autospec(
        monitor_service.list_monitors_for_user,
        side_effect=RuntimeError("db connection lost"),
    )
    monkeypatch.setattr(monitor_service, "list_monitors_for_user", list_mock)

    tool = build_dalton_monitor_tool()
    with caplog.at_level(logging.ERROR, logger=monitor_tools.logger.name):
        res = await _invoke(tool, {"action": "list"}, config=_run_config())

    assert res["ok"] is False
    assert "监控面板操作失败" in res["error"]
    assert "db connection lost" in res["error"]
    assert any(r.exc_info is not None for r in caplog.records)


# ---------------------------------------------------------------------------
# 7. `republish` sends `page_sha256`, `update`, and `list`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_republish_passes_page_sha256_and_returns_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(
                slug="beta-board", visibility="own", entry_path="dashboard.html"
            ),
        ),
    )
    expected_status = _make_status(["dashboard.html"])
    assert expected_status.snapshot is not None
    expected_sha = expected_status.snapshot.page_sha256

    republish_mock = create_autospec(
        monitor_service.republish_workspace_monitor,
        return_value=_detail_payload(
            slug="beta-board",
            title="Beta Board",
            visibility="own",
            published_seq=4,
            cut=True,
        ),
    )
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool, {"action": "republish", "slug": "beta-board"}, config=_run_config()
    )
    assert res["ok"] is True
    assert res["action"] == "republish"
    assert res["url"] == "/monitors/beta-board"
    assert res["version"] == 4
    assert res["cut"] is True
    req = republish_mock.await_args.args[1]
    assert isinstance(req, RepublishMonitorRequest)
    assert req.page_sha256 == expected_sha


@pytest.mark.asyncio
async def test_list_returns_only_manageable_monitors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_mock = create_autospec(
        monitor_service.list_monitors_for_user,
        return_value=[
            {
                "slug": "my-panel",
                "title": "My Panel",
                "visibility": "own",
                "category": "portfolio",
                "published_seq": 2,
                "updated_at": "2026-11-17T07:00:00+00:00",
                "keep_updated": True,
                "can_manage": True,
            },
            {
                "slug": "colleague-company-panel",
                "title": "Colleague Panel",
                "visibility": "company",
                "category": "overview",
                "published_seq": 5,
                "updated_at": "2026-11-17T06:00:00+00:00",
                "keep_updated": False,
                "can_manage": False,
            },
        ],
    )
    monkeypatch.setattr(monitor_service, "list_monitors_for_user", list_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(tool, {"action": "list"}, config=_run_config())
    assert res["ok"] is True
    assert res["count"] == 1
    assert [m["slug"] for m in res["monitors"]] == ["my-panel"]


# ---------------------------------------------------------------------------
# 8. Prompt Integration (Item 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["analyst", "chief_of_staff"])
@pytest.mark.parametrize("guidance", ["lean", "detailed"])
def test_monitor_prompt_renders_for_both_roles_when_enabled(
    role: str, guidance: str
) -> None:
    loader = init_loader()
    base = dict(current_time="2026-11-17 07:00 ET", subagent_summary="")
    vars_ = guidance_template_vars(guidance)

    prompt_off = loader.get_system_prompt(**base, role=role, **vars_)
    assert "dalton_monitor" not in prompt_off

    prompt_on = loader.get_system_prompt(
        **base, role=role, dalton_monitors_enabled=True, **vars_
    )
    assert "dalton_monitor" in prompt_on
    assert "默认仅自己可见" in prompt_on
    assert "扩大可见范围前必须先询问用户" in prompt_on
    assert "技能或工作流规定的交付物与正式研报除外" in prompt_on


# ---------------------------------------------------------------------------
# 9. Round 3 Regression Tests (left_out, existing scope, page_may_load, dotted folder, list/file_missing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_folder_excludes_existing_left_out_files_and_reports_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _folder_tree(workspace_id: str) -> list[dict[str, Any]]:
        _ = workspace_id
        return [
            {"path": "dashboards/beta/index.html", "size": 500},
            {"path": "dashboards/beta/data/keep.json", "size": 200},
            {"path": "dashboards/beta/data/excluded.json", "size": 300},
        ]

    monkeypatch.setattr(
        FilePersistenceService,
        "get_file_tree",
        create_autospec(_ORIG_GET_FILE_TREE, side_effect=_folder_tree),
    )

    async def _ensure_existing(workspace_id: str, path: str, **_kw: Any) -> Artifact:
        return _make_artifact(
            workspace_id=workspace_id,
            path=path,
            left_out_files=(
                "dashboards/beta/data/excluded.json",
                "dashboards/beta/data/already-deleted.json",
            ),
        )

    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(_ORIG_ENSURE_ARTIFACT, side_effect=_ensure_existing),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_by_artifact",
        create_autospec(
            _ORIG_GET_MONITOR_BY_ARTIFACT,
            return_value=_detail_payload(
                slug="beta-board",
                visibility="own",
                entry_path="dashboards/beta/index.html",
            )["monitor"],
        ),
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(
            slug="beta-board",
            visibility="own",
            published_seq=2,
            entry_path="dashboards/beta/index.html",
        ),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/beta"},
        config=_run_config(),
    )
    assert res["ok"] is True
    assert res["left_out"] == 1
    req = publish_mock.await_args.args[0]
    assert req.files == ["dashboards/beta/index.html", "dashboards/beta/data/keep.json"]
    assert req.loaded == ["dashboards/beta/data/keep.json"]


@pytest.mark.asyncio
async def test_publish_rejects_existing_public_monitor_even_for_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_by_artifact",
        create_autospec(
            _ORIG_GET_MONITOR_BY_ARTIFACT,
            return_value=_detail_payload(slug="pub-board", visibility="public")["monitor"],
        ),
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboard.html"},
        config=_run_config(user_id="admin-1"),
    )
    assert res["ok"] is False
    assert res["error"] == PUBLIC_REPUBLISH_FORBIDDEN_MESSAGE
    publish_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_visibility", ["company", "members"])
async def test_publish_rejects_external_member_when_existing_monitor_is_non_own(
    existing_visibility: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="external"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_by_artifact",
        create_autospec(
            _ORIG_GET_MONITOR_BY_ARTIFACT,
            return_value=_detail_payload(
                slug="shared-board",
                visibility=existing_visibility,
                owner_user_id="ext-1",
            )["monitor"],
        ),
    )
    publish_mock = create_autospec(monitor_service.publish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboard.html"},
        config=_run_config(user_id="ext-1"),
    )
    assert res["ok"] is False
    assert res["error"] == EXTERNAL_SCOPE_FORBIDDEN_MESSAGE
    publish_mock.assert_not_called()


@pytest.mark.asyncio
async def test_publish_running_folder_uses_page_load_rules_not_backup_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_root = tmp_path / "ws-dir"
    folder = ws_root / "dashboards" / "beta"
    (folder / "vendor").mkdir(parents=True)
    (folder / "node_modules" / "pkg").mkdir(parents=True)
    (folder / "lib").mkdir(parents=True)
    (folder / ".hidden").mkdir(parents=True)
    (folder / "__pycache__").mkdir(parents=True)

    (folder / "index.html").write_text("<html></html>", encoding="utf-8")
    (folder / "vendor" / "chart.js").write_text("console.log(1);", encoding="utf-8")
    (folder / "node_modules" / "pkg" / "index.js").write_text("export {};", encoding="utf-8")
    (folder / "lib" / "native.so").write_bytes(b"\x00\x01")
    (folder / ".DS_Store").write_bytes(b"ds")
    (folder / ".hidden" / "secret.json").write_text("{}", encoding="utf-8")
    (folder / "__pycache__" / "mod.pyc").write_bytes(b"pyc")

    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _running_ws(
        workspace_id: str, user_id: str, *, need: str | None = None
    ) -> dict[str, Any]:
        _ = need
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "status": "running",
            "dir_name": "ws-dir",
            "computer_root_dir": str(tmp_path),
        }

    async def _exec_local(command: str, timeout: int = 30) -> ExecResult:
        _ = timeout
        assert command.startswith("python3 ")
        patched = f"{shlex.quote(sys.executable)}{command.removeprefix('python3')}"
        proc = subprocess.run(
            ["/bin/sh", "-c", patched],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        return ExecResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)

    runtime_mock = create_autospec(SandboxRuntime, instance=True)
    runtime_mock.exec.side_effect = _exec_local

    class _FakeSandbox:
        runtime = runtime_mock

    class _Ctx:
        async def __aenter__(self) -> tuple[object, dict[str, Any]]:
            return _FakeSandbox(), {
                "workspace_id": WS_ID,
                "user_id": USER_ID,
                "dir_name": "ws-dir",
                "computer_root_dir": str(tmp_path),
            }

        async def __aexit__(self, *args: Any) -> None:
            return None

    list_sandbox_spy = create_autospec(_ORIG_LIST_SANDBOX_FILES)
    monkeypatch.setattr(
        monitor_tools,
        "owned_workspace_on_computer",
        create_autospec(_ORIG_OWNED_WS, side_effect=_running_ws),
    )
    monkeypatch.setattr(
        monitor_tools,
        "acquire_sandbox_to_change",
        create_autospec(monitor_tools.acquire_sandbox_to_change, return_value=_Ctx()),
    )
    monkeypatch.setattr(
        FilePersistenceService,
        "list_sandbox_files",
        list_sandbox_spy,
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(
            slug="beta-board",
            entry_path="dashboards/beta/index.html",
        ),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/beta"},
        config=_run_config(),
    )
    assert res["ok"] is True
    list_sandbox_spy.assert_not_called()
    req = publish_mock.await_args.args[0]
    assert req.files == [
        "dashboards/beta/index.html",
        "dashboards/beta/lib/native.so",
        "dashboards/beta/node_modules/pkg/index.js",
        "dashboards/beta/vendor/chart.js",
    ]


@pytest.mark.asyncio
async def test_publish_dotted_folder_path_treated_as_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _dotted_tree(workspace_id: str) -> list[dict[str, Any]]:
        _ = workspace_id
        return [
            {"path": "reports/v1.0/index.html", "size": 512},
            {"path": "reports/v1.0/data/metrics.json", "size": 128},
        ]

    monkeypatch.setattr(
        FilePersistenceService,
        "get_file_tree",
        create_autospec(_ORIG_GET_FILE_TREE, side_effect=_dotted_tree),
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(
            slug="v1-report",
            entry_path="reports/v1.0/index.html",
        ),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "reports/v1.0"},
        config=_run_config(),
    )
    assert res["ok"] is True
    req = publish_mock.await_args.args[0]
    assert req.path == "reports/v1.0/index.html"
    assert req.files == ["reports/v1.0/index.html", "reports/v1.0/data/metrics.json"]
    assert req.loaded == ["reports/v1.0/data/metrics.json"]


@pytest.mark.asyncio
async def test_list_includes_source_kind_and_skips_data_job_monitors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_mock = create_autospec(
        monitor_service.list_monitors_for_user,
        return_value=[
            {
                "slug": "ws-panel",
                "title": "Workspace Panel",
                "visibility": "own",
                "category": "portfolio",
                "source_kind": "workspace",
                "published_seq": 3,
                "updated_at": "2026-11-17T07:00:00+00:00",
                "keep_updated": True,
                "can_manage": True,
            },
            {
                "slug": "job-panel",
                "title": "Data Job Panel",
                "visibility": "company",
                "category": "overview",
                "source_kind": "data_job",
                "published_seq": 10,
                "updated_at": "2026-11-17T07:05:00+00:00",
                "keep_updated": False,
                "can_manage": True,
            },
        ],
    )
    monkeypatch.setattr(monitor_service, "list_monitors_for_user", list_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(tool, {"action": "list"}, config=_run_config())
    assert res["ok"] is True
    assert res["count"] == 1
    assert res["monitors"][0]["slug"] == "ws-panel"
    assert res["monitors"][0]["source_kind"] == "workspace"


@pytest.mark.asyncio
async def test_republish_file_missing_omits_folder_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_detail",
        create_autospec(
            monitor_service.get_monitor_detail,
            return_value=_detail_payload(slug="beta-board", visibility="own"),
        ),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "status",
        create_autospec(
            _ORIG_SNAPSHOT_STATUS,
            side_effect=artifact_snapshot.SnapshotRefused("file_missing"),
        ),
    )
    republish_mock = create_autospec(monitor_service.republish_workspace_monitor)
    monkeypatch.setattr(monitor_service, "republish_workspace_monitor", republish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool, {"action": "republish", "slug": "beta-board"}, config=_run_config()
    )
    assert res["ok"] is False
    assert "文件夹" not in res["error"]
    assert "index.html" not in res["error"]
    assert "beta-board" in res["error"]
    republish_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 10. Round 4 Regression Tests (consecutive left_out persistence, symlink bypass)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consecutive_folder_publishes_preserve_left_out_and_exclude_file_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive folder publishes must keep excluded files in left_out_files and out of versions."""
    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    folder_contents = {
        "dashboards/beta/index.html": "<html><body><script>fetch('data/keep.json')</script></body></html>",
        "dashboards/beta/data/keep.json": '{"ok": true}',
        "dashboards/beta/data/excluded.json": '{"secret": "excluded"}',
    }

    async def _folder_tree(workspace_id: str) -> list[dict[str, Any]]:
        _ = workspace_id
        return [
            {"path": p, "size": len(body.encode("utf-8"))}
            for p, body in folder_contents.items()
        ]

    async def _file_content(workspace_id: str, file_path: str) -> dict[str, Any] | None:
        _ = workspace_id
        body = folder_contents.get(file_path)
        if body is None:
            return None
        raw = body.encode("utf-8")
        return {
            "file_path": file_path,
            "file_size": len(raw),
            "content_text": body,
            "content_binary": None,
            "mime_type": "text/html" if file_path.endswith(".html") else "application/json",
        }

    artifact_state: dict[str, Any] = {
        "artifact_id": "22222222-2222-4222-8222-222222222222",
        "workspace_id": WS_ID,
        "path": "dashboards/beta/index.html",
        "latest_seq": 0,
        "files": None,
        "loaded_files": (),
        "left_out_files": ("dashboards/beta/data/excluded.json",),
        "revision": 1,
    }
    monitor_record: MonitorRecord | None = None
    committed_versions: list[list[str]] = []
    version_hashes_by_seq: dict[int, dict[str, str]] = {}

    def _current_artifact() -> Artifact:
        return Artifact(kind="file", 
            artifact_id=artifact_state["artifact_id"],
            code="abc123",
            workspace_id=artifact_state["workspace_id"],
            path=artifact_state["path"],
            created_thread_id=None,
            bound_thread_id=None,
            keep_updated=False,
            latest_seq=artifact_state["latest_seq"],
            shared_at=None,
            files=artifact_state["files"],
            revision=artifact_state["revision"],
            created_at=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),
            loaded_files=artifact_state["loaded_files"],
            left_out_files=artifact_state["left_out_files"],
        )

    async def _ensure_artifact(workspace_id: str, path: str, **_kw: Any) -> Artifact:
        assert workspace_id == WS_ID
        assert path == "dashboards/beta/index.html"
        return _current_artifact()

    async def _get_version_hashes(artifact_id: str, seq: int) -> dict[str, str]:
        assert artifact_id == artifact_state["artifact_id"]
        return dict(version_hashes_by_seq.get(seq, {}))

    async def _stopped_ws(
        workspace_id: str, user_id: str, *, need: str | None = None
    ) -> dict[str, Any]:
        _ = need
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "status": "stopped",
            "dir_name": "ws-dir",
            "computer_root_dir": "/workspace",
        }

    async def _db_get_ws(workspace_id: str) -> dict[str, Any]:
        return {
            "workspace_id": workspace_id,
            "user_id": USER_ID,
            "status": "stopped",
            "dir_name": "ws-dir",
            "computer_root_dir": "/workspace",
        }

    async def _get_monitor_by_artifact_id(artifact_id: str) -> MonitorRecord | None:
        _ = artifact_id
        return monitor_record

    async def _get_monitor_by_artifact(
        artifact_id: str, *, viewer_id: str
    ) -> dict[str, Any] | None:
        _ = (artifact_id, viewer_id)
        if monitor_record is None:
            return None
        return monitor_service.serialize_monitor(monitor_record, can_manage=True)

    async def _commit_publish_tx(**kwargs: Any) -> tuple[MonitorRecord, PublishResult]:
        nonlocal monitor_record
        version_paths = [vf.path for vf in kwargs["files"]]
        committed_versions.append(version_paths)
        artifact_state["latest_seq"] += 1
        artifact_state["revision"] += 1
        artifact_state["files"] = tuple(version_paths)
        artifact_state["loaded_files"] = tuple(kwargs.get("loaded_files") or ())
        artifact_state["left_out_files"] = tuple(kwargs.get("left_out_files") or ())
        seq = artifact_state["latest_seq"]
        version_hashes_by_seq[seq] = {vf.path: vf.sha256 for vf in kwargs["files"]}
        now = datetime(2026, 11, 17, 8, 0, tzinfo=UTC)
        monitor_record = MonitorRecord(
            monitor_id="60000000-0000-4000-8000-000000000001",
            slug=kwargs["slug"],
            artifact_id=kwargs["artifact_id"],
            owner_user_id=kwargs["monitor_owner_id"],
            title=kwargs["title"],
            category=kwargs["category"],
            sort_order=kwargs["sort_order"],
            visibility=kwargs["visibility"],
            published_seq=seq,
            keep_updated=kwargs["keep_updated"],
            source_kind=kwargs["source_kind"],
            source_ref=kwargs["source_ref"],
            acl_revision=1,
            created_at=now,
            updated_at=now,
            workspace_id=WS_ID,
            entry_path=artifact_state["path"],
        )
        updated_art = _current_artifact()
        return monitor_record, PublishResult(
            artifact=updated_art,
            seq=seq,
            cut=True,
            shared_now=False,
            changed=True,
        )

    async def _build_detail(mon: MonitorRecord, *, viewer_id: str) -> dict[str, Any]:
        _ = viewer_id
        return _detail_payload(
            slug=mon.slug,
            title=mon.title,
            visibility=mon.visibility,
            published_seq=mon.published_seq,
            entry_path=mon.entry_path or "dashboards/beta/index.html",
        )

    async def _validate_perms(**_kw: Any) -> list[str]:
        return []

    async def _no_secrets(_uid: str) -> dict[str, str]:
        return {}

    async def _no_blobs(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(FilePersistenceService, "get_file_tree", _folder_tree)
    monkeypatch.setattr(FilePersistenceService, "get_file_content", _file_content)
    monkeypatch.setattr(artifacts_db, "ensure_artifact", _ensure_artifact)
    monkeypatch.setattr(artifacts_db, "get_version_hashes", _get_version_hashes)
    monkeypatch.setattr(monitor_tools, "owned_workspace_on_computer", _stopped_ws)
    monkeypatch.setattr(monitor_service, "owned_workspace_on_computer", _stopped_ws)
    monkeypatch.setattr(artifact_snapshot, "db_get_workspace", _db_get_ws)
    monkeypatch.setattr(artifact_snapshot, "status", _ORIG_SNAPSHOT_STATUS)
    monkeypatch.setattr(artifact_snapshot, "get_vault_secrets_for_redaction", _no_secrets)
    monkeypatch.setattr(artifact_snapshot, "store_unregistered_blobs", _no_blobs)
    monkeypatch.setattr(monitor_service, "validate_publish_permissions", _validate_perms)
    monkeypatch.setattr(monitor_service, "get_monitor_by_artifact", _get_monitor_by_artifact)
    monkeypatch.setattr(monitor_db, "get_monitor_by_artifact_id", _get_monitor_by_artifact_id)
    monkeypatch.setattr(monitor_db, "pick_available_slug", create_autospec(monitor_db.pick_available_slug, return_value="beta-board"))
    monkeypatch.setattr(monitor_db, "commit_monitor_publish_tx", _commit_publish_tx)
    monkeypatch.setattr(monitor_service, "_build_monitor_detail_payload", _build_detail)

    tool = build_dalton_monitor_tool()
    res1 = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/beta", "title": "Beta Board"},
        config=_run_config(),
    )
    assert res1["ok"] is True
    assert res1["version"] == 1
    assert res1["left_out"] == 1
    assert committed_versions[0] == [
        "dashboards/beta/index.html",
        "dashboards/beta/data/keep.json",
    ]
    assert artifact_state["left_out_files"] == ("dashboards/beta/data/excluded.json",)

    res2 = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/beta", "title": "Beta Board"},
        config=_run_config(),
    )
    assert res2["ok"] is True
    assert res2["version"] == 2
    assert res2["left_out"] == 1
    assert committed_versions[1] == [
        "dashboards/beta/index.html",
        "dashboards/beta/data/keep.json",
    ]
    assert "dashboards/beta/data/excluded.json" not in committed_versions[1]
    assert artifact_state["left_out_files"] == ("dashboards/beta/data/excluded.json",)


@pytest.mark.asyncio
async def test_publish_running_folder_skips_symlink_pointing_to_excluded_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlink `alias.json -> excluded.json` must not bypass exclusion; neither file is published."""
    ws_root = tmp_path / "ws-dir"
    folder = ws_root / "dashboards" / "beta"
    (folder / "data").mkdir(parents=True)
    (folder / "index.html").write_text("<html></html>", encoding="utf-8")
    (folder / "data" / "keep.json").write_text('{"ok": true}', encoding="utf-8")
    excluded_file = folder / "data" / "excluded.json"
    excluded_file.write_text('{"secret": 1}', encoding="utf-8")
    (folder / "data" / "alias.json").symlink_to(excluded_file)

    monkeypatch.setattr(
        monitor_tools,
        "get_user_tier",
        create_autospec(monitor_tools.get_user_tier, return_value="internal"),
    )

    async def _running_ws(
        workspace_id: str, user_id: str, *, need: str | None = None
    ) -> dict[str, Any]:
        _ = need
        return {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "status": "running",
            "dir_name": "ws-dir",
            "computer_root_dir": str(tmp_path),
        }

    async def _exec_local(command: str, timeout: int = 30) -> ExecResult:
        _ = timeout
        assert command.startswith("python3 ")
        patched = f"{shlex.quote(sys.executable)}{command.removeprefix('python3')}"
        proc = subprocess.run(
            ["/bin/sh", "-c", patched],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        return ExecResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)

    runtime_mock = create_autospec(SandboxRuntime, instance=True)
    runtime_mock.exec.side_effect = _exec_local

    class _FakeSandbox:
        runtime = runtime_mock

    class _Ctx:
        async def __aenter__(self) -> tuple[object, dict[str, Any]]:
            return _FakeSandbox(), {
                "workspace_id": WS_ID,
                "user_id": USER_ID,
                "dir_name": "ws-dir",
                "computer_root_dir": str(tmp_path),
            }

        async def __aexit__(self, *args: Any) -> None:
            return None

    async def _ensure_existing(workspace_id: str, path: str, **_kw: Any) -> Artifact:
        return _make_artifact(
            workspace_id=workspace_id,
            path=path,
            left_out_files=("dashboards/beta/data/excluded.json",),
        )

    monkeypatch.setattr(
        monitor_tools,
        "owned_workspace_on_computer",
        create_autospec(_ORIG_OWNED_WS, side_effect=_running_ws),
    )
    monkeypatch.setattr(
        monitor_tools,
        "acquire_sandbox_to_change",
        create_autospec(monitor_tools.acquire_sandbox_to_change, return_value=_Ctx()),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(_ORIG_ENSURE_ARTIFACT, side_effect=_ensure_existing),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_monitor_by_artifact",
        create_autospec(
            _ORIG_GET_MONITOR_BY_ARTIFACT,
            return_value=_detail_payload(
                slug="beta-board",
                visibility="own",
                entry_path="dashboards/beta/index.html",
            )["monitor"],
        ),
    )
    publish_mock = create_autospec(
        monitor_service.publish_workspace_monitor,
        return_value=_detail_payload(
            slug="beta-board",
            entry_path="dashboards/beta/index.html",
        ),
    )
    monkeypatch.setattr(monitor_service, "publish_workspace_monitor", publish_mock)

    tool = build_dalton_monitor_tool()
    res = await _invoke(
        tool,
        {"action": "publish", "path": "dashboards/beta"},
        config=_run_config(),
    )
    assert res["ok"] is True
    assert res["left_out"] == 1
    req = publish_mock.await_args.args[0]
    assert req.files == [
        "dashboards/beta/index.html",
        "dashboards/beta/data/keep.json",
    ]
    assert req.loaded == ["dashboards/beta/data/keep.json"]
    assert req.earlier == ["dashboards/beta/data/excluded.json"]
    assert "dashboards/beta/data/excluded.json" not in req.files
    assert "dashboards/beta/data/alias.json" not in req.files
