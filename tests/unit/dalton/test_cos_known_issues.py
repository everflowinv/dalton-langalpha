"""Unit tests for Chief of Staff known permission issues (#1-#4).

Every item covers both denied cases and normal-sharing-works cases using
``create_autospec`` test doubles:
1. Home stand-in workspace (``config.home_stand_in``) rejected uniformly with
   the same "not found" error as a non-existent workspace/thread across all
   non-owner entry points (CoS tools, ``colleague_workspace``, file & thread
   listings, knowledge read, sharing/settings routes), while normal shared
   workspaces continue to work.
2. Unified sensitive path filter (``served_paths.is_non_owner_sensitive_path``
   / ``_shared._is_non_owner_blocked_path``) shared across Chief of Staff,
   Analyst ``colleague_workspace``, and Web non-owner file routes for ``.env``,
   ``.env.*``, hidden dotfiles/dirs (except explicit ``.agents/`` shared
   prefixes), and key/credential filenames (``*.pem``, ``*.key``, ``id_rsa*``,
   ``credentials*.json``, etc.), while normal shared files work.
3. Copy-file revocation safety: stages to a transient temp file, re-verifies
   source sharing permission before atomic rename, and removes the temp file
   without landing the final file when revoked mid-copy (CoS & Analyst),
   while normal shared copies commit atomically.
4. Binary copy secret checks: refuses macro-enabled Office files
   (``.xlsm``, ``.docm``, ``.pptm``, ``.xlam`` and embedded ``vbaProject.bin``)
   with an explanation; runs the same text known-secret redactor on unzipped
   OOXML XML parts (``.docx``, ``.xlsx``, ``.pptx``); and allows clean OOXML,
   images, and scanned image-only PDFs normally.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import zipfile
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import HTTPException
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ptc_agent.core.path_fold import BACKUP_EXCLUDE_AGENT_SUBDIRS
from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from src.dalton import access_tiers as access_tiers_mod
from src.dalton import auth as dalton_auth_mod
from src.dalton import colleague_outbound_guard as guard_mod
from src.dalton import settings as dalton_settings_mod
from src.dalton import sharing as sharing_mod
from src.dalton.app import router as dalton_router_mod
from src.dalton.database import knowledge as db_knowledge_mod
from src.dalton.database import verdicts as db_verdicts_mod
from src.dalton.tools import colleague_workspace_tools as cw_mod
from src.dalton.tools import knowledge_tools as kt_mod
from src.server.app import workspaces as workspaces_app_mod
from src.server.app.workspace_files import _shared as ws_shared_mod
from src.server.app.workspace_files import backup_reads as backup_reads_mod
from src.server.app.workspace_files import crud as ws_files_crud_mod
from src.server.database import conversation as conv_db_mod
from src.server.database import workspace as ws_db_mod
from src.server.database.conversation import threads_read as threads_read_mod
from src.server.dependencies.principal import Principal
from src.server.services import served_paths as served_paths_mod
from src.server.services import workspace_files as ws_files_svc_mod
from src.server.services.persistence import file as persistence_file_mod
from src.server.services.persistence import transfer as transfer_mod
from src.server.services.shared_workspaces import access as shared_access_mod
from src.server.services.workspace_file_containment import ContainedFile
from src.server.utils import secret_redactor as redactor_mod
from src.server.utils.secret_redactor import SecretRedactor
from src.tools.secretary import _commands as sec_commands_mod
from src.tools.secretary import chief_of_staff as cos_mod
from src.tools.secretary import tools as sec_tools_mod


OWNER_ID = "user-owner"
COLLEAGUE_ID = "user-colleague"
STAND_IN_WS_ID = "11111111-1111-5111-8111-111111111111"
NORMAL_WS_ID = "22222222-2222-5222-8222-222222222222"
MISSING_WS_ID = "33333333-3333-5333-8333-333333333333"
CUR_WS_ID = "44444444-4444-5444-8444-444444444444"
HOME_WS_ID = "55555555-5555-5555-8555-555555555555"

STAND_IN_WS_ROW: dict[str, Any] = {
    "workspace_id": STAND_IN_WS_ID,
    "user_id": OWNER_ID,
    "name": "Home Stand-In",
    "dir_name": "Home",
    "status": "stopped",
    "config": {"home_stand_in": True},
    "computer_id": "comp-owner",
    "computer_root_dir": "/home/workspace",
}

NORMAL_SHARED_WS_ROW: dict[str, Any] = {
    "workspace_id": NORMAL_WS_ID,
    "user_id": OWNER_ID,
    "name": "NVDA Deep Dive",
    "dir_name": "NVDA",
    "status": "stopped",
    "config": {},
    "computer_id": "comp-owner",
    "computer_root_dir": "/home/workspace",
}

COLLEAGUE_HOME_ROW: dict[str, Any] = {
    "workspace_id": HOME_WS_ID,
    "user_id": COLLEAGUE_ID,
    "name": "Home",
    "dir_name": "Home",
    "status": "running",
    "config": {},
    "computer_id": "comp-colleague",
    "computer_root_dir": "/home/workspace",
}

COLLEAGUE_CUR_WS_ROW: dict[str, Any] = {
    "workspace_id": CUR_WS_ID,
    "user_id": COLLEAGUE_ID,
    "name": "Semis Tracker",
    "dir_name": "Semis",
    "status": "running",
    "config": {},
    "computer_id": "comp-colleague",
    "computer_root_dir": "/home/workspace",
}


def _cmd_payload(cmd: Command) -> dict[str, Any]:
    msg = cmd.update["messages"][0]
    assert isinstance(msg, ToolMessage)
    return json.loads(msg.content)


async def _invoke_tool(
    tool_obj: Any,
    args: dict[str, Any],
    *,
    config: dict[str, Any],
    call_id: str = "tc-1",
) -> Any:
    return await tool_obj.ainvoke(
        {"name": tool_obj.name, "args": args, "id": call_id, "type": "tool_call"},
        config=config,
    )


def _patch_sharing_env(
    monkeypatch: pytest.MonkeyPatch,
    ws_map: dict[str, dict[str, Any]],
    *,
    cos_access: str = "rw",
    web_access: str = "ro",
    pm_ids: tuple[str, ...] = (),
) -> None:
    """Wire sharing & auth helpers so ``OWNER_ID`` shares normal workspaces to ``COLLEAGUE_ID``."""
    monkeypatch.setattr(
        dalton_settings_mod,
        "is_dalton_sharing_auth_enabled",
        create_autospec(dalton_settings_mod.is_dalton_sharing_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings_mod.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_auth_mod,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_auth_mod.is_dalton_supabase_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        dalton_settings_mod,
        "is_colleague_dispatch_enabled",
        create_autospec(dalton_settings_mod.is_colleague_dispatch_enabled, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "_sharing_auth_enabled",
        create_autospec(sharing_mod._sharing_auth_enabled, return_value=True),
    )
    monkeypatch.setattr(
        sharing_mod,
        "is_colleague_dispatch_enabled",
        create_autospec(sharing_mod.is_colleague_dispatch_enabled, return_value=False),
    )
    monkeypatch.setattr(
        sharing_mod,
        "require_team_reader",
        create_autospec(
            sharing_mod.require_team_reader,
            side_effect=lambda uid, **kw: str(uid),
        ),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_user_tier",
        create_autospec(sharing_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        sharing_mod,
        "classify_owner_tier",
        create_autospec(sharing_mod.classify_owner_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "classify_owner_tier",
        create_autospec(access_tiers_mod.classify_owner_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        sharing_mod,
        "get_workspace_sharing",
        create_autospec(
            sharing_mod.get_workspace_sharing,
            return_value={
                "workspace_id": NORMAL_WS_ID,
                "cos_access": cos_access,
                "web_access": web_access,
                "updated_by": OWNER_ID,
                "updated_at": None,
            },
        ),
    )
    get_ws_mock = create_autospec(
        ws_db_mod.get_workspace,
        side_effect=lambda ws_id, **kw: (
            dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None
        ),
    )
    monkeypatch.setattr(ws_db_mod, "get_workspace", get_ws_mock)
    monkeypatch.setattr(ws_files_crud_mod, "db_get_workspace", get_ws_mock)
    if pm_ids:
        orig_get_settings = dalton_router_mod.get_dalton_settings
        fake_settings = orig_get_settings().model_copy(
            update={"pm_user_ids": frozenset(pm_ids)}
        )
        monkeypatch.setattr(
            dalton_router_mod,
            "get_dalton_settings",
            create_autospec(orig_get_settings, return_value=fake_settings),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_dalton_settings",
            create_autospec(sharing_mod.get_dalton_settings, return_value=fake_settings),
        )


def _make_ooxml_zip(
    members: dict[str, bytes | str],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        if "[Content_Types].xml" not in members:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8"?><Types/>',
            )
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_ole2_bytes(
    entries: list[tuple[str, int]],
    *,
    extra_body: bytes = b"",
) -> bytes:
    """Build a valid minimal OLE2 Compound Document with the given directory entries.

    Each entry is ``(name, etype)`` where ``etype=1`` is a storage and ``etype=2``
    is a stream. Optional ``extra_body`` bytes are stored in an extra data sector
    so clean documents can carry ordinary UTF-16LE text (e.g. ``Macros``/``VBA``)
    without creating a VBA directory entry.
    """
    all_entries = [("Root Entry", 5), *entries]
    assert len(all_entries) <= 4, "helper supports up to 3 child entries"
    extra_sectors = (len(extra_body) + 511) // 512 if extra_body else 0

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9)
    struct.pack_into("<H", header, 32, 6)
    struct.pack_into("<iiiiiiii", header, 44, 1, 1, 0, 4096, -2, 0, -2, 0)
    difat = [0] + [-1] * 108
    struct.pack_into("<109i", header, 76, *difat)

    fat = bytearray(512)
    fat_entries = [-3, -2] + [-1] * 126
    for i in range(extra_sectors):
        sec_idx = 2 + i
        fat_entries[sec_idx] = (sec_idx + 1) if (i + 1 < extra_sectors) else -2
    struct.pack_into("<128i", fat, 0, *fat_entries)

    dir_buf = bytearray(512)
    for idx, (name, etype) in enumerate(all_entries):
        off = idx * 128
        raw_name = name.encode("utf-16-le") + b"\x00\x00"
        dir_buf[off : off + len(raw_name)] = raw_name
        cbufsize = len(raw_name)
        left_did = -1
        right_did = idx + 1 if (idx >= 1 and idx + 1 < len(all_entries)) else -1
        root_did = 1 if (idx == 0 and len(all_entries) > 1) else -1
        first_sid = 2 if (idx == 1 and extra_sectors > 0) else -2
        tot_size = len(extra_body) if (idx == 1 and extra_sectors > 0) else 0
        struct.pack_into(
            "<HBBiii", dir_buf, off + 64, cbufsize, etype, 1, left_did, right_did, root_did
        )
        struct.pack_into("<ii", dir_buf, off + 116, first_sid, tot_size)

    padded_body = extra_body + b"\x00" * (extra_sectors * 512 - len(extra_body))
    return bytes(header + fat + dir_buf + padded_body)


def _make_minimal_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 32


def _make_scanned_image_pdf() -> bytes:
    """Minimal valid PDF with one blank/image-style page and no extractable text."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ===========================================================================
# 1. Home stand-in (config.home_stand_in) uniform refusal vs normal sharing
# ===========================================================================


