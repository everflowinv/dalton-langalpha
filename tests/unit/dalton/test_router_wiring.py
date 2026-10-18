"""G2 Gatekeeper tests for `/api/v1/dalton/*` and `/api/v1/oauth/*` router wiring.

Verifies that every route resolves its target service/database functions with
matching signatures (never raising AttributeError/TypeError -> 500), enforces
workspace ownership and PM permissions, and fails if a target function is
renamed or missing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dalton.app import dalton_router
from src.server.utils.api import get_current_user_id


class _FakeCursor:
    def __init__(self) -> None:
        self.description = []
        self.rowcount = 1

    async def execute(self, query: str, params: Any = None) -> None:
        return None

    async def fetchone(self) -> dict[str, Any] | None:
        return None

    async def fetchall(self) -> list[dict[str, Any]]:
        return []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _FakeConn:
    def cursor(self, *args: Any, **kwargs: Any) -> _FakeCursor:
        return _FakeCursor()

    @asynccontextmanager
    async def transaction(self):
        yield

    async def commit(self) -> None:
        return None


@asynccontextmanager
async def _fake_db_connection():
    yield _FakeConn()


class _StubLLMClient:
    def __init__(self, reply_text: str = "PONG") -> None:
        self.reply_text = reply_text

    async def ainvoke(self, messages: list[Any]) -> Any:
        return SimpleNamespace(
            content=self.reply_text,
            usage_metadata={"input_tokens": 8, "output_tokens": 2, "cached_tokens": 0},
        )


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Patch DB pool at the lowest layer so real service + database functions execute
    # their imports and parameter bindings without needing a live Postgres server.
    # Every test double uses create_autospec (L33) so wrong parameter names fail immediately.
    from ptc_agent.config.agent import CredentialSource
    from src.server.services.llm.user_models import ModelSource
    import src.dalton.autonomy.board as board_mod
    import src.dalton.autonomy.report as report_mod
    import src.dalton.budget.service as budget_svc
    import src.dalton.database.budget as budget_db
    import src.dalton.database.knowledge as knowledge_db
    import src.dalton.database.llm_journal as journal_db
    import src.dalton.database.openclaw_sync as oc_sync_db
    import src.dalton.database.provider_events as pe_db
    import src.dalton.database.publications as pub_db
    import src.dalton.database.verdicts as verdicts_db
    import src.dalton.llm.complete as complete_mod
    import src.dalton.llm.providers.antigravity.oauth as ag_oauth
    import src.dalton.llm.providers.muse.oauth as muse_oauth
    import src.dalton.services.openclaw_admin as oc_admin
    import src.dalton.services.provider_status as ps_mod
    import src.server.database.automation as auto_db
    import src.server.database.oauth_tokens as oauth_db
    import src.server.database.pool as pool_mod
    import src.server.database.workspace as ws_db
    import src.server.services.llm.clients as llm_clients_mod

    for mod in (
        pool_mod,
        budget_svc,
        budget_db,
        knowledge_db,
        journal_db,
        oc_sync_db,
        oc_admin,
        pe_db,
        pub_db,
        verdicts_db,
        board_mod,
        report_mod,
        ps_mod,
        oauth_db,
        complete_mod,
    ):
        if hasattr(mod, "get_db_connection"):
            monkeypatch.setattr(mod, "get_db_connection", _fake_db_connection)

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "user_id": "local-dev-user",
                "name": "META",
                "status": "running",
            },
        ),
    )
    monkeypatch.setattr(
        auto_db,
        "list_automations",
        create_autospec(auto_db.list_automations, return_value=([], 0)),
    )
    monkeypatch.setattr(
        auto_db,
        "create_automation",
        create_autospec(
            auto_db.create_automation,
            return_value={
                "automation_id": "00000000-0000-0000-0000-000000000099",
                "name": "dalton-meta-watch",
                "status": "active",
            },
        ),
    )
    for o_mod in (oauth_db, ag_oauth, muse_oauth):
        if hasattr(o_mod, "get_oauth_tokens"):
            monkeypatch.setattr(
                o_mod,
                "get_oauth_tokens",
                create_autospec(o_mod.get_oauth_tokens, return_value=None),
            )

    monkeypatch.setattr(
        oc_admin,
        "get_dalton_broker_token",
        create_autospec(oc_admin.get_dalton_broker_token, return_value=""),
    )
    monkeypatch.setattr(
        oc_admin,
        "try_acquire_sync_lock",
        create_autospec(oc_admin.try_acquire_sync_lock, return_value=True),
    )
    monkeypatch.setattr(
        oc_admin,
        "release_sync_lock",
        create_autospec(oc_admin.release_sync_lock, return_value=None),
    )
    monkeypatch.setattr(
        oc_admin,
        "create_sync_run",
        create_autospec(
            oc_admin.create_sync_run,
            return_value="00000000-0000-0000-0000-000000000088",
        ),
    )
    monkeypatch.setattr(
        oc_admin,
        "finish_sync_run",
        create_autospec(oc_admin.finish_sync_run, return_value=None),
    )
    monkeypatch.setattr(
        oc_admin,
        "find_user_referenced_models",
        create_autospec(oc_admin.find_user_referenced_models, return_value=set()),
    )

    # Wire resolve_model_client with create_autospec so probe_provider executes end-to-end
    monkeypatch.setattr(
        llm_clients_mod,
        "resolve_model_client",
        create_autospec(
            llm_clients_mod.resolve_model_client,
            return_value=llm_clients_mod.ResolvedClient(
                _StubLLMClient("PONG"),
                ModelSource.SYSTEM,
                CredentialSource.PLATFORM,
            ),
        ),
    )
    monkeypatch.setattr(
        complete_mod,
        "_check_cash_budget_admission",
        create_autospec(
            complete_mod._check_cash_budget_admission,
            return_value=(True, None, None),
        ),
    )
    monkeypatch.setattr(
        complete_mod,
        "claim_journal_invocation",
        create_autospec(
            complete_mod.claim_journal_invocation,
            return_value=("claimed", {"attempt": 1}),
        ),
    )
    monkeypatch.setattr(
        complete_mod,
        "complete_journal_done",
        create_autospec(complete_mod.complete_journal_done, return_value=None),
    )

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "local-dev-user"
    app.include_router(dalton_router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("GET", "/api/v1/dalton/openclaw/models/status", None),
        ("GET", "/api/v1/dalton/openclaw/health", None),
        ("POST", "/api/v1/dalton/openclaw/models/sync", {"dry_run": True, "force": False}),
        ("GET", "/api/v1/dalton/providers/status", None),
        ("POST", "/api/v1/dalton/providers/xai/probe", {"model_key": "grok-4.7"}),
        ("GET", "/api/v1/dalton/providers/available-models", None),
        ("GET", "/api/v1/dalton/budget", None),
        ("PUT", "/api/v1/dalton/budget", {"daily_cap_usd": 25.0, "timezone": "Asia/Shanghai"}),
        (
            "PUT",
            "/api/v1/dalton/budget/task-caps/workspace/00000000-0000-0000-0000-000000000001",
            {"cap_usd": 5.0},
        ),
        (
            "DELETE",
            "/api/v1/dalton/budget/task-caps/workspace/00000000-0000-0000-0000-000000000001",
            None,
        ),
        ("GET", "/api/v1/dalton/budget/today", None),
        (
            "GET",
            "/api/v1/dalton/knowledge/00000000-0000-0000-0000-000000000001/thesis/versions",
            None,
        ),
        (
            "GET",
            "/api/v1/dalton/knowledge/00000000-0000-0000-0000-000000000001/thesis/versions/1",
            None,
        ),
        (
            "GET",
            "/api/v1/dalton/knowledge/00000000-0000-0000-0000-000000000001/thesis/diff?from=1&to=2",
            None,
        ),
        (
            "POST",
            "/api/v1/dalton/knowledge/00000000-0000-0000-0000-000000000001/company/import",
            {
                "content": {
                    "schema_version": 1,
                    "ticker": "META",
                    "name": "Meta Platforms",
                    "exchange": "NASDAQ",
                    "currency": "USD",
                    "coverage_tier": "core",
                },
                "reason": "test import",
                "evidence_refs": [],
            },
        ),
        (
            "GET",
            "/api/v1/dalton/verdicts?workspace_id=00000000-0000-0000-0000-000000000001",
            None,
        ),
        (
            "GET",
            "/api/v1/dalton/publications?workspace_id=00000000-0000-0000-0000-000000000001",
            None,
        ),
        (
            "GET",
            "/api/v1/dalton/publications/00000000-0000-0000-0000-000000000002",
            None,
        ),
        ("GET", "/api/v1/dalton/publish-requests", None),
        (
            "PUT",
            "/api/v1/dalton/automations/dalton-meta-watch",
            {
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "entry": {
                    "trigger_type": "cron",
                    "cron_expression": "0 7 * * 1-5",
                    "instruction": "watch META",
                },
            },
        ),
        ("GET", "/api/v1/dalton/coverage/board", None),
        ("GET", "/api/v1/dalton/autonomy/report", None),
        ("GET", "/api/v1/oauth/antigravity/status", None),
        ("GET", "/api/v1/oauth/muse/status", None),
    ],
)
def test_every_dalton_route_resolves_without_500(
    client: TestClient,
    method: str,
    path: str,
    json_body: dict[str, Any] | None,
) -> None:
    resp = client.request(method, path, json=json_body)
    assert resp.status_code != 500, (
        f"{method} {path} returned 500: {resp.text}"
    )
    if resp.headers.get("content-type", "").startswith("application/json"):
        payload = resp.json()
        if isinstance(payload, dict):
            assert payload.get("error_kind") not in (
                "ImportError",
                "AttributeError",
                "NameError",
                "TypeError",
            ), f"{method} {path} swallowed runtime error in payload: {payload}"


def test_probe_provider_executes_via_dalton_complete_and_checks_pong(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0H §4 P1/P2/Low #6: probe endpoint resolves client with real signature, runs via dalton_complete, and requires PONG."""
    from ptc_agent.config.agent import CredentialSource
    from src.server.services.llm.user_models import ModelSource
    import src.dalton.llm.complete as complete_mod
    import src.server.services.llm.clients as llm_clients_mod

    # 1. PONG reply -> ok=True, journal recorded via complete_journal_done
    resp = client.post("/api/v1/dalton/providers/xai/probe", json={"model_key": "grok-4.7"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "xai"
    assert body["model"] == "grok-4.7"
    assert body["reply"] == "PONG"
    assert body["error_kind"] is None
    complete_mod.complete_journal_done.assert_called_once()

    # 2. Non-PONG reply -> ok=False, error_kind="unexpected_reply" (Low #6)
    llm_clients_mod.resolve_model_client.return_value = llm_clients_mod.ResolvedClient(
        _StubLLMClient("HELLO WORLD"),
        ModelSource.SYSTEM,
        CredentialSource.PLATFORM,
    )
    resp2 = client.post("/api/v1/dalton/providers/xai/probe", json={"model_key": "grok-4.7"})
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["ok"] is False
    assert body2["error_kind"] == "unexpected_reply"


def test_providers_status_reads_real_byok_dict_shape(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0H §4 P4: get_providers_status and explain_model_availability read rows['keys'] from get_user_api_keys."""
    import src.server.database.api_keys as api_keys_mod

    monkeypatch.setattr(
        api_keys_mod,
        "get_user_api_keys",
        create_autospec(
            api_keys_mod.get_user_api_keys,
            return_value={
                "byok_enabled": True,
                "keys": {"xai": "xai-test-key-not-leaked"},
                "base_urls": {"xai": None},
            },
        ),
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    resp = client.get("/api/v1/dalton/providers/status")
    assert resp.status_code == 200
    data = resp.json()
    xai_row = next(p for p in data["providers"] if p["provider"] == "xai")
    assert xai_row["configured"] is True
    assert xai_row["credential"]["has_byok"] is True
    assert any(w["code"] == "byok_cash_key" for w in xai_row["warnings"])
    assert "xai-test-key-not-leaked" not in resp.text

    avail_resp = client.get("/api/v1/dalton/providers/available-models")
    assert avail_resp.status_code == 200
    avail_data = avail_resp.json()
    assert avail_data["models"]["grok-4.7"]["available"] is True


def test_gatekeeper_catches_broken_service_symbol(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirm that if a service function called by router.py is missing, the test catches the 500."""
    import src.dalton.knowledge.service as knowledge_service

    monkeypatch.delattr(knowledge_service, "diff_versions")
    resp = client.get(
        "/api/v1/dalton/knowledge/00000000-0000-0000-0000-000000000001/thesis/diff?from=1&to=2"
    )
    assert resp.status_code == 500


def test_workspace_ownership_and_pm_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify non-owner gets 403 on workspace knowledge routes and non-PM gets 403 on sync/import."""
    import src.server.database.workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={
                "workspace_id": "00000000-0000-0000-0000-000000000001",
                "user_id": "owner-user",
                "name": "META",
                "status": "running",
            },
        ),
    )

    app = FastAPI()
    app.dependency_overrides[get_current_user_id] = lambda: "intruder-user"
    app.include_router(dalton_router)
    intruder_client = TestClient(app, raise_server_exceptions=False)

    r_vers = intruder_client.get(
        "/api/v1/dalton/knowledge/00000000-0000-0000-0000-000000000001/thesis/versions"
    )
    assert r_vers.status_code == 403

    r_sync = intruder_client.post(
        "/api/v1/dalton/openclaw/models/sync",
        json={"dry_run": True, "force": False},
    )
    assert r_sync.status_code == 403

