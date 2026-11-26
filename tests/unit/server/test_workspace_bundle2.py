"""Unit tests for Bundle 2 server workspace fixes: BO2, E1, E3."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.dalton.coverage import dedup as dedup_mod
from src.server.app import workspaces as workspaces_app
from src.server.dependencies.principal import Principal
from src.server.models.computer import ComputerStatus
from src.server.models.workspace import WorkspaceCreate
from src.server.services.computer_errors import ComputerArchivingError
from src.server.services.workspace_manager import WorkspaceManager

USER_ID = "user-server-b2"
COMP_ID = "11111111-1111-4111-8111-111111111111"
WS_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_computer", [True, False])
async def test_bo2_archiving_computer_mid_create_maps_to_409_computer_archiving(
    monkeypatch: pytest.MonkeyPatch,
    explicit_computer: bool,
) -> None:
    """BO2: when a computer enters archiving after precheck and before the DB insert,
    both explicit computer_id and default primary raise ComputerArchivingError -> HTTP 409."""
    from src.server.services import workspace_manager as wm_mod

    healthy_comp = {
        "computer_id": COMP_ID,
        "user_id": USER_ID,
        "name": "Primary Cloud",
        "kind": "docker",
        "status": ComputerStatus.RUNNING,
        "provider_ref": "ctr-1",
        "config": {},
        "is_primary": True,
    }
    archiving_comp = {**healthy_comp, "config": {"archiving": True}}

    # Simulate transaction observing archiving under FOR SHARE (populating reject_reason_out)
    # EVEN IF archiving finishes and clears the flag before any subsequent get_computer read!
    async def _create_rejected_by_archiving(*_a: Any, **kwargs: Any) -> Any:
        out = kwargs.get("reject_reason_out")
        if out is not None:
            out.append("archiving")
        return None

    monkeypatch.setattr(
        wm_mod,
        "get_computer",
        create_autospec(wm_mod.get_computer, return_value=dict(healthy_comp)),
    )
    monkeypatch.setattr(
        wm_mod,
        "create_workspace_on_computer",
        create_autospec(wm_mod.create_workspace_on_computer, side_effect=_create_rejected_by_archiving),
    )
    monkeypatch.setattr(wm_mod, "server_owned_config_keys", lambda: frozenset())

    mgr = WorkspaceManager.__new__(WorkspaceManager)
    monkeypatch.setattr(
        mgr,
        "ensure_primary_computer",
        create_autospec(mgr.ensure_primary_computer, return_value=dict(healthy_comp)),
    )

    with pytest.raises(ComputerArchivingError):
        await mgr.create_workspace(
            user_id=USER_ID,
            name="NVDA",
            computer_id=COMP_ID if explicit_computer else None,
        )

    # Verify REST route maps ComputerArchivingError to HTTP 409 computer_archiving.
    monkeypatch.setattr(
        workspaces_app.cli_approval,
        "workspace_create",
        create_autospec(workspaces_app.cli_approval.workspace_create, return_value=None),
    )
    monkeypatch.setattr(
        dedup_mod,
        "find_existing_company_workspace",
        create_autospec(dedup_mod.find_existing_company_workspace, return_value=None),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: mgr),
    )
    principal = Principal(user_id=USER_ID, auth="session")
    http_req = Request({"type": "http", "headers": [], "app": object()})
    req = WorkspaceCreate(
        name="NVDA",
        computer_id=UUID(COMP_ID) if explicit_computer else None,
    )
    with pytest.raises(HTTPException) as exc:
        await workspaces_app.create_workspace(req, principal, http_req)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "computer_archiving"


@pytest.mark.asyncio
async def test_e1_web_workspace_create_dedup_overall_timeout_warns_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E1: web workspace create enforces WEB_DEDUP_OVERALL_TIMEOUT_SECONDS and returns
    dedup_warning_code='dedup_unavailable' with a clear message instead of hanging."""
    now = datetime.now(timezone.utc)
    created_ws = {
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "name": "Tencent",
        "description": "",
        "status": "stopped",
        "computer_id": COMP_ID,
        "dir_name": "Tencent",
        "created_at": now,
        "updated_at": now,
        "config": {},
    }

    async def _slow_dedup(*_a: Any, **_kw: Any) -> Any:
        await asyncio.sleep(5)
        return None

    monkeypatch.setattr(dedup_mod, "WEB_DEDUP_OVERALL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(
        dedup_mod,
        "find_existing_company_workspace",
        create_autospec(dedup_mod.find_existing_company_workspace, side_effect=_slow_dedup),
    )
    monkeypatch.setattr(
        workspaces_app.cli_approval,
        "workspace_create",
        create_autospec(workspaces_app.cli_approval.workspace_create, return_value=None),
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = created_ws
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: mgr),
    )

    principal = Principal(user_id=USER_ID, auth="session")
    http_req = Request({"type": "http", "headers": [], "app": object()})
    resp = await workspaces_app.create_workspace(
        WorkspaceCreate(name="Tencent"),
        principal,
        http_req,
    )
    assert resp.workspace_id == WS_ID
    assert resp.dedup_warning_code == dedup_mod.WEB_DEDUP_UNAVAILABLE_CODE
    assert resp.dedup_warning == dedup_mod.WEB_DEDUP_UNAVAILABLE_MESSAGE
    mgr.create_workspace.assert_awaited_once()