class TestHomeStandInRefusal:
    @pytest.mark.asyncio
    async def test_require_workspace_access_and_sharing_routes_hide_stand_in_like_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_map = {
            STAND_IN_WS_ID: STAND_IN_WS_ROW,
            NORMAL_WS_ID: NORMAL_SHARED_WS_ROW,
        }
        _patch_sharing_env(monkeypatch, ws_map, pm_ids=(COLLEAGUE_ID,))

        # Denied: non-owner (even PM!) accessing stand-in gets exact same 404 as missing
        with pytest.raises(HTTPException) as exc_missing:
            await sharing_mod.require_workspace_access(
                MISSING_WS_ID, COLLEAGUE_ID, channel="cos", need="read"
            )
        with pytest.raises(HTTPException) as exc_stand_in_cos:
            await sharing_mod.require_workspace_access(
                STAND_IN_WS_ID, COLLEAGUE_ID, channel="cos", need="read"
            )
        with pytest.raises(HTTPException) as exc_stand_in_web:
            await sharing_mod.require_workspace_access(
                STAND_IN_WS_ID, COLLEAGUE_ID, channel="web", need="read"
            )
        assert exc_stand_in_cos.value.status_code == exc_missing.value.status_code == 404
        assert exc_stand_in_cos.value.detail == exc_missing.value.detail
        assert exc_stand_in_web.value.status_code == 404
        assert exc_stand_in_web.value.detail == exc_missing.value.detail

        # Denied: can_edit helpers reject stand-in even for owner/PM
        assert sharing_mod.can_edit_workspace_sharing(STAND_IN_WS_ROW, COLLEAGUE_ID) is False
        assert sharing_mod.can_edit_workspace_sharing(STAND_IN_WS_ROW, OWNER_ID) is False
        assert sharing_mod.can_edit_workspace_settings(STAND_IN_WS_ROW, OWNER_ID) is False

        # Denied: sharing & settings REST endpoints return 404 for non-owner (including PM)
        for endpoint, args in (
            (dalton_router_mod.get_workspace_sharing_endpoint, (STAND_IN_WS_ID, COLLEAGUE_ID)),
            (
                dalton_router_mod.update_workspace_sharing_endpoint,
                (
                    STAND_IN_WS_ID,
                    dalton_router_mod.WorkspaceSharingUpdateRequest(web_access="ro"),
                    COLLEAGUE_ID,
                ),
            ),
            (dalton_router_mod.get_workspace_settings_endpoint, (STAND_IN_WS_ID, COLLEAGUE_ID)),
            (
                dalton_router_mod.update_workspace_settings_endpoint,
                (
                    STAND_IN_WS_ID,
                    dalton_router_mod.WorkspaceSettingsUpdateRequest(team_memory_enabled=True),
                    COLLEAGUE_ID,
                ),
            ),
        ):
            with pytest.raises(HTTPException) as exc_ep:
                await endpoint(*args)
            assert exc_ep.value.status_code == 404
            assert exc_ep.value.detail == "Workspace not found"

        # Normal sharing works: colleague reads normal shared workspace & owner still accesses own stand-in
        ws_ok = await sharing_mod.require_workspace_access(
            NORMAL_WS_ID, COLLEAGUE_ID, channel="cos", need="read"
        )
        assert ws_ok["workspace_id"] == NORMAL_WS_ID
        own_stand_in = await sharing_mod.require_workspace_access(
            STAND_IN_WS_ID, OWNER_ID, channel="web", need="read"
        )
        assert own_stand_in["workspace_id"] == STAND_IN_WS_ID

    @pytest.mark.asyncio
    async def test_cos_tools_colleague_workspace_and_knowledge_read_refuse_stand_in_and_allow_normal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        orig_supabase_auth_enabled = dalton_auth_mod.is_dalton_supabase_auth_enabled
        ws_map = {
            STAND_IN_WS_ID: STAND_IN_WS_ROW,
            NORMAL_WS_ID: NORMAL_SHARED_WS_ROW,
            CUR_WS_ID: COLLEAGUE_CUR_WS_ROW,
            HOME_WS_ID: COLLEAGUE_HOME_ROW,
        }
        _patch_sharing_env(monkeypatch, ws_map, cos_access="ro", web_access="ro")

        monkeypatch.setattr(
            persistence_file_mod.FilePersistenceService,
            "get_file_tree",
            staticmethod(
                create_autospec(
                    persistence_file_mod.FilePersistenceService.get_file_tree,
                    return_value=[{"path": "reports/q3.md"}],
                )
            ),
        )
        monkeypatch.setattr(
            persistence_file_mod.FilePersistenceService,
            "get_file_content",
            staticmethod(
                create_autospec(
                    persistence_file_mod.FilePersistenceService.get_file_content,
                    return_value={
                        "path": "reports/q3.md",
                        "is_binary": False,
                        "file_size": 12,
                    },
                )
            ),
        )
        monkeypatch.setattr(
            ws_shared_mod,
            "http_file_text",
            create_autospec(ws_shared_mod.http_file_text, return_value="# Q3 Report"),
        )
        monkeypatch.setattr(
            redactor_mod,
            "get_vault_secrets_for_redaction",
            create_autospec(redactor_mod.get_vault_secrets_for_redaction, return_value={}),
        )
        monkeypatch.setattr(
            threads_read_mod,
            "get_workspace_threads",
            create_autospec(
                threads_read_mod.get_workspace_threads,
                return_value=([{"conversation_thread_id": "t-normal", "title": "Q3", "current_status": "idle"}], 1),
            ),
        )
        monkeypatch.setattr(
            threads_read_mod,
            "get_thread_owner_id",
            create_autospec(
                threads_read_mod.get_thread_owner_id,
                side_effect=lambda tid: None if tid == "t-missing" else OWNER_ID,
            ),
        )

        async def _thread_meta(tid: str, **kw: Any) -> dict[str, Any] | None:
            if tid == "t-stand-in":
                return {
                    "workspace_id": STAND_IN_WS_ID,
                    "workspace_owner_id": OWNER_ID,
                    "user_id": OWNER_ID,
                    "thread_user_id": OWNER_ID,
                }
            if tid == "t-normal":
                return {
                    "workspace_id": NORMAL_WS_ID,
                    "workspace_owner_id": OWNER_ID,
                    "user_id": OWNER_ID,
                    "thread_user_id": OWNER_ID,
                }
            return None

        monkeypatch.setattr(
            threads_read_mod,
            "get_thread_auth_meta",
            create_autospec(threads_read_mod.get_thread_auth_meta, side_effect=_thread_meta),
        )
        monkeypatch.setattr(
            conv_db_mod,
            "get_thread_auth_meta",
            create_autospec(conv_db_mod.get_thread_auth_meta, side_effect=_thread_meta),
        )
        monkeypatch.setattr(
            cw_mod,
            "get_thread_auth_meta",
            create_autospec(cw_mod.get_thread_auth_meta, side_effect=_thread_meta),
        )
        monkeypatch.setattr(
            cw_mod,
            "get_workspace",
            create_autospec(
                cw_mod.get_workspace,
                side_effect=lambda ws_id: dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None,
            ),
        )
        monkeypatch.setattr(
            guard_mod,
            "mark_run_outbound_blocked",
            create_autospec(guard_mod.mark_run_outbound_blocked, return_value=True),
        )
        monkeypatch.setattr(
            cw_mod,
            "mark_run_outbound_blocked",
            create_autospec(cw_mod.mark_run_outbound_blocked, return_value=True),
        )

        # 1) CoS list_files / read_file / threads_list / verify_thread_owner refuse stand-in identically to missing
        p_missing_list = _cmd_payload(
            await sec_tools_mod.workspace_files_list_cos(COLLEAGUE_ID, MISSING_WS_ID, None, "tc-1")
        )
        p_standin_list = _cmd_payload(
            await sec_tools_mod.workspace_files_list_cos(COLLEAGUE_ID, STAND_IN_WS_ID, None, "tc-2")
        )
        assert p_standin_list == p_missing_list == {"success": False, "error": "workspace not found"}

        p_standin_read = _cmd_payload(
            await sec_tools_mod.workspace_file_read_cos(COLLEAGUE_ID, STAND_IN_WS_ID, "reports/q3.md", "tc-3")
        )
        assert p_standin_read == {"success": False, "error": "workspace not found"}

        p_standin_threads = _cmd_payload(
            await sec_tools_mod.threads_list(COLLEAGUE_ID, STAND_IN_WS_ID, "tc-4")
        )
        assert p_standin_threads == {"success": False, "error": "workspace not found"}

        err_missing_thread = await sec_commands_mod.verify_thread_owner("t-missing", COLLEAGUE_ID, "tc-5")
        err_standin_thread = await sec_commands_mod.verify_thread_owner("t-stand-in", COLLEAGUE_ID, "tc-6")
        assert err_standin_thread is not None and err_missing_thread is not None
        assert _cmd_payload(err_standin_thread) == _cmd_payload(err_missing_thread)

        # Normal CoS sharing works
        p_ok_list = _cmd_payload(
            await sec_tools_mod.workspace_files_list_cos(COLLEAGUE_ID, NORMAL_WS_ID, None, "tc-7")
        )
        assert p_ok_list["success"] is True
        assert p_ok_list["files"] == ["reports/q3.md"]
        p_ok_read = _cmd_payload(
            await sec_tools_mod.workspace_file_read_cos(COLLEAGUE_ID, NORMAL_WS_ID, "reports/q3.md", "tc-8")
        )
        assert p_ok_read["success"] is True and "# Q3 Report" in p_ok_read["content"]
        p_ok_threads = _cmd_payload(
            await sec_tools_mod.threads_list(COLLEAGUE_ID, NORMAL_WS_ID, "tc-9")
        )
        assert p_ok_threads["success"] is True and len(p_ok_threads["threads"]) == 1
        assert await sec_commands_mod.verify_thread_owner("t-normal", COLLEAGUE_ID, "tc-10") is None

        # 2) Analyst colleague_workspace refuses stand-in identically to missing, allows normal shared workspace
        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        analyst_cfg = {
            "configurable": {
                "user_id": COLLEAGUE_ID,
                "workspace_id": CUR_WS_ID,
                "workspace_owner_id": COLLEAGUE_ID,
                "origin_channel": "web",
            }
        }
        p_cw_standin = _cmd_payload(
            await _invoke_tool(
                cw_tool,
                {"action": "list_files", "workspace_id": STAND_IN_WS_ID},
                config=analyst_cfg,
            )
        )
        p_cw_missing = _cmd_payload(
            await _invoke_tool(
                cw_tool,
                {"action": "list_files", "workspace_id": MISSING_WS_ID},
                config=analyst_cfg,
            )
        )
        assert p_cw_standin == p_cw_missing == {"success": False, "error": "workspace not found"}

        p_cw_thread_standin = _cmd_payload(
            await _invoke_tool(
                cw_tool,
                {"action": "read_thread", "thread_id": "t-stand-in"},
                config=analyst_cfg,
            )
        )
        assert p_cw_thread_standin == {"success": False, "error": "thread not found"}

        p_cw_normal = _cmd_payload(
            await _invoke_tool(
                cw_tool,
                {"action": "list_files", "workspace_id": NORMAL_WS_ID},
                config=analyst_cfg,
            )
        )
        assert p_cw_normal["success"] is True and p_cw_normal["count"] == 1

        # 3) knowledge_read (both CoS and Analyst) refuses stand-in with same 404 as missing, allows normal
        monkeypatch.setattr(
            db_knowledge_mod,
            "latest_valid_version",
            create_autospec(
                db_knowledge_mod.latest_valid_version,
                return_value={"version_no": 2, "content": {"summary": "NVDA thesis"}},
            ),
        )
        monkeypatch.setattr(
            db_knowledge_mod,
            "latest_version",
            create_autospec(
                db_knowledge_mod.latest_version,
                return_value={"version_no": 2, "content": {"summary": "NVDA thesis"}},
            ),
        )
        monkeypatch.setattr(
            db_verdicts_mod,
            "list_verdicts",
            create_autospec(db_verdicts_mod.list_verdicts, return_value=[]),
        )

        cos_k_read = kt_mod.create_knowledge_tools(None, role="chief_of_staff")[2]
        cos_cfg = {"configurable": {"user_id": COLLEAGUE_ID, "workspace_id": HOME_WS_ID}}
        with pytest.raises(HTTPException) as exc_k_missing:
            await _invoke_tool(
                cos_k_read,
                {"workspace_id": MISSING_WS_ID, "doc": "thesis"},
                config=cos_cfg,
            )
        with pytest.raises(HTTPException) as exc_k_standin:
            await _invoke_tool(
                cos_k_read,
                {"workspace_id": STAND_IN_WS_ID, "doc": "thesis"},
                config=cos_cfg,
            )
        assert exc_k_standin.value.status_code == exc_k_missing.value.status_code == 404

        raw_k_ok = await _invoke_tool(
            cos_k_read,
            {"workspace_id": NORMAL_WS_ID, "doc": "thesis"},
            config=cos_cfg,
        )
        assert json.loads(raw_k_ok.content)["ok"] is True

        analyst_k_read = kt_mod.create_knowledge_tools(None, role="analyst")[2]
        with pytest.raises(HTTPException) as exc_ak_standin:
            await _invoke_tool(
                analyst_k_read,
                {"workspace_id": STAND_IN_WS_ID, "doc": "thesis"},
                config=analyst_cfg,
            )
        assert exc_ak_standin.value.status_code == 404
        raw_ak_ok = await _invoke_tool(
            analyst_k_read,
            {"workspace_id": NORMAL_WS_ID, "doc": "thesis"},
            config=analyst_cfg,
        )
        assert "NVDA thesis" in raw_ak_ok.content

        # 4) Web files & threads list endpoints refuse stand-in with 404
        with pytest.raises(HTTPException) as exc_wf_standin:
            await ws_files_crud_mod.list_workspace_files(
                workspace_id=STAND_IN_WS_ID,
                x_user_id=COLLEAGUE_ID,
                path=".",
                include_system=False,
                pattern="**/*",
                wait_for_sandbox=False,
                auto_start=False,
                backup_ok=True,
            )
        assert exc_wf_standin.value.status_code == 404

        # Verify dalton_auth.verify_workspace_access also rejects stand-in with exact 404 when auth off
        monkeypatch.setattr(
            dalton_auth_mod,
            "is_dalton_supabase_auth_enabled",
            create_autospec(orig_supabase_auth_enabled, return_value=False),
        )
        with pytest.raises(HTTPException) as exc_auth_standin:
            await dalton_auth_mod.verify_workspace_access(
                STAND_IN_WS_ID, COLLEAGUE_ID, need="read", channel="cos"
            )
        assert exc_auth_standin.value.status_code == 404
        assert exc_auth_standin.value.detail == f"Workspace '{STAND_IN_WS_ID}' not found"
        # Unused import reference check: cos_mod.agent_output delegates to verify_thread_owner
        assert callable(cos_mod.agent_output.coroutine)

    @pytest.mark.asyncio
    async def test_workspace_routes_hide_stand_in_and_authorize_before_home_delete_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.requests import Request
        from src.server.database.home_workspace import get_home_workspace_id

        owner_home_id = get_home_workspace_id(OWNER_ID)
        owner_home_row = {
            **NORMAL_SHARED_WS_ROW,
            "workspace_id": owner_home_id,
            "name": "Home",
            "dir_name": "Home",
            "config": {},
        }
        ws_map = {
            STAND_IN_WS_ID: STAND_IN_WS_ROW,
            NORMAL_WS_ID: NORMAL_SHARED_WS_ROW,
            owner_home_id: owner_home_row,
        }
        get_ws_mock = create_autospec(
            ws_db_mod.get_workspace,
            side_effect=lambda ws_id, **kw: (
                dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None
            ),
        )
        monkeypatch.setattr(workspaces_app_mod, "db_get_workspace", get_ws_mock)
        monkeypatch.setattr(ws_db_mod, "get_workspace", get_ws_mock)
        monkeypatch.setattr(shared_access_mod.workspace_db, "get_workspace", get_ws_mock)

        # 1) /events (workspaces.py:533): stand-in returns same 404 as missing
        with pytest.raises(HTTPException) as exc_ev_missing:
            await workspaces_app_mod.workspace_status_events(MISSING_WS_ID, COLLEAGUE_ID)
        with pytest.raises(HTTPException) as exc_ev_standin:
            await workspaces_app_mod.workspace_status_events(STAND_IN_WS_ID, COLLEAGUE_ID)
        assert exc_ev_standin.value.status_code == exc_ev_missing.value.status_code == 404
        assert exc_ev_standin.value.detail == exc_ev_missing.value.detail

        # 2) GET /move (workspaces.py:1172): stand-in returns same 404 as missing
        with pytest.raises(HTTPException) as exc_mv_missing:
            await workspaces_app_mod.get_workspace_move(MISSING_WS_ID, COLLEAGUE_ID)
        with pytest.raises(HTTPException) as exc_mv_standin:
            await workspaces_app_mod.get_workspace_move(STAND_IN_WS_ID, COLLEAGUE_ID)
        assert exc_mv_standin.value.status_code == exc_mv_missing.value.status_code == 404
        assert exc_mv_standin.value.detail == exc_mv_missing.value.detail

        # 3) DELETE /{workspace_id} (workspaces.py:1293): authorize BEFORE "Cannot delete Home"
        req = create_autospec(Request, instance=True)
        colleague_principal = Principal(user_id=COLLEAGUE_ID, auth="session")
        owner_principal = Principal(user_id=OWNER_ID, auth="session")

        with pytest.raises(HTTPException) as exc_del_missing:
            await workspaces_app_mod.delete_workspace(MISSING_WS_ID, colleague_principal, req)
        with pytest.raises(HTTPException) as exc_del_standin:
            await workspaces_app_mod.delete_workspace(STAND_IN_WS_ID, colleague_principal, req)
        assert exc_del_standin.value.status_code == exc_del_missing.value.status_code == 404
        assert exc_del_standin.value.detail == exc_del_missing.value.detail

        # Non-owner deleting regular Home gets 403 Forbidden (not 400 Cannot delete Home)
        with pytest.raises(HTTPException) as exc_del_home_colleague:
            await workspaces_app_mod.delete_workspace(owner_home_id, colleague_principal, req)
        assert exc_del_home_colleague.value.status_code == 403

        # Owner deleting own Home or own stand-in still gets 400 Cannot delete Home
        for own_reserved_id in (owner_home_id, STAND_IN_WS_ID):
            with pytest.raises(HTTPException) as exc_del_own:
                await workspaces_app_mod.delete_workspace(own_reserved_id, owner_principal, req)
            assert exc_del_own.value.status_code == 400
            assert exc_del_own.value.detail == "Cannot delete Home"

    @pytest.mark.asyncio
    async def test_partial_dict_missing_config_queries_db_and_thread_access_hides_stand_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_map = {
            STAND_IN_WS_ID: STAND_IN_WS_ROW,
            NORMAL_WS_ID: NORMAL_SHARED_WS_ROW,
        }
        orig_dispatch_enabled = dalton_settings_mod.is_colleague_dispatch_enabled
        orig_user_cos_sharing = sharing_mod.get_user_cos_sharing
        _patch_sharing_env(monkeypatch, ws_map, cos_access="rw", web_access="hidden")
        monkeypatch.setattr(
            sharing_mod,
            "is_colleague_dispatch_enabled",
            create_autospec(orig_dispatch_enabled, return_value=True),
        )
        monkeypatch.setattr(
            sharing_mod,
            "get_user_cos_sharing",
            create_autospec(orig_user_cos_sharing, return_value={"cos_rw_enabled": True}),
        )

        # Partial dict omitting "config" must query DB and still return 404 (not assume non-stand-in)
        partial_stand_in = {
            "workspace_id": STAND_IN_WS_ID,
            "user_id": OWNER_ID,
            "status": "running",
        }
        with pytest.raises(HTTPException) as exc_partial_sharing:
            await sharing_mod.require_workspace_access(
                partial_stand_in, COLLEAGUE_ID, channel="cos", need="read"
            )
        assert exc_partial_sharing.value.status_code == 404
        assert exc_partial_sharing.value.detail == "Workspace not found"

        # Pre-existing cross-user thread in owner's Home stand-in: web thread-initiator fast path
        # MUST NOT bypass workspace ownership / stand-in check, and returns "Thread not found".
        async def _thread_meta(tid: str, **kw: Any) -> dict[str, Any] | None:
            if tid == "t-standin-cross":
                return {
                    "workspace_id": STAND_IN_WS_ID,
                    "workspace_owner_id": OWNER_ID,
                    "user_id": COLLEAGUE_ID,
                    "thread_user_id": COLLEAGUE_ID,
                }
            if tid == "t-hidden-normal":
                return {
                    "workspace_id": NORMAL_WS_ID,
                    "workspace_owner_id": OWNER_ID,
                    "user_id": OWNER_ID,
                    "thread_user_id": OWNER_ID,
                }
            return None

        monkeypatch.setattr(
            conv_db_mod,
            "get_thread_auth_meta",
            create_autospec(conv_db_mod.get_thread_auth_meta, side_effect=_thread_meta),
        )

        with pytest.raises(HTTPException) as exc_missing_t:
            await sharing_mod.require_thread_access(
                "t-does-not-exist", COLLEAGUE_ID, channel="web", need="read"
            )
        with pytest.raises(HTTPException) as exc_standin_web_t:
            await sharing_mod.require_thread_access(
                "t-standin-cross", COLLEAGUE_ID, channel="web", need="read"
            )
        with pytest.raises(HTTPException) as exc_hidden_ws_t:
            await sharing_mod.require_thread_access(
                "t-hidden-normal", COLLEAGUE_ID, channel="web", need="read"
            )
        assert exc_missing_t.value.status_code == 404
        assert exc_missing_t.value.detail == "Thread not found"
        assert exc_standin_web_t.value.status_code == 404
        assert exc_standin_web_t.value.detail == "Thread not found"
        # Invisible workspace in require_thread_access normalizes 404 detail to "Thread not found"
        assert exc_hidden_ws_t.value.status_code == 404
        assert exc_hidden_ws_t.value.detail == "Thread not found"

    @pytest.mark.asyncio
    async def test_partial_dict_refetch_missing_returns_404_and_runs_after_hidden_and_ro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4 item 3: missing DB row on refetch returns 404 (not stale dict), and
        refetch runs after hidden (404) and ro+write (403) so timeouts never turn
        existing 404/403 refusals into 503."""
        partial_ws = {
            "workspace_id": NORMAL_WS_ID,
            "user_id": OWNER_ID,
            "status": "running",
        }

        orig_get_ws = ws_db_mod.get_workspace
        orig_get_sharing = sharing_mod.get_workspace_sharing

        # 1) Refetch returns None (deleted workspace) -> uniform 404, not authorized
        _patch_sharing_env(monkeypatch, {}, cos_access="rw", web_access="rw")
        with pytest.raises(HTTPException) as exc_gone:
            await sharing_mod.require_workspace_access(
                partial_ws, COLLEAGUE_ID, channel="cos", need="read"
            )
        assert exc_gone.value.status_code == 404
        assert exc_gone.value.detail == "Workspace not found"

        # 2) hidden workspace -> returns 404 without calling get_workspace (even if DB times out)
        monkeypatch.setattr(
            sharing_mod,
            "get_workspace_sharing",
            create_autospec(
                orig_get_sharing,
                return_value={
                    "workspace_id": NORMAL_WS_ID,
                    "cos_access": "hidden",
                    "web_access": "hidden",
                    "updated_by": OWNER_ID,
                    "updated_at": None,
                },
            ),
        )
        timeout_get_ws = create_autospec(
            orig_get_ws, side_effect=TimeoutError("db timeout")
        )
        monkeypatch.setattr(ws_db_mod, "get_workspace", timeout_get_ws)
        with pytest.raises(HTTPException) as exc_hidden:
            await sharing_mod.require_workspace_access(
                partial_ws, COLLEAGUE_ID, channel="web", need="read"
            )
        assert exc_hidden.value.status_code == 404
        assert exc_hidden.value.detail == "Workspace not found"
        timeout_get_ws.assert_not_awaited()

        # 3) ro workspace with need="write" -> returns 403 without calling get_workspace
        monkeypatch.setattr(
            sharing_mod,
            "get_workspace_sharing",
            create_autospec(
                orig_get_sharing,
                return_value={
                    "workspace_id": NORMAL_WS_ID,
                    "cos_access": "ro",
                    "web_access": "ro",
                    "updated_by": OWNER_ID,
                    "updated_at": None,
                },
            ),
        )
        timeout_get_ws_ro = create_autospec(
            orig_get_ws, side_effect=TimeoutError("db timeout")
        )
        monkeypatch.setattr(ws_db_mod, "get_workspace", timeout_get_ws_ro)
        with pytest.raises(HTTPException) as exc_ro:
            await sharing_mod.require_workspace_access(
                partial_ws, COLLEAGUE_ID, channel="web", need="write"
            )
        assert exc_ro.value.status_code == 403
        assert exc_ro.value.detail == "Forbidden"
        timeout_get_ws_ro.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_workspaces_access_decides_from_the_row_without_a_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``workspace_access`` gets a full ``get_workspace`` row, so the stand-in
        check reads that row and never queries again: a stand-in row is 404 to a
        non-owner, and a row without ``config`` keeps the plain 403."""
        get_ws = create_autospec(
            ws_db_mod.get_workspace,
            side_effect=RuntimeError("Conversation database pool is not open"),
        )
        monkeypatch.setattr(shared_access_mod.workspace_db, "get_workspace", get_ws)

        with pytest.raises(HTTPException) as exc_stand_in:
            await shared_access_mod.workspace_access(STAND_IN_WS_ROW, COLLEAGUE_ID)
        assert exc_stand_in.value.status_code == 404

        plain_ws = {
            "workspace_id": "00000000-0000-4000-8000-0000000000aa",
            "user_id": OWNER_ID,
            "status": "running",
        }
        with pytest.raises(HTTPException) as exc_plain:
            await shared_access_mod.workspace_access(plain_ws, COLLEAGUE_ID)
        assert exc_plain.value.status_code == 403
        get_ws.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sharing_disabled_refuses_prior_thread_in_colleague_home_stand_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4 item 4: with colleague sharing OFF, if A previously started a thread in
        B's Home stand-in workspace, A requesting that thread is refused with 404
        'Thread not found' (same as a missing thread), both via require_thread_access
        and via GET /threads/{thread_id}."""
        from src.server.app.threads import crud as threads_crud_mod

        orig_get_meta = conv_db_mod.get_thread_auth_meta
        orig_get_owner = conv_db_mod.get_thread_owner_id
        orig_get_ws = ws_db_mod.get_workspace
        ws_map = {STAND_IN_WS_ID: STAND_IN_WS_ROW}
        orig_sharing_enabled = sharing_mod._sharing_auth_enabled
        orig_supabase_enabled = dalton_settings_mod.is_dalton_supabase_auth_enabled
        _patch_sharing_env(monkeypatch, ws_map, cos_access="rw", web_access="rw")
        # Keep Supabase auth ON, turn colleague sharing OFF
        monkeypatch.setattr(
            sharing_mod,
            "_sharing_auth_enabled",
            create_autospec(orig_sharing_enabled, return_value=False),
        )
        monkeypatch.setattr(
            dalton_settings_mod,
            "is_dalton_supabase_auth_enabled",
            create_autospec(orig_supabase_enabled, return_value=True),
        )

        stand_in_thread_meta = {
            "workspace_id": STAND_IN_WS_ID,
            "workspace_owner_id": OWNER_ID,
            "user_id": COLLEAGUE_ID,
            "owner_id": COLLEAGUE_ID,
            "thread_user_id": COLLEAGUE_ID,
            "is_shared": False,
        }

        async def _meta(tid: str, **kw: Any) -> dict[str, Any] | None:
            return dict(stand_in_thread_meta) if tid == "t-legacy-standin" else None

        async def _owner_id(tid: str, **kw: Any) -> str | None:
            return COLLEAGUE_ID if tid == "t-legacy-standin" else None

        meta_mock = create_autospec(orig_get_meta, side_effect=_meta)
        owner_mock = create_autospec(orig_get_owner, side_effect=_owner_id)
        monkeypatch.setattr(conv_db_mod, "get_thread_auth_meta", meta_mock)
        monkeypatch.setattr(conv_db_mod, "get_thread_owner_id", owner_mock)
        monkeypatch.setattr(threads_read_mod, "get_thread_auth_meta", meta_mock)
        monkeypatch.setattr(threads_read_mod, "get_thread_owner_id", owner_mock)
        monkeypatch.setattr(shared_access_mod.conversation_db, "get_thread_auth_meta", meta_mock)
        monkeypatch.setattr(
            shared_access_mod.workspace_db,
            "get_workspace",
            create_autospec(
                orig_get_ws,
                side_effect=lambda ws_id, **kw: (
                    dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None
                ),
            ),
        )

        # Direct require_thread_access (with and without owner_id shortcut)
        with pytest.raises(HTTPException) as exc_missing:
            await sharing_mod.require_thread_access(
                "t-nonexistent", COLLEAGUE_ID, channel="web", need="read"
            )
        with pytest.raises(HTTPException) as exc_off:
            await sharing_mod.require_thread_access(
                "t-legacy-standin", COLLEAGUE_ID, channel="web", need="read"
            )
        with pytest.raises(HTTPException) as exc_off_owner_hint:
            await sharing_mod.require_thread_access(
                "t-legacy-standin",
                COLLEAGUE_ID,
                channel="web",
                need="read",
                owner_id=COLLEAGUE_ID,
            )
        assert exc_off.value.status_code == exc_missing.value.status_code == 404
        assert exc_off.value.detail == exc_missing.value.detail == "Thread not found"
        assert exc_off_owner_hint.value.status_code == 404
        assert exc_off_owner_hint.value.detail == "Thread not found"

        # Web GET /threads/{thread_id} with Supabase auth ON & colleague sharing OFF
        with pytest.raises(HTTPException) as exc_route:
            await threads_crud_mod.get_thread("t-legacy-standin", COLLEAGUE_ID)
        assert exc_route.value.status_code == 404
        assert exc_route.value.detail == "Thread not found"

        # Also refused via shared_workspaces.access.thread_access when Supabase auth is OFF
        monkeypatch.setattr(
            dalton_settings_mod,
            "is_dalton_supabase_auth_enabled",
            create_autospec(orig_supabase_enabled, return_value=False),
        )
        with pytest.raises(HTTPException) as exc_generic_thread:
            await shared_access_mod.thread_access(
                "t-legacy-standin",
                COLLEAGUE_ID,
                meta={**stand_in_thread_meta, "workspace_config": {"home_stand_in": True}},
            )
        assert exc_generic_thread.value.status_code == 404
        assert exc_generic_thread.value.detail == "Thread not found"


# ===========================================================================
# 2. Unified sensitive path filter across CoS, Analyst, and Web non-owner routes
# ===========================================================================


class TestUnifiedSensitivePathFilter:
    @pytest.mark.parametrize(
        "blocked_path",
        [
            ".env",
            ".env.local",
            ".env.production",
            "config/.env",
            "config/.env.staging",
            "prod.env",
            ".hidden",
            "reports/.secret.md",
            ".ssh/id_rsa",
            ".aws/credentials",
            "sub/.hidden_dir/readme.md",
            ".agents/memory/notes.md",
            ".agents/skills/foo/SKILL.md",
            "certs/server.pem",
            "tls/PRIVATE.KEY",
            "keys/id_rsa",
            "keys/id_rsa.pub",
            "keys/id_rsa_prod",
            "keys/id_ed25519",
            "gcp/credentials.json",
            "gcp/credentials_prod.json",
            "aws_credentials.json",
            "gcp-credentials.json",
            "secret.json",
            "token.json",
            "deploy_key",
            "prod_private_key.yaml",
            "api_TOKEN.yml",
            "service_secret.toml",
            "backup_id_ed25519",
            "auth/client_secret_app.json",
            "auth/service_account.json",
            "secrets.yaml",
            "secrets.json",
            "cert.p12",
            "cert.pfx",
            "putty.ppk",
            ".agents/shared/guide.md",
            ".agents/tmp/colleague-copy-1234",
        ],
    )
    def test_sensitive_paths_blocked_by_shared_classifier(self, blocked_path: str) -> None:
        assert served_paths_mod.is_non_owner_sensitive_path(blocked_path) is True
        assert ws_shared_mod._is_non_owner_blocked_path(blocked_path) is True

    @pytest.mark.parametrize(
        "allowed_path",
        [
            "reports/q3_summary.md",
            "notes/key_drivers.md",
            "notes/tokenomics_analysis.md",
            "data/company.json",
            "models/NVDA_DCF.xlsx",
            "shared/valuation_key_points.txt",
            "work/task1/output.html",
            "research.env.md",
            "notes/macro.env.txt",
            ".",
            "./",
        ],
    )
    def test_normal_research_paths_and_dot_root_allowed_by_shared_classifier(
        self, allowed_path: str
    ) -> None:
        assert served_paths_mod.is_non_owner_sensitive_path(allowed_path) is False
        assert ws_shared_mod._is_non_owner_blocked_path(allowed_path) is False

    def test_agents_has_no_non_owner_shared_carveout_and_dot_root_allowed(self) -> None:
        assert not hasattr(served_paths_mod, "_NON_OWNER_SHARED_AGENTS_PREFIXES")
        assert served_paths_mod.is_non_owner_sensitive_path(".agents/shared/guide.md") is True
        assert ws_shared_mod._is_non_owner_blocked_path(".agents/shared/guide.md") is True
        assert served_paths_mod.is_non_owner_sensitive_path(".agents/memory/private.md") is True
        assert served_paths_mod.is_non_owner_sensitive_path(".") is False
        assert ws_shared_mod._is_non_owner_blocked_path(".") is False

    @pytest.mark.asyncio
    async def test_cos_analyst_and_web_backup_share_same_sensitive_path_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_map = {
            NORMAL_WS_ID: NORMAL_SHARED_WS_ROW,
            CUR_WS_ID: COLLEAGUE_CUR_WS_ROW,
            HOME_WS_ID: COLLEAGUE_HOME_ROW,
        }
        _patch_sharing_env(monkeypatch, ws_map, cos_access="ro", web_access="ro")

        tree = [
            {"path": "reports/q3.md"},
            {"path": "notes/key_drivers.md"},
            {"path": ".env"},
            {"path": ".env.production"},
            {"path": "sub/.hidden.txt"},
            {"path": "certs/api.pem"},
            {"path": "certs/tls.key"},
            {"path": "keys/id_rsa"},
            {"path": "gcp/credentials_prod.json"},
            {"path": ".agents/memory/secret.md"},
        ]
        monkeypatch.setattr(
            persistence_file_mod.FilePersistenceService,
            "get_file_tree",
            staticmethod(
                create_autospec(
                    persistence_file_mod.FilePersistenceService.get_file_tree,
                    return_value=tree,
                )
            ),
        )
        monkeypatch.setattr(
            persistence_file_mod.FilePersistenceService,
            "get_file_content",
            staticmethod(
                create_autospec(
                    persistence_file_mod.FilePersistenceService.get_file_content,
                    side_effect=lambda ws_id, path: {
                        "path": path,
                        "is_binary": False,
                        "file_size": 10,
                    },
                )
            ),
        )
        monkeypatch.setattr(
            ws_shared_mod,
            "http_file_text",
            create_autospec(ws_shared_mod.http_file_text, return_value="normal text"),
        )
        monkeypatch.setattr(
            redactor_mod,
            "get_vault_secrets_for_redaction",
            create_autospec(redactor_mod.get_vault_secrets_for_redaction, return_value={}),
        )
        monkeypatch.setattr(
            cw_mod,
            "get_workspace",
            create_autospec(
                cw_mod.get_workspace,
                side_effect=lambda ws_id: dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None,
            ),
        )
        monkeypatch.setattr(
            cw_mod,
            "mark_run_outbound_blocked",
            create_autospec(cw_mod.mark_run_outbound_blocked, return_value=True),
        )

        # 1) CoS list_files / read_file / copy_file
        cos_list = _cmd_payload(
            await sec_tools_mod.workspace_files_list_cos(COLLEAGUE_ID, NORMAL_WS_ID, None, "tc-1")
        )
        assert cos_list["success"] is True
        assert cos_list["files"] == ["reports/q3.md", "notes/key_drivers.md"]

        for sensitive in (".env", ".env.production", "sub/.hidden.txt", "certs/api.pem", "keys/id_rsa", "gcp/credentials_prod.json"):
            read_p = _cmd_payload(
                await sec_tools_mod.workspace_file_read_cos(COLLEAGUE_ID, NORMAL_WS_ID, sensitive, "tc-r")
            )
            assert read_p == {"success": False, "error": "file not found"}
            copy_p = _cmd_payload(
                await sec_tools_mod.workspace_file_copy_cos(COLLEAGUE_ID, NORMAL_WS_ID, sensitive, None, "tc-c")
            )
            assert copy_p == {"success": False, "error": "file not found"}

        # 2) Analyst colleague_workspace list_files / read_file / copy_file
        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        analyst_cfg = {
            "configurable": {
                "user_id": COLLEAGUE_ID,
                "workspace_id": CUR_WS_ID,
                "workspace_owner_id": COLLEAGUE_ID,
                "origin_channel": "web",
            }
        }
        cw_list = _cmd_payload(
            await _invoke_tool(
                cw_tool,
                {"action": "list_files", "workspace_id": NORMAL_WS_ID},
                config=analyst_cfg,
            )
        )
        assert cw_list["success"] is True
        assert cw_list["count"] == 2
        assert all(".env" not in entry and "credentials" not in entry and "id_rsa" not in entry for entry in cw_list["files"])

        for sensitive in (".env", "certs/tls.key", "gcp/credentials_prod.json"):
            cw_read = _cmd_payload(
                await _invoke_tool(
                    cw_tool,
                    {"action": "read_file", "workspace_id": NORMAL_WS_ID, "path": sensitive},
                    config=analyst_cfg,
                )
            )
            assert cw_read == {"success": False, "error": "file not found"}
            cw_copy = _cmd_payload(
                await _invoke_tool(
                    cw_tool,
                    {"action": "copy_file", "workspace_id": NORMAL_WS_ID, "path": sensitive},
                    config=analyst_cfg,
                )
            )
            assert cw_copy == {"success": False, "error": "file not found"}

        # Normal read still succeeds for both CoS and Analyst
        assert _cmd_payload(
            await sec_tools_mod.workspace_file_read_cos(COLLEAGUE_ID, NORMAL_WS_ID, "notes/key_drivers.md", "tc-ok")
        )["success"] is True
        assert _cmd_payload(
            await _invoke_tool(
                cw_tool,
                {"action": "read_file", "workspace_id": NORMAL_WS_ID, "path": "notes/key_drivers.md"},
                config=analyst_cfg,
            )
        )["success"] is True

        # 3) Web backup_listing & read_from_backup non-owner path filter
        web_files = await backup_reads_mod.backup_listing(
            NORMAL_WS_ID, ".", False, "/home/workspace/NVDA", (), is_non_owner_reader=True
        )
        assert web_files == ["reports/q3.md", "notes/key_drivers.md"]
        with pytest.raises(HTTPException) as exc_web_read:
            await backup_reads_mod.read_from_backup(
                NORMAL_WS_ID,
                NORMAL_SHARED_WS_ROW,
                ".env.production",
                "/home/workspace/NVDA",
                (),
                0,
                100,
                True,
                is_non_owner_reader=True,
            )
        assert exc_web_read.value.status_code == 404


# ===========================================================================
# 3. Copy-file atomic temp write + source re-check + cleanup on revocation
# ===========================================================================


class TestCopyFileRevocationAtomicity:
    def _setup_copy_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        file_bytes: bytes,
        file_path: str = "notes/report.md",
        dest_row: dict[str, Any] = COLLEAGUE_HOME_ROW,
    ) -> PTCSandbox:
        ws_map = {
            NORMAL_WS_ID: NORMAL_SHARED_WS_ROW,
            dest_row["workspace_id"]: dest_row,
        }
        monkeypatch.setattr(
            ws_db_mod,
            "get_workspace",
            create_autospec(
                ws_db_mod.get_workspace,
                side_effect=lambda ws_id: dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None,
            ),
        )
        monkeypatch.setattr(
            cw_mod,
            "get_workspace",
            create_autospec(
                cw_mod.get_workspace,
                side_effect=lambda ws_id: dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None,
            ),
        )
        monkeypatch.setattr(
            persistence_file_mod.FilePersistenceService,
            "get_file_content",
            staticmethod(
                create_autospec(
                    persistence_file_mod.FilePersistenceService.get_file_content,
                    return_value={
                        "path": file_path,
                        "is_binary": False,
                        "file_size": len(file_bytes),
                    },
                )
            ),
        )
        monkeypatch.setattr(
            ws_shared_mod,
            "http_file_bytes",
            create_autospec(ws_shared_mod.http_file_bytes, return_value=file_bytes),
        )
        monkeypatch.setattr(
            redactor_mod,
            "get_vault_secrets_for_redaction",
            create_autospec(redactor_mod.get_vault_secrets_for_redaction, return_value={}),
        )

        import src.server.database.home_workspace as home_ws_mod

        monkeypatch.setattr(
            home_ws_mod,
            "get_home_workspace_id",
            create_autospec(home_ws_mod.get_home_workspace_id, return_value=HOME_WS_ID),
        )

        dest_sandbox = create_autospec(PTCSandbox, instance=True)
        dest_sandbox.aupload_file_bytes.return_value = True
        dest_sandbox.execute_bash_command.return_value = {"success": True, "exit_code": 0}

        @asynccontextmanager
        async def _acquire(ws_id: str, owner_id: str, cause: Any = "files"):
            yield dest_sandbox, dest_row

        monkeypatch.setattr(
            ws_files_svc_mod,
            "acquire_sandbox_to_change",
            create_autospec(
                ws_files_svc_mod.acquire_sandbox_to_change,
                side_effect=_acquire,
            ),
        )
        monkeypatch.setattr(
            ws_files_svc_mod,
            "owner_work_dir",
            create_autospec(
                ws_files_svc_mod.owner_work_dir,
                return_value=f"/home/workspace/{dest_row['dir_name']}",
            ),
        )
        monkeypatch.setattr(
            ws_files_svc_mod,
            "previous_dir_names_of",
            create_autospec(ws_files_svc_mod.previous_dir_names_of, return_value=()),
        )
        monkeypatch.setattr(
            sec_tools_mod,
            "_sibling_folder_named",
            create_autospec(sec_tools_mod._sibling_folder_named, return_value=None),
        )

        async def _contained(sb: Any, rel: str, wd: str, prev=()):
            return f"{wd.rstrip('/')}/{rel.lstrip('/')}"

        monkeypatch.setattr(
            ws_files_svc_mod,
            "contained_target",
            create_autospec(ws_files_svc_mod.contained_target, side_effect=_contained),
        )
        monkeypatch.setattr(
            ws_files_svc_mod,
            "to_client_path",
            create_autospec(
                ws_files_svc_mod.to_client_path,
                side_effect=lambda sb, abs_p, wd: abs_p[len(wd.rstrip("/")) + 1 :],
            ),
        )
        return dest_sandbox

    @pytest.mark.asyncio
    async def test_cos_copy_file_revoked_before_commit_deletes_temp_and_never_renames(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = b"# Shared Research Note\n"
        dest_sandbox = self._setup_copy_env(monkeypatch, file_bytes=raw)

        # Denied: 1st verify passes, 2nd verify (after staging temp file) fails because sharing was revoked
        verify_mock = create_autospec(
            sec_tools_mod.verify_workspace_owner,
            side_effect=[
                None,
                sec_commands_mod.error_command("workspace not found", "tc-revoke"),
            ],
        )
        monkeypatch.setattr(sec_tools_mod, "verify_workspace_owner", verify_mock)

        cmd = await sec_tools_mod.workspace_file_copy_cos(
            user_id=COLLEAGUE_ID,
            workspace_id=NORMAL_WS_ID,
            path="notes/report.md",
            dest_path="shared/report.md",
            tool_call_id="tc-revoke",
        )
        payload = _cmd_payload(cmd)
        assert payload == {"success": False, "error": "workspace not found"}
        assert verify_mock.await_count == 2
        # Uploaded ONLY to temp file under .agents/tmp/colleague-copy-*, never directly to dest
        assert dest_sandbox.aupload_file_bytes.await_count == 1
        tmp_path, uploaded = dest_sandbox.aupload_file_bytes.await_args.args
        assert tmp_path.startswith("/home/workspace/Home/.agents/tmp/colleague-copy-")
        assert uploaded == raw
        # 1st cmd prepares dirs (mkdir -p), 2nd cmd removes temp file; mv never ran
        cmds = [c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list]
        assert len(cmds) == 2
        assert cmds[0].startswith("mkdir -p -- ")
        assert cmds[1].startswith("rm -f -- ") and tmp_path in cmds[1]
        assert not any("mv " in c for c in cmds)

    @pytest.mark.asyncio
    async def test_cos_copy_file_normal_sharing_commits_via_atomic_rename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = b"# Shared Research Note\n"
        dest_sandbox = self._setup_copy_env(monkeypatch, file_bytes=raw)
        events: list[str] = []

        async def _record_verify(*args: Any, **kwargs: Any) -> Any:
            events.append("verify_source")
            return None

        async def _record_upload(*args: Any, **kwargs: Any) -> bool:
            events.append("upload")
            return True

        async def _record_bash(*, command: str, timeout: int = 30) -> dict[str, Any]:
            if command.startswith("mkdir -p -- "):
                events.append("mkdir")
            elif "mv -f -- " in command:
                events.append("mv")
            elif command.startswith("rm -f -- "):
                events.append("rm")
            return {"success": True, "exit_code": 0}

        verify_mock = create_autospec(
            sec_tools_mod.verify_workspace_owner,
            side_effect=_record_verify,
        )
        monkeypatch.setattr(sec_tools_mod, "verify_workspace_owner", verify_mock)
        dest_sandbox.aupload_file_bytes.side_effect = _record_upload
        dest_sandbox.execute_bash_command.side_effect = _record_bash

        cmd = await sec_tools_mod.workspace_file_copy_cos(
            user_id=COLLEAGUE_ID,
            workspace_id=NORMAL_WS_ID,
            path="notes/report.md",
            dest_path="shared/report.md",
            tool_call_id="tc-ok",
        )
        payload = _cmd_payload(cmd)
        assert payload["success"] is True
        assert payload["path"] == "shared/report.md"
        assert verify_mock.await_count == 2
        # Order: initial verify -> mkdir -> upload -> recheck right before mv -> mv
        assert events == ["verify_source", "mkdir", "upload", "verify_source", "mv"]
        tmp_path = dest_sandbox.aupload_file_bytes.await_args.args[0]
        assert tmp_path.startswith("/home/workspace/Home/.agents/tmp/colleague-copy-")
        cmds = [c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list]
        assert len(cmds) == 2
        assert cmds[0].startswith("mkdir -p -- ")
        assert "[ ! -d " in cmds[1] and "mv -f -- " in cmds[1]
        assert "-T" not in cmds[1]
        assert tmp_path in cmds[1] and "/home/workspace/Home/shared/report.md" in cmds[1]

    @pytest.mark.asyncio
    async def test_cos_copy_to_own_workspace_allows_dotdir_regression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression (R2 item 6): non-owner path filter must not tighten owner's own copy destination."""
        raw = b"# Internal Draft Summary\n"
        self._setup_copy_env(monkeypatch, file_bytes=raw)
        verify_mock = create_autospec(
            sec_tools_mod.verify_workspace_owner,
            side_effect=[None, None],
        )
        monkeypatch.setattr(sec_tools_mod, "verify_workspace_owner", verify_mock)

        cmd = await sec_tools_mod.workspace_file_copy_cos(
            user_id=COLLEAGUE_ID,
            workspace_id=NORMAL_WS_ID,
            path="notes/report.md",
            dest_path=".reports/summary.md",
            tool_call_id="tc-own-dotdir",
        )
        payload = _cmd_payload(cmd)
        assert payload["success"] is True
        assert payload["path"] == ".reports/summary.md"

    @pytest.mark.asyncio
    async def test_staging_cleanup_on_upload_error_cancel_and_warns_on_rm_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = b"# Staging Cleanup Test\n"
        dest_sandbox = self._setup_copy_env(monkeypatch, file_bytes=raw)
        monkeypatch.setattr(
            sec_tools_mod,
            "verify_workspace_owner",
            create_autospec(sec_tools_mod.verify_workspace_owner, return_value=None),
        )

        # 1) Staging path excluded from panel (even include_system=True) and backups
        sample_staging = f"{SandboxLayout.TMP_DIR}/colleague-copy-abc123"
        assert served_paths_mod.is_always_hidden_path(sample_staging) is True
        assert served_paths_mod.panel_lists_stored_file(
            sample_staging, include_system=True, include_hidden=True
        ) is False
        assert SandboxLayout.TMP_DIR in BACKUP_EXCLUDE_AGENT_SUBDIRS
        assert SandboxLayout.TMP_DIR in transfer_mod.EXCLUDE_REL_DIRS

        # 2) Upload raises RuntimeError + rm returns non-zero -> cleanup runs AND logs warning
        dest_sandbox.aupload_file_bytes.side_effect = RuntimeError("upload boom")
        dest_sandbox.execute_bash_command.side_effect = [
            {"success": True, "exit_code": 0},  # mkdir
            {"success": False, "exit_code": 1},  # rm fails
        ]
        with caplog.at_level(logging.WARNING):
            cmd = await sec_tools_mod.workspace_file_copy_cos(
                user_id=COLLEAGUE_ID,
                workspace_id=NORMAL_WS_ID,
                path="notes/report.md",
                dest_path="shared/report.md",
                tool_call_id="tc-upload-err",
            )
        assert _cmd_payload(cmd)["success"] is False
        cmds = [c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list]
        assert len(cmds) == 2 and cmds[1].startswith("rm -f -- ")
        assert any("Failed to remove temp copy file" in r.message for r in caplog.records)

        # 3) Upload cancelled (asyncio.CancelledError) -> cleanup still runs before propagating
        dest_sandbox.execute_bash_command.reset_mock()
        dest_sandbox.execute_bash_command.side_effect = [
            {"success": True, "exit_code": 0},  # mkdir
            {"success": True, "exit_code": 0},  # rm
        ]
        dest_sandbox.aupload_file_bytes.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await sec_tools_mod.workspace_file_copy_cos(
                user_id=COLLEAGUE_ID,
                workspace_id=NORMAL_WS_ID,
                path="notes/report.md",
                dest_path="shared/report.md",
                tool_call_id="tc-upload-cancel",
            )
        cmds_cancel = [
            c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list
        ]
        assert len(cmds_cancel) == 2 and cmds_cancel[1].startswith("rm -f -- ")

    @pytest.mark.asyncio
    async def test_analyst_copy_file_revoked_before_commit_deletes_temp_and_normal_commits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = b"# Analyst Copy Note\n"
        dest_sandbox = self._setup_copy_env(
            monkeypatch, file_bytes=raw, dest_row=COLLEAGUE_CUR_WS_ROW
        )
        monkeypatch.setattr(
            dalton_settings_mod,
            "is_dalton_sharing_auth_enabled",
            create_autospec(dalton_settings_mod.is_dalton_sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            cw_mod,
            "mark_run_outbound_blocked",
            create_autospec(cw_mod.mark_run_outbound_blocked, return_value=True),
        )
        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        analyst_cfg = {
            "configurable": {
                "user_id": COLLEAGUE_ID,
                "workspace_id": CUR_WS_ID,
                "workspace_owner_id": COLLEAGUE_ID,
                "origin_channel": "web",
            }
        }

        # Denied: source revoked between initial check and commit re-check
        orig_dual = cw_mod._dual_verify_workspace
        dual_revoke = create_autospec(
            orig_dual,
            side_effect=[
                None,
                sec_commands_mod.error_command("workspace not found", "tc-a1"),
            ],
        )
        monkeypatch.setattr(cw_mod, "_dual_verify_workspace", dual_revoke)

        cmd_revoked = await _invoke_tool(
            cw_tool,
            {"action": "copy_file", "workspace_id": NORMAL_WS_ID, "path": "notes/report.md"},
            config=analyst_cfg,
            call_id="tc-a1",
        )
        assert _cmd_payload(cmd_revoked) == {"success": False, "error": "workspace not found"}
        assert dual_revoke.await_count == 2
        tmp_path = dest_sandbox.aupload_file_bytes.await_args.args[0]
        assert tmp_path.startswith("/home/workspace/Semis/.agents/tmp/colleague-copy-")
        cmds = [c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list]
        assert len(cmds) == 2
        assert cmds[0].startswith("mkdir -p -- ")
        assert cmds[1].startswith("rm -f -- ") and tmp_path in cmds[1]

        # Normal sharing works: both checks succeed -> atomic rename commits without GNU -T
        dest_sandbox.aupload_file_bytes.reset_mock()
        dest_sandbox.execute_bash_command.reset_mock()
        dual_ok = create_autospec(orig_dual, side_effect=[None, None])
        monkeypatch.setattr(cw_mod, "_dual_verify_workspace", dual_ok)

        cmd_ok = await _invoke_tool(
            cw_tool,
            {"action": "copy_file", "workspace_id": NORMAL_WS_ID, "path": "notes/report.md"},
            config=analyst_cfg,
            call_id="tc-a2",
        )
        payload_ok = _cmd_payload(cmd_ok)
        assert payload_ok["success"] is True
        assert payload_ok["path"] == "shared/report.md"
        assert dual_ok.await_count == 2
        cmds_ok = [c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list]
        assert len(cmds_ok) == 2
        assert cmds_ok[0].startswith("mkdir -p -- ")
        assert "[ ! -d " in cmds_ok[1] and "mv -f -- " in cmds_ok[1]
        assert "-T" not in cmds_ok[1]

    @pytest.mark.asyncio
    async def test_real_task_cancel_at_recheck_and_upload_runs_shielded_cleanup_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R4 item 5: genuine Task.cancel() during recheck await (and during in-flight
        upload) must finish shielded upload drain and shielded rm cleanup before
        propagating CancelledError to the caller."""
        raw = b"# Real Task Cancel Test\n"
        orig_verify = sec_tools_mod.verify_workspace_owner
        dest_sandbox = self._setup_copy_env(monkeypatch, file_bytes=raw)

        # 1) Genuine task.cancel() at the recheck await
        recheck_entered = asyncio.Event()
        verify_calls = 0

        async def _verify_hang_on_recheck(*args: Any, **kwargs: Any) -> Any:
            nonlocal verify_calls
            verify_calls += 1
            if verify_calls == 2:
                recheck_entered.set()
                await asyncio.Future()
            return None

        monkeypatch.setattr(
            sec_tools_mod,
            "verify_workspace_owner",
            create_autospec(orig_verify, side_effect=_verify_hang_on_recheck),
        )

        rm_commands: list[str] = []

        async def _bash_for_cancel(*, command: str, timeout: int = 30) -> dict[str, Any]:
            await asyncio.sleep(0)
            if command.startswith("rm -f -- "):
                rm_commands.append(command)
            return {"success": True, "exit_code": 0}

        dest_sandbox.execute_bash_command.side_effect = _bash_for_cancel

        task = asyncio.create_task(
            sec_tools_mod.workspace_file_copy_cos(
                user_id=COLLEAGUE_ID,
                workspace_id=NORMAL_WS_ID,
                path="notes/report.md",
                dest_path="shared/report.md",
                tool_call_id="tc-real-cancel-recheck",
            )
        )
        await recheck_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert len(rm_commands) == 1
        assert "colleague-copy-" in rm_commands[0]
        all_cmds = [c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list]
        assert not any("mv " in c for c in all_cmds)

        # 2) Genuine task.cancel() during an in-flight upload -> waits for upload via shield before rm
        upload_started = asyncio.Event()
        allow_upload_finish = asyncio.Event()
        order: list[str] = []

        async def _slow_upload(tmp_path: str, data: bytes) -> bool:
            upload_started.set()
            await allow_upload_finish.wait()
            order.append("upload_done")
            return True

        async def _bash_order(*, command: str, timeout: int = 30) -> dict[str, Any]:
            if command.startswith("rm -f -- "):
                order.append("rm_done")
            return {"success": True, "exit_code": 0}

        monkeypatch.setattr(
            sec_tools_mod,
            "verify_workspace_owner",
            create_autospec(orig_verify, return_value=None),
        )
        dest_sandbox.aupload_file_bytes.side_effect = _slow_upload
        dest_sandbox.execute_bash_command.reset_mock()
        dest_sandbox.execute_bash_command.side_effect = _bash_order

        upload_cancel_task = asyncio.create_task(
            sec_tools_mod.workspace_file_copy_cos(
                user_id=COLLEAGUE_ID,
                workspace_id=NORMAL_WS_ID,
                path="notes/report.md",
                dest_path="shared/report.md",
                tool_call_id="tc-real-cancel-upload",
            )
        )
        await upload_started.wait()
        upload_cancel_task.cancel()
        await asyncio.sleep(0.02)
        # Cleanup must not have run before the upload finished
        assert order == []
        allow_upload_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await upload_cancel_task
        assert order == ["upload_done", "rm_done"]

        # 3) _remove_temp_copy_file catches CancelledError during rm, completes cleanup, and re-raises
        rm_entered = asyncio.Event()
        rm_finish = asyncio.Event()
        rm_completed = False

        async def _slow_rm(*, command: str, timeout: int = 15) -> dict[str, Any]:
            nonlocal rm_completed
            rm_entered.set()
            await rm_finish.wait()
            rm_completed = True
            return {"success": True, "exit_code": 0}

        dest_sandbox.execute_bash_command.side_effect = _slow_rm
        rm_outer = asyncio.create_task(
            sec_tools_mod._remove_temp_copy_file(dest_sandbox, "/tmp/colleague-copy-shield")
        )
        await rm_entered.wait()
        rm_outer.cancel()
        await asyncio.sleep(0.02)
        rm_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await rm_outer
        assert rm_completed is True

        # 4) Upload drain timeout logs a warning naming the leftover staging path
        monkeypatch.setattr(sec_tools_mod, "_COS_COPY_UPLOAD_DRAIN_TIMEOUT_S", 0.02)
        never_done = asyncio.create_task(asyncio.sleep(10))
        with caplog.at_level(logging.WARNING):
            await sec_tools_mod._drain_cancelled_upload(
                never_done, "/home/workspace/Home/.agents/tmp/colleague-copy-stale", timeout=0.02
            )
        never_done.cancel()
        with pytest.raises(asyncio.CancelledError):
            await never_done
        assert any(
            "/home/workspace/Home/.agents/tmp/colleague-copy-stale" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_analyst_copy_rechecks_dest_before_source_and_mv_immediately_after_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4 item 6 & item 2: analyst copy rechecks dest write first, then source read
        immediately before POSIX rename (no DB query between source recheck and mv, no -T)."""
        raw = b"# Analyst Order Check\n"
        cross_dest_row = {**COLLEAGUE_CUR_WS_ROW, "user_id": OWNER_ID}
        dest_sandbox = self._setup_copy_env(
            monkeypatch, file_bytes=raw, dest_row=cross_dest_row
        )
        monkeypatch.setattr(
            dalton_settings_mod,
            "is_dalton_sharing_auth_enabled",
            create_autospec(dalton_settings_mod.is_dalton_sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            cw_mod,
            "mark_run_outbound_blocked",
            create_autospec(cw_mod.mark_run_outbound_blocked, return_value=True),
        )
        monkeypatch.setattr(
            cw_mod,
            "get_user_tier",
            create_autospec(cw_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            cw_mod,
            "classify_owner_tier",
            create_autospec(cw_mod.classify_owner_tier, return_value="internal"),
        )

        events: list[str] = []
        dual_calls = 0
        req_calls = 0

        async def _rec_dual(*args: Any, **kwargs: Any) -> Any:
            nonlocal dual_calls
            dual_calls += 1
            events.append("init_source" if dual_calls == 1 else "recheck_source")
            return None

        async def _rec_req(*args: Any, **kwargs: Any) -> Any:
            nonlocal req_calls
            req_calls += 1
            events.append("init_dest" if req_calls == 1 else "recheck_dest")
            return cross_dest_row

        async def _rec_upload(*args: Any, **kwargs: Any) -> bool:
            events.append("upload")
            return True

        async def _rec_bash(*, command: str, timeout: int = 30) -> dict[str, Any]:
            if command.startswith("mkdir -p -- "):
                events.append("mkdir")
            elif "mv -f -- " in command:
                assert "-T" not in command
                events.append("mv")
            return {"success": True, "exit_code": 0}

        monkeypatch.setattr(
            cw_mod,
            "_dual_verify_workspace",
            create_autospec(cw_mod._dual_verify_workspace, side_effect=_rec_dual),
        )
        monkeypatch.setattr(
            cw_mod,
            "require_workspace_access",
            create_autospec(cw_mod.require_workspace_access, side_effect=_rec_req),
        )
        dest_sandbox.aupload_file_bytes.side_effect = _rec_upload
        dest_sandbox.execute_bash_command.side_effect = _rec_bash

        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        cross_cfg = {
            "configurable": {
                "user_id": COLLEAGUE_ID,
                "workspace_id": CUR_WS_ID,
                "workspace_owner_id": OWNER_ID,
                "origin_channel": "web",
            }
        }
        cmd = await _invoke_tool(
            cw_tool,
            {"action": "copy_file", "workspace_id": NORMAL_WS_ID, "path": "notes/report.md"},
            config=cross_cfg,
            call_id="tc-a-order",
        )
        assert _cmd_payload(cmd)["success"] is True
        # Dest write recheck happens BEFORE source read recheck, and mv immediately follows source recheck
        assert events == [
            "init_dest",
            "init_source",
            "mkdir",
            "upload",
            "recheck_dest",
            "recheck_source",
            "mv",
        ]

    @pytest.mark.asyncio
    async def test_analyst_copy_file_dest_write_revoked_right_before_rename_deletes_temp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = b"# Analyst Dest Revoke Note\n"
        cross_dest_row = {
            **COLLEAGUE_CUR_WS_ROW,
            "user_id": OWNER_ID,
        }
        dest_sandbox = self._setup_copy_env(
            monkeypatch, file_bytes=raw, dest_row=cross_dest_row
        )
        monkeypatch.setattr(
            dalton_settings_mod,
            "is_dalton_sharing_auth_enabled",
            create_autospec(dalton_settings_mod.is_dalton_sharing_auth_enabled, return_value=True),
        )
        monkeypatch.setattr(
            cw_mod,
            "mark_run_outbound_blocked",
            create_autospec(cw_mod.mark_run_outbound_blocked, return_value=True),
        )
        monkeypatch.setattr(
            cw_mod,
            "get_user_tier",
            create_autospec(cw_mod.get_user_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            cw_mod,
            "classify_owner_tier",
            create_autospec(cw_mod.classify_owner_tier, return_value="internal"),
        )
        monkeypatch.setattr(
            cw_mod,
            "_dual_verify_workspace",
            create_autospec(cw_mod._dual_verify_workspace, side_effect=[None, None]),
        )
        # 1st require_workspace_access (initial dest write check) passes;
        # 2nd (pre-rename dest write recheck) raises 403; 3rd (read check) passes -> read-only error
        req_dest_mock = create_autospec(
            cw_mod.require_workspace_access,
            side_effect=[
                cross_dest_row,
                HTTPException(status_code=403, detail="Forbidden"),
                cross_dest_row,
            ],
        )
        monkeypatch.setattr(cw_mod, "require_workspace_access", req_dest_mock)
        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        cross_cfg = {
            "configurable": {
                "user_id": COLLEAGUE_ID,
                "workspace_id": CUR_WS_ID,
                "workspace_owner_id": OWNER_ID,
                "origin_channel": "web",
            }
        }
        cmd_dest_revoked = await _invoke_tool(
            cw_tool,
            {"action": "copy_file", "workspace_id": NORMAL_WS_ID, "path": "notes/report.md"},
            config=cross_cfg,
            call_id="tc-a-dest-revoked",
        )
        assert _cmd_payload(cmd_dest_revoked)["success"] is False
        assert "只读权限" in _cmd_payload(cmd_dest_revoked)["error"]
        cmds_dest = [
            c.kwargs["command"] for c in dest_sandbox.execute_bash_command.await_args_list
        ]
        assert len(cmds_dest) == 2
        assert cmds_dest[0].startswith("mkdir -p -- ")
        assert cmds_dest[1].startswith("rm -f -- ")
        assert not any("mv " in c for c in cmds_dest)


# ===========================================================================
# 4. Binary copy secret checks (macro Office refusal, OOXML XML scan, images/PDF)
# ===========================================================================


class TestBinaryCopySecretChecks:
    @pytest.mark.parametrize(
        "macro_file",
        ["model.xlsm", "MODEL.XLSM", "memo.docm", "deck.pptm", "addin.xlam"],
    )
    def test_macro_office_files_always_refused_with_explanation(
        self, macro_file: str
    ) -> None:
        clean_zip = _make_ooxml_zip({"xl/workbook.xml": "<workbook/>"})
        err = sec_tools_mod._scan_copy_bytes_for_secrets(macro_file, clean_zip, {})
        assert err == sec_tools_mod._COS_MACRO_OFFICE_REFUSED_ERROR
        assert "带宏" in err
        assert sec_tools_mod._is_allowed_copy_extension(macro_file) is False

    def test_disguised_xlsx_carrying_vba_macro_project_is_refused(self) -> None:
        ole_vba = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128
        disguised = _make_ooxml_zip(
            {"xl/workbook.xml": "<workbook/>", "xl/vbaProject.bin": ole_vba}
        )
        err = sec_tools_mod._scan_copy_bytes_for_secrets("model.xlsx", disguised, {})
        assert err == sec_tools_mod._COS_MACRO_OFFICE_REFUSED_ERROR

    @pytest.mark.parametrize(
        ("filename", "xml_member", "secret_text"),
        [
            (
                "memo.docx",
                "word/document.xml",
                "<w:document><w:t>token=gxsa_live_token_1234567890</w:t></w:document>",
            ),
            (
                "model.xlsx",
                "xl/sharedStrings.xml",
                "<sst><si><t>sk-ant-oat01-secretSetupToken12345</t></si></sst>",
            ),
            (
                "deck.pptx",
                "ppt/slides/slide1.xml",
                "<p:sld><a:t>escaped=gxsa&#95;escaped&#95;token12345</a:t></p:sld>",
            ),
        ],
    )
    def test_ooxml_xml_parts_checked_with_same_known_secret_redactor_as_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        filename: str,
        xml_member: str,
        secret_text: str,
    ) -> None:
        # Even with empty vault_secrets={}, known platform/sandbox/setup tokens in XML are rejected
        redactor = SecretRedactor.__new__(SecretRedactor)
        redactor._secrets = [("GLOBAL_API_KEY", "platform-global-secret-xyz")]
        monkeypatch.setattr(
            redactor_mod,
            "get_redactor",
            create_autospec(redactor_mod.get_redactor, return_value=redactor),
        )
        ooxml = _make_ooxml_zip({xml_member: secret_text})
        err = sec_tools_mod._scan_copy_bytes_for_secrets(filename, ooxml, {})
        assert err is not None
        assert "密钥" in err or "凭据" in err

    def test_ooxml_xml_global_platform_secret_refused_and_clean_files_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        redactor = SecretRedactor.__new__(SecretRedactor)
        redactor._secrets = [("MCP_FMP_KEY", "fmp-platform-secret-987654321")]
        monkeypatch.setattr(
            redactor_mod,
            "get_redactor",
            create_autospec(redactor_mod.get_redactor, return_value=redactor),
        )

        # Denied: global platform secret inside .xlsx XML even when vault_secrets is empty
        dirty_xlsx = _make_ooxml_zip(
            {"xl/sharedStrings.xml": "<sst><si><t>fmp-platform-secret-987654321</t></si></sst>"}
        )
        assert sec_tools_mod._scan_copy_bytes_for_secrets("model.xlsx", dirty_xlsx, {}) is not None

        # Normal sharing works: clean .docx, .xlsx, .pptx, PNG image, and scanned-style PDF all pass
        for clean_name, member in (
            ("memo.docx", "word/document.xml"),
            ("model.xlsx", "xl/sharedStrings.xml"),
            ("deck.pptx", "ppt/slides/slide1.xml"),
        ):
            clean_ooxml = _make_ooxml_zip({member: "<root><t>NVDA Q3 FY26 revenue $35.1B</t></root>"})
            assert sec_tools_mod._scan_copy_bytes_for_secrets(clean_name, clean_ooxml, {}) is None

        assert sec_tools_mod._scan_copy_bytes_for_secrets("chart.png", _make_minimal_png(), {}) is None

        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = peak if sys.platform == "darwin" else peak * 1024
        monkeypatch.setattr(
            sec_tools_mod,
            "_COS_SCAN_MAX_RSS_BYTES",
            peak_bytes + sec_tools_mod._COS_SCAN_MAX_RSS_BYTES,
        )
        assert (
            sec_tools_mod._scan_copy_bytes_for_secrets(
                "scanned.pdf", _make_scanned_image_pdf(), {}
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_cos_copy_file_refuses_macro_office_before_upload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_map = {
            NORMAL_WS_ID: {**NORMAL_SHARED_WS_ROW, "status": "running"},
            HOME_WS_ID: COLLEAGUE_HOME_ROW,
        }
        monkeypatch.setattr(
            sec_tools_mod,
            "verify_workspace_owner",
            create_autospec(sec_tools_mod.verify_workspace_owner, return_value=None),
        )
        monkeypatch.setattr(
            ws_db_mod,
            "get_workspace",
            create_autospec(
                ws_db_mod.get_workspace,
                side_effect=lambda ws_id: dict(ws_map[str(ws_id)]) if str(ws_id) in ws_map else None,
            ),
        )
        import src.server.database.home_workspace as home_ws_mod

        monkeypatch.setattr(
            home_ws_mod,
            "get_home_workspace_id",
            create_autospec(home_ws_mod.get_home_workspace_id, return_value=HOME_WS_ID),
        )
        dest_sandbox = create_autospec(PTCSandbox, instance=True)

        @asynccontextmanager
        async def _acquire(ws_id: str, owner_id: str, cause: Any = "files"):
            yield dest_sandbox, ws_map[str(ws_id)]

        monkeypatch.setattr(
            ws_files_svc_mod,
            "acquire_sandbox_to_change",
            create_autospec(
                ws_files_svc_mod.acquire_sandbox_to_change,
                side_effect=_acquire,
            ),
        )
        monkeypatch.setattr(
            ws_files_crud_mod,
            "_read_contained_target",
            create_autospec(
                ws_files_crud_mod._read_contained_target,
                return_value=ContainedFile("/home/workspace/NVDA/model.xlsm", 10, b"PK\x03\x04"),
            ),
        )

        for ext in (".xlsm", ".docm", ".pptm", ".xlam"):
            cmd = await sec_tools_mod.workspace_file_copy_cos(
                user_id=COLLEAGUE_ID,
                workspace_id=NORMAL_WS_ID,
                path=f"models/book{ext}",
                dest_path=None,
                tool_call_id="tc-macro",
            )
            payload = _cmd_payload(cmd)
            assert payload["success"] is False
            assert payload["error"] == sec_tools_mod._COS_MACRO_OFFICE_REFUSED_ERROR
        cmd_ppt = await sec_tools_mod.workspace_file_copy_cos(
            user_id=COLLEAGUE_ID,
            workspace_id=NORMAL_WS_ID,
            path="decks/legacy.ppt",
            dest_path=None,
            tool_call_id="tc-ppt",
        )
        payload_ppt = _cmd_payload(cmd_ppt)
        assert payload_ppt["success"] is False
        assert payload_ppt["error"] == sec_tools_mod._COS_LEGACY_PPT_REFUSED_ERROR
        dest_sandbox.aupload_file_bytes.assert_not_called()

    @pytest.mark.parametrize(
        ("filename", "clean_stream", "macro_entry"),
        [
            ("legacy.xls", "Workbook", ("_VBA_PROJECT_CUR", 1)),
            ("legacy.doc", "WordDocument", ("Macros", 1)),
        ],
    )
    def test_legacy_ole_office_macro_refused_and_macro_free_allowed(
        self,
        filename: str,
        clean_stream: str,
        macro_entry: tuple[str, int],
    ) -> None:
        # Clean OLE file (even with body text mentioning "Macros" / "VBA") is allowed
        clean_ole = _make_ole2_bytes(
            [(clean_stream, 2)],
            extra_body="Macroeconomics and VBA discussion".encode("utf-16-le"),
        )
        assert sec_tools_mod._scan_copy_bytes_for_secrets(filename, clean_ole, {}) is None

        # Macro-bearing OLE file is refused with the macro error
        macro_ole = _make_ole2_bytes([(clean_stream, 2), macro_entry])
        err = sec_tools_mod._scan_copy_bytes_for_secrets(filename, macro_ole, {})
        assert err == sec_tools_mod._COS_MACRO_OFFICE_REFUSED_ERROR

    def test_legacy_ppt_always_refused_with_exact_guidance(self) -> None:
        """R4 item 1: .ppt VBA projects can live compressed inside PowerPoint Document,
        so .ppt is always refused regardless of outer OLE directory entries."""
        expected_msg = (
            "老格式 PowerPoint（.ppt）可能内嵌宏，无法检查。请让对方另存为 .pptx 后再复制。"
        )
        clean_ppt = _make_ole2_bytes([("PowerPoint Document", 2)])
        assert sec_tools_mod._scan_copy_bytes_for_secrets("deck.ppt", clean_ppt, {}) == expected_msg
        assert sec_tools_mod._scan_copy_bytes_for_secrets("DECK.PPT", clean_ppt, {}) == expected_msg
        assert sec_tools_mod._is_allowed_copy_extension("deck.ppt") is False
        assert ".ppt" in (cos_mod.chief_of_staff_workspaces.description or "")
        assert "refused" in (cos_mod.chief_of_staff_workspaces.description or "")
        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        assert ".ppt" in (cw_tool.description or "")
        assert "拒绝" in (cw_tool.description or "")

    def test_presentation_renamed_to_xls_or_doc_is_refused_like_ppt(self) -> None:
        renamed_ppt = (
            sec_tools_mod._OLE_HEADER
            + b"\x00" * 64
            + "PowerPoint Document".encode("utf-16-le")
            + b"\x00\x00"
        )
        for name in ("deck.xls", "deck.doc"):
            assert (
                sec_tools_mod._scan_copy_bytes_for_secrets(name, renamed_ppt, {})
                == sec_tools_mod._COS_LEGACY_PPT_REFUSED_ERROR
            )

    def test_body_text_naming_powerpoint_document_is_not_a_presentation(self) -> None:
        mention = (
            sec_tools_mod._OLE_HEADER
            + b"\x00" * 64
            + "Please open the PowerPoint Document attached.".encode("utf-16-le")
        )
        for name in ("memo.doc", "sheet.xls"):
            assert sec_tools_mod._scan_copy_bytes_for_secrets(name, mention, {}) is None

    def test_stored_member_bytes_resembling_central_entries_do_not_count_as_members(
        self,
    ) -> None:
        noisy = _make_ooxml_zip(
            {"xl/media/image1.png": b"PK\x01\x02" * 2100},
            compression=zipfile.ZIP_STORED,
        )
        assert sec_tools_mod._count_zip_central_entries(noisy, 2000) == 2
        assert sec_tools_mod._scan_copy_bytes_for_secrets("book.xlsx", noisy, {}) is None

    def test_powerpoint_stream_found_in_a_real_ole_directory_not_in_body_text(self) -> None:
        presentation = _make_ole2_bytes([("PowerPoint Document", 2), ("Current User", 2)])
        assert sec_tools_mod._ole_has_powerpoint_stream(presentation) is True
        mention = _make_ole2_bytes(
            [("WordDocument", 2)],
            extra_body="see the PowerPoint Document".encode("utf-16-le") + b"\x00\x00",
        )
        assert sec_tools_mod._ole_has_powerpoint_stream(mention) is False
        for name in ("deck.xls", "deck.doc"):
            assert (
                sec_tools_mod._scan_copy_bytes_for_secrets(name, presentation, {})
                == sec_tools_mod._COS_LEGACY_PPT_REFUSED_ERROR
            )
        assert sec_tools_mod._scan_copy_bytes_for_secrets("memo.doc", mention, {}) is None

    def test_zip64_directory_is_found_under_the_pre_3_13_10_end_record_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        z64 = _make_ooxml_zip({"word/document.xml": "<w:document/>"})
        monkeypatch.undo()
        real_end_rec_data = zipfile._EndRecData

        def _old_end_rec_data(fpin):  # noqa: ANN001 - mirrors zipfile's private signature
            endrec = real_end_rec_data(fpin)
            if endrec and endrec[zipfile._ECD_SIGNATURE] == zipfile.stringEndArchive64:
                # Before 3.13.10 / 3.14.1 the location stayed at the classic end record.
                endrec = list(endrec)
                endrec[zipfile._ECD_LOCATION] += (
                    zipfile.sizeEndCentDir64 + zipfile.sizeEndCentDir64Locator
                )
            return endrec

        monkeypatch.setattr(zipfile, "_EndRecData", _old_end_rec_data)
        assert sec_tools_mod._count_zip_central_entries(z64, 2000) == 2

    def test_central_entry_running_past_the_directory_is_malformed(self) -> None:
        good = bytearray(_make_ooxml_zip({"word/document.xml": "<w:document/>"}))
        last = good.rfind(b"PK\x01\x02")
        struct.pack_into("<H", good, last + 32, 12)  # comment length the directory lacks
        assert sec_tools_mod._count_zip_central_entries(bytes(good), 2000) is None

    def test_zip64_ooxml_with_a_real_central_directory_is_counted_and_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make the writer emit ZIP64 end records for a small archive.
        monkeypatch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        z64 = _make_ooxml_zip({"word/document.xml": "<w:document/>"})
        monkeypatch.undo()
        assert b"PK\x06\x06" in z64
        assert sec_tools_mod._count_zip_central_entries(z64, 2000) == 2
        assert sec_tools_mod._scan_copy_bytes_for_secrets("report.docx", z64, {}) is None

    def test_legacy_ole_utf16le_fallback_catches_macro_in_truncated_container(self) -> None:
        truncated_macro = (
            sec_tools_mod._OLE_HEADER
            + b"\x00" * 64
            + "_VBA_PROJECT".encode("utf-16-le")
            + b"\x00\x00"
        )
        assert (
            sec_tools_mod._scan_copy_bytes_for_secrets("broken.xls", truncated_macro, {})
            == sec_tools_mod._COS_MACRO_OFFICE_REFUSED_ERROR
        )

    def test_ooxml_understated_eocd_count_is_refused_before_zipfile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An end record claiming few entries over a central directory holding
        more than the limit is refused before ZipFile parses that directory."""
        many_members = {f"xl/m{i}.xml": "<a/>" for i in range(2001)}
        real = bytearray(_make_ooxml_zip(many_members))
        eocd = real.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", real, eocd + 8, 5, 5)
        understated = bytes(real)
        assert sec_tools_mod._read_zip_eocd_member_count(understated) == 5

        opened: list[object] = []
        real_zipfile = zipfile.ZipFile

        def _tracking_zipfile(*args: object, **kwargs: object) -> zipfile.ZipFile:
            opened.append(args)
            return real_zipfile(*args, **kwargs)

        monkeypatch.setattr(zipfile, "ZipFile", _tracking_zipfile)
        err = sec_tools_mod._scan_copy_bytes_for_secrets("many.xlsx", understated, {})
        assert err == "压缩容器成员数量超出安全上限，不能拷贝"
        assert opened == []

    @pytest.mark.asyncio
    async def test_upload_finishing_after_the_drain_still_has_its_staging_file_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        removed: list[str] = []

        async def _fake_remove(sandbox: object, tmp_abs: str) -> None:
            removed.append(tmp_abs)

        monkeypatch.setattr(
            sec_tools_mod,
            "_remove_temp_copy_file",
            create_autospec(sec_tools_mod._remove_temp_copy_file, side_effect=_fake_remove),
        )
        finish = asyncio.Event()

        async def _slow_upload() -> None:
            await finish.wait()

        upload = asyncio.create_task(_slow_upload())
        tmp = "/home/workspace/Home/.agents/tmp/colleague-copy-late"
        sec_tools_mod._remove_after_upload(upload, object(), tmp)
        await asyncio.sleep(0)
        assert removed == []
        finish.set()
        await upload
        for _ in range(5):
            await asyncio.sleep(0)
        assert removed == [tmp]

    def test_ooxml_eocd_precheck_standard_zip64_missing_and_nonempty_dir_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4 item 7: EOCD (including ZIP64) member count is checked BEFORE opening
        ZipFile; unreadable EOCD is refused; dir entries ending in '/' with
        file_size > 0 are refused as format anomalies."""
        # 1) Build standard ZIP with 2001 entries and verify ZipFile is NEVER opened
        many_members = {f"xl/m{i}.xml": "<a/>" for i in range(2001)}
        too_many_zip = _make_ooxml_zip(many_members)

        # 2) Build synthetic ZIP64 EOCD declaring 2500 entries (no central dir needed)
        z64_offset = 4
        z64_eocd = struct.pack(
            "<4sQHHIIQQQQ",
            b"PK\x06\x06",
            44,       # size of remaining record
            45, 45,   # versions
            0, 0,     # disks
            2500,     # disk entries
            2500,     # total entries
            0, 0,     # cd size / offset
        )
        z64_locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, z64_offset, 1)
        std_eocd = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0, 0,
            0xFFFF, 0xFFFF,
            0xFFFFFFFF, 0xFFFFFFFF,
            0,
        )
        zip64_too_many = b"PK\x03\x04" + z64_eocd + z64_locator + std_eocd
        assert sec_tools_mod._read_zip_eocd_member_count(zip64_too_many) == 2500

        # Valid small ZIP64 EOCD returns exact count
        z64_small_eocd = struct.pack(
            "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 3, 3, 0, 0
        )
        zip64_small = b"PK\x03\x04" + z64_small_eocd + z64_locator + std_eocd
        assert sec_tools_mod._read_zip_eocd_member_count(zip64_small) == 3

        # Missing EOCD returns None
        no_eocd = b"PK\x03\x04" + b"\x00" * 64
        assert sec_tools_mod._read_zip_eocd_member_count(no_eocd) is None

        # Monkeypatch zipfile.ZipFile to fail if opened; all three must be refused BEFORE ZipFile
        def _forbid_zipfile(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("zipfile.ZipFile must not be opened before EOCD passes")

        monkeypatch.setattr(zipfile, "ZipFile", _forbid_zipfile)
        err_std = sec_tools_mod._scan_copy_bytes_for_secrets("many.xlsx", too_many_zip, {})
        assert err_std is not None and "成员数量" in err_std
        err_z64 = sec_tools_mod._scan_copy_bytes_for_secrets("many64.xlsx", zip64_too_many, {})
        assert err_z64 is not None and "成员数量" in err_z64
        err_no_eocd = sec_tools_mod._scan_copy_bytes_for_secrets("broken.xlsx", no_eocd, {})
        assert err_no_eocd is not None

        monkeypatch.undo()

        # 3) Directory member ending with '/' but file_size > 0 is refused as format anomaly
        buf_bad_dir = io.BytesIO()
        with zipfile.ZipFile(buf_bad_dir, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", "<w:document/>")
            zf.writestr("word/hidden/", b"secret payload disguised as dir")
        err_dir_data = sec_tools_mod._scan_copy_bytes_for_secrets(
            "bad_dir.docx", buf_bad_dir.getvalue(), {}
        )
        assert err_dir_data is not None and "格式异常" in err_dir_data

        # 4) Normal empty directory member ending with '/' (file_size == 0) is still allowed
        buf_empty_dir = io.BytesIO()
        with zipfile.ZipFile(buf_empty_dir, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", "<w:document/>")
            zf.writestr("word/empty/", b"")
        assert (
            sec_tools_mod._scan_copy_bytes_for_secrets(
                "clean_dir.docx", buf_empty_dir.getvalue(), {}
            )
            is None
        )

    def test_ooxml_resource_limits_compression_members_streaming_bytes_and_non_xml_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        redactor = SecretRedactor.__new__(SecretRedactor)
        redactor._secrets = [("GLOBAL_SECRET_KEY", "platform-nonxml-secret-555555")]
        monkeypatch.setattr(
            redactor_mod,
            "get_redactor",
            create_autospec(redactor_mod.get_redactor, return_value=redactor),
        )

        # 1) Stored & Deflated allowed; BZIP2 & LZMA refused
        stored = _make_ooxml_zip({"xl/workbook.xml": "<w/>"}, compression=zipfile.ZIP_STORED)
        deflated = _make_ooxml_zip({"xl/workbook.xml": "<w/>"}, compression=zipfile.ZIP_DEFLATED)
        assert sec_tools_mod._scan_copy_bytes_for_secrets("ok.xlsx", stored, {}) is None
        assert sec_tools_mod._scan_copy_bytes_for_secrets("ok.xlsx", deflated, {}) is None

        for bad_comp in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            bad_zip = _make_ooxml_zip({"xl/workbook.xml": "<w/>"}, compression=bad_comp)
            err = sec_tools_mod._scan_copy_bytes_for_secrets("bad.xlsx", bad_zip, {})
            assert err is not None and "压缩方式" in err

        # 2) Member count > 2000 refused
        many_members = {f"xl/m{i}.xml": "<a/>" for i in range(2001)}
        too_many_zip = _make_ooxml_zip(many_members)
        err_members = sec_tools_mod._scan_copy_bytes_for_secrets("many.xlsx", too_many_zip, {})
        assert err_members is not None and "成员数量" in err_members

        # 3) Actual decompressed total > cap stops & refuses (use small cap via monkeypatch)
        monkeypatch.setattr(sec_tools_mod, "_COS_ZIP_TOTAL_MAX_BYTES", 200_000)
        monkeypatch.setattr(sec_tools_mod, "_COS_ZIP_MEMBER_MAX_BYTES", 150_000)
        over_total_zip = _make_ooxml_zip(
            {"xl/a.xml": "A" * 120_000, "xl/b.xml": "B" * 120_000}
        )
        err_total = sec_tools_mod._scan_copy_bytes_for_secrets("bomb.xlsx", over_total_zip, {})
        assert err_total is not None and "解压总量" in err_total

        # 4) Non-XML UTF-8 text member also checked with known-secret redactor
        dirty_non_xml = _make_ooxml_zip(
            {
                "xl/workbook.xml": "<workbook/>",
                "docProps/custom_note.txt": "key=platform-nonxml-secret-555555",
            }
        )
        err_non_xml = sec_tools_mod._scan_copy_bytes_for_secrets("note.xlsx", dirty_non_xml, {})
        assert err_non_xml is not None and ("密钥" in err_non_xml or "凭据" in err_non_xml)

        # Tool docstrings no longer advertise .xlsm as supported
        assert ".xlsm" not in (cos_mod.chief_of_staff_workspaces.description or "")
        cw_tool = cw_mod.create_colleague_workspace_tools()[0]
        assert ".xlsm" not in (cw_tool.description or "")