def test_e3_workspace_create_ignores_wrong_types_on_coverage_fields_without_422() -> None:
    """E3: invalid types on coverage_kind / ticker / display_name / skip_coverage_suggestion
    are coerced/ignored instead of rejecting workspace creation with 422."""
    model = WorkspaceCreate.model_validate(
        {
            "name": "Macro Notes",
            "coverage_kind": 12345,
            "ticker": ["NVDA"],
            "display_name": {"nested": "bad"},
            "skip_coverage_suggestion": "not-a-bool",
        }
    )
    assert model.name == "Macro Notes"
    assert model.coverage_kind is None
    assert model.ticker is None
    assert model.display_name is None
    assert model.skip_coverage_suggestion is False

    # Valid typed values continue to pass through intact.
    valid = WorkspaceCreate.model_validate(
        {
            "name": "NVDA",
            "coverage_kind": "company",
            "ticker": "NVDA",
            "display_name": "NVIDIA",
            "skip_coverage_suggestion": True,
        }
    )
    assert valid.coverage_kind == "company"
    assert valid.ticker == "NVDA"
    assert valid.display_name == "NVIDIA"
    assert valid.skip_coverage_suggestion is True


@pytest.mark.asyncio
async def test_bo2_r2_db_create_workspace_on_computer_records_archiving_reason_under_share_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BO2 r2: create_workspace_on_computer checks archiving under FOR SHARE inside the
    transaction and populates reject_reason_out=['archiving'] before releasing the lock."""
    from contextlib import asynccontextmanager
    from src.server.database import workspace as ws_db

    executed_sql: list[str] = []

    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            executed_sql.append(" ".join(sql.split()))

        async def fetchone(self) -> dict[str, Any] | None:
            return {"config": {"archiving": True}}

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return False

    class _Conn:
        def cursor(self, **_kw: Any) -> _Cur:
            return _Cur()

        def transaction(self) -> _Tx:
            return _Tx()

    @asynccontextmanager
    async def _folder_alloc(_cid: str, conn: Any = None):
        yield conn or _Conn()

    monkeypatch.setattr(ws_db, "folder_allocation", _folder_alloc)
    monkeypatch.setattr(
        ws_db,
        "lock_user_writes",
        create_autospec(ws_db.lock_user_writes, return_value=None),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace_dir_names_for_computer",
        create_autospec(ws_db.get_workspace_dir_names_for_computer, return_value=()),
    )
    monkeypatch.setattr(
        ws_db,
        "get_computer",
        create_autospec(ws_db.get_computer, return_value={"config": {}}),
    )

    reasons: list[str] = []
    res = await ws_db.create_workspace_on_computer(
        USER_ID,
        "NVDA",
        COMP_ID,
        reject_reason_out=reasons,
    )
    assert res is None
    assert reasons == ["archiving"]
    assert any("FOR SHARE" in sql and "SELECT config FROM computers" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_e1_r2_internal_model_15s_deadline_surfaces_dedup_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E1 r2: when the internal 15s dedup model deadline fires inside
    find_existing_company_workspace, web workspace creation surfaces
    dedup_warning_code='dedup_unavailable' instead of silently succeeding."""
    now = datetime.now(timezone.utc)
    created_ws = {
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "name": "Tencent",
        "description": "",
        "status": "stopped",
        "computer_id": COMP_ID,
        "dir_name": "Tencent",
        "created_at": now,
        "updated_at": now,
        "config": {},
    }

    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "_resolve_caller_tier",
        create_autospec(dedup_mod._resolve_caller_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "_find_matches",
        create_autospec(
            dedup_mod._find_matches,
            side_effect=TimeoutError("Workspace dedup LLM deadline exceeded"),
        ),
    )
    monkeypatch.setattr(
        workspaces_app.cli_approval,
        "workspace_create",
        create_autospec(workspaces_app.cli_approval.workspace_create, return_value=None),
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = created_ws
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        classmethod(lambda cls: mgr),
    )

    principal = Principal(user_id=USER_ID, auth="session")
    http_req = Request({"type": "http", "headers": [], "app": object()})
    resp = await workspaces_app.create_workspace(
        WorkspaceCreate(name="Tencent"),
        principal,
        http_req,
    )
    assert resp.workspace_id == WS_ID
    assert resp.dedup_warning_code == dedup_mod.WEB_DEDUP_UNAVAILABLE_CODE
    assert resp.dedup_warning == dedup_mod.WEB_DEDUP_UNAVAILABLE_MESSAGE
    mgr.create_workspace.assert_awaited_once()
