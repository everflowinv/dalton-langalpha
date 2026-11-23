"""Unit tests for Standalone Core Batch 2b:
1. ``plugins/dalton_sec/`` split (Agent Plugins 1.0.0 contract & caller cutover).
2. ``d045_plugin_disable_overrides`` migration upgrade/downgrade & compatibility.
3. Three-way plugin toggle consistency across ``GET /api/v1/plugins``,
   ``PATCH /api/v1/plugins/{name}/enabled``, ``GET /api/v1/mcp/builtin-servers``,
   and ``resolve_mcp_config`` (including legacy ``('bundle', 'dalton-data')``
   inheritance, re-enable override, re-disable, and capability-gated hiding).
4. ``src/ptc_agent/config/utils.py`` MCP merge capability filtering.
5. ``src/dalton/llm/overlay.py`` generic-only pruning when ``native_oauth_sync`` is off.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection, AsyncCursor
from psycopg.errors import OperationalError, UndefinedTable
from sqlalchemy.engine import Connection, Result

from ptc_agent.config.utils import create_mcp_config
from src.config.core import clear_config_cache
from src.dalton.app import oauth_native as oauth_native_app
from src.dalton.capabilities import (
    CAPABILITY_SPECS,
    CapabilityId,
    has_capability,
)
from src.dalton.database import plugin_disable_overrides as pdo_mod
from src.dalton.llm import overlay as overlay_mod
from src.dalton.migrations.versions import d045_plugin_disable_overrides as d045
from src.dalton.settings import get_dalton_config
from src.llms.llm import ModelConfig
from src.server.app import mcp_builtin as mcp_builtin_app
from src.server.app import oauth as oauth_app
from src.server.app import plugins as plugins_app
from src.server.app import setup as server_setup
from src.server.database import account_disables as account_disables_db
from src.server.database import mcp_config_version as mcp_ver_db
from src.server.database import mcp_descriptions as mcp_desc_db
from src.server.database import mcp_oauth as mcp_oauth_db
from src.server.database import mcp_servers as mcp_servers_db
from src.server.database import mcp_tool_schemas as mcp_schemas_db
from src.server.database import plugin_subagents as plugin_subagents_db
from src.server.database import plugins as plugins_db
from src.server.database import user_skills as user_skills_db
from src.server.services import mcp_config as mcp_config_svc
from src.server.services.mcp_oauth import lifecycle as oauth_lifecycle_mod
from tests.conftest import create_test_app

REPO_ROOT = Path(__file__).resolve().parents[3]

_CLEAR_ENV_VARS: tuple[str, ...] = (
    "APP_ENV",
    "DALTON_PROFILE",
    "DALTON_ENABLED",
    "DALTON_RESEARCHDATA_URL",
    "DALTON_BRIDGE_URL",
    "CHANNEL_GATEWAY_URL",
    *(spec.env_var for spec in CAPABILITY_SPECS.values()),
)


def _set_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str | None = None,
    dalton_profile: str | None = None,
) -> None:
    for var in _CLEAR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    if app_env is not None:
        monkeypatch.setenv("APP_ENV", app_env)
    if dalton_profile is not None:
        monkeypatch.setenv("DALTON_PROFILE", dalton_profile)
    clear_config_cache()
    get_dalton_config.cache_clear()


class _InMemoryDisableStore:
    """Minimal stateful in-memory store for ``user_mcp_builtin_disables`` and ``dalton_plugin_disable_overrides``."""

    def __init__(
        self,
        *,
        interleave_yield: bool = False,
        missing_overrides_table: bool = False,
        override_query_error: Exception | None = None,
    ) -> None:
        self.disables: set[tuple[str, str, str]] = set()
        self.overrides: set[tuple[str, str]] = set()
        self.version_bumps: list[str] = []
        self.interleave_yield = interleave_yield
        self.missing_overrides_table = missing_overrides_table
        self.override_query_error = override_query_error
        self.advisory_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.lock_events: list[tuple[str, tuple[str, str]]] = []
        self.read_tx_count: int = 0
        self.read_query_count: int = 0

    def _check_overrides_table(self) -> None:
        if self.override_query_error is not None:
            raise self.override_query_error
        if self.missing_overrides_table:
            raise UndefinedTable(
                'relation "dalton_plugin_disable_overrides" does not exist'
            )

    def make_connection_factory(self):
        store = self

        class _CursorState:
            def __init__(self, held_locks: list[asyncio.Lock]) -> None:
                self._rows: list[dict[str, Any]] = []
                self._held_locks = held_locks

            async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
                q = " ".join(str(sql).split())
                if "pg_advisory_xact_lock" in q:
                    key = (str(params[0]), str(params[1]))
                    lock = store.advisory_locks.setdefault(key, asyncio.Lock())
                    await lock.acquire()
                    self._held_locks.append(lock)
                    store.lock_events.append(("acquire", key))
                    self._rows = []
                elif "UNION ALL" in q and "user_mcp_builtin_disables" in q and "dalton_plugin_disable_overrides" in q:
                    store.read_query_count += 1
                    store._check_overrides_table()
                    uid = params[0]
                    rows = [
                        {"kind": k, "name": n}
                        for u, k, n in sorted(store.disables)
                        if u == uid
                    ]
                    rows.extend(
                        {"kind": "override", "name": b}
                        for u, b in sorted(store.overrides)
                        if u == uid
                    )
                    self._rows = rows
                elif "SELECT kind, name FROM user_mcp_builtin_disables" in q:
                    store.read_query_count += 1
                    uid = params[0]
                    self._rows = [
                        {"kind": k, "name": n}
                        for u, k, n in sorted(store.disables)
                        if u == uid
                    ]
                elif "INSERT INTO user_mcp_builtin_disables" in q:
                    store.disables.add((params[0], params[1], params[2]))
                    self._rows = []
                    if store.interleave_yield:
                        await asyncio.sleep(0)
                elif "DELETE FROM user_mcp_builtin_disables" in q:
                    store.disables.discard((params[0], params[1], params[2]))
                    self._rows = []
                    if store.interleave_yield:
                        await asyncio.sleep(0)
                elif "SELECT bundle_name FROM dalton_plugin_disable_overrides" in q:
                    store.read_query_count += 1
                    store._check_overrides_table()
                    uid = params[0]
                    self._rows = [
                        {"bundle_name": b}
                        for u, b in sorted(store.overrides)
                        if u == uid
                    ]
                elif "INSERT INTO dalton_plugin_disable_overrides" in q:
                    store._check_overrides_table()
                    store.overrides.add((params[0], params[1]))
                    self._rows = []
                elif "DELETE FROM dalton_plugin_disable_overrides" in q:
                    store._check_overrides_table()
                    store.overrides.discard((params[0], params[1]))
                    self._rows = []
                else:
                    raise AssertionError(f"Unexpected SQL in in-memory disable store: {q}")

            async def fetchall(self) -> list[dict[str, Any]]:
                return list(self._rows)

        def _make_conn() -> Any:
            held_locks: list[asyncio.Lock] = []

            def _make_cursor(*args: Any, **kwargs: Any):
                state = _CursorState(held_locks)
                cur = create_autospec(AsyncCursor, instance=True)
                cur.execute.side_effect = state.execute
                cur.fetchall.side_effect = state.fetchall

                @asynccontextmanager
                async def _cm():
                    yield cur

                return _cm()

            @asynccontextmanager
            async def _tx():
                store.read_tx_count += 1
                disables_snap = set(store.disables)
                overrides_snap = set(store.overrides)
                bumps_len = len(store.version_bumps)
                try:
                    yield None
                except BaseException:
                    store.disables.clear()
                    store.disables.update(disables_snap)
                    store.overrides.clear()
                    store.overrides.update(overrides_snap)
                    del store.version_bumps[bumps_len:]
                    raise
                finally:
                    while held_locks:
                        held_locks.pop().release()

            conn = create_autospec(AsyncConnection, instance=True)
            conn.cursor.side_effect = _make_cursor
            conn.transaction.side_effect = _tx
            return conn

        @asynccontextmanager
        async def _get_db_connection(existing: Any = None):
            yield _make_conn() if existing is None else existing

        return _get_db_connection


# ---------------------------------------------------------------------------
# 1. plugins/dalton_sec/ split & Agent Plugins 1.0.0 schema compliance
# ---------------------------------------------------------------------------


class TestDaltonSecBundleSplit:
    def test_dalton_sec_is_valid_standalone_open_source_bundle(self) -> None:
        sec_dir = REPO_ROOT / "plugins" / "dalton_sec"
        plugin_doc = json.loads((sec_dir / "plugin.json").read_text(encoding="utf-8"))
        mcp_doc = json.loads((sec_dir / "mcp.json").read_text(encoding="utf-8"))

        assert plugin_doc["name"] == "dalton-sec"
        assert plugin_doc["license"] == "Apache-2.0"
        assert (sec_dir / "dalton_sec_mcp_server.py").is_file()
        assert (sec_dir / "_bootstrap.py").is_file()

        # mcp.json is closed (additionalProperties: false): only spec transport keys allowed
        assert set(mcp_doc.keys()) == {"$schema", "mcpServers"}
        assert set(mcp_doc["mcpServers"].keys()) == {"dalton_sec"}
        sec_srv = mcp_doc["mcpServers"]["dalton_sec"]
        assert set(sec_srv.keys()) <= {"type", "command", "args", "env", "cwd"}
        assert sec_srv["type"] == "stdio"
        assert sec_srv["args"][-1] == "plugins/dalton_sec/dalton_sec_mcp_server.py"

        # Descriptive fields live in plugin.json extensions["ai.langalpha"]
        ext_servers = plugin_doc["extensions"]["ai.langalpha"]["servers"]
        assert set(ext_servers.keys()) == {"dalton_sec"}
        assert ext_servers["dalton_sec"]["tool_exposure_mode"] == "summary"
        assert "get_company_facts" in ext_servers["dalton_sec"]["instruction"]

    def test_dalton_data_retains_only_researchdata_proprietary_bundle(self) -> None:
        data_dir = REPO_ROOT / "plugins" / "dalton_data"
        plugin_doc = json.loads((data_dir / "plugin.json").read_text(encoding="utf-8"))
        mcp_doc = json.loads((data_dir / "mcp.json").read_text(encoding="utf-8"))

        assert plugin_doc["name"] == "dalton-data"
        assert plugin_doc["license"] == "Proprietary"
        assert not (data_dir / "dalton_sec_mcp_server.py").exists()
        assert set(mcp_doc["mcpServers"].keys()) == {"researchdata"}
        assert set(plugin_doc["extensions"]["ai.langalpha"]["servers"].keys()) == {"researchdata"}

    def test_health_sec_probe_imports_from_dalton_sec_bundle(self) -> None:
        health_src = (REPO_ROOT / "src" / "dalton" / "data_map" / "health.py").read_text(
            encoding="utf-8"
        )
        assert "from plugins.dalton_sec.dalton_sec_mcp_server import list_filings" in health_src
        assert "plugins.dalton_data.dalton_sec_mcp_server" not in health_src


# ---------------------------------------------------------------------------
# 2. Migration d045_plugin_disable_overrides
# ---------------------------------------------------------------------------


class TestD045Migration:
    def _wire_d045_op(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        sec_disable_count: int = 0,
        override_count: int = 0,
        as_sql: bool = False,
    ) -> list[str]:
        executed: list[str] = []

        ctx_mock = create_autospec(MigrationContext, instance=True)
        ctx_mock.as_sql = as_sql

        def _bind_execute(stmt: Any) -> Any:
            sql_text = str(stmt)
            executed.append(sql_text)
            res = create_autospec(Result, instance=True)
            if "to_regclass('public.user_mcp_builtin_disables')" in sql_text:
                res.scalar.return_value = "user_mcp_builtin_disables"
            elif "to_regclass('public.dalton_plugin_disable_overrides')" in sql_text:
                res.scalar.return_value = "dalton_plugin_disable_overrides"
            elif "FROM user_mcp_builtin_disables" in sql_text:
                res.scalar.return_value = sec_disable_count
            elif "FROM dalton_plugin_disable_overrides" in sql_text:
                res.scalar.return_value = override_count
            else:
                raise AssertionError(f"Unexpected bind SQL in d045 downgrade: {sql_text}")
            return res

        bind_mock = create_autospec(Connection, instance=True)
        bind_mock.execute.side_effect = _bind_execute

        op_mock = create_autospec(Operations, instance=True)
        op_mock.get_context.return_value = ctx_mock
        op_mock.get_bind.return_value = bind_mock
        op_mock.execute.side_effect = lambda sql: executed.append(str(sql))
        monkeypatch.setattr(d045, "op", op_mock)
        return executed

    def test_d045_revision_chain_upgrade_and_clean_downgrade(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert d045.revision == "d045"
        assert d045.down_revision == "d044"

        executed_up = self._wire_d045_op(monkeypatch)
        d045.upgrade()
        up_sql = "\n".join(executed_up)
        assert "SET LOCAL lock_timeout = '5s'" in up_sql
        assert "CREATE TABLE IF NOT EXISTS dalton_plugin_disable_overrides" in up_sql
        assert "user_id" in up_sql
        assert "bundle_name" in up_sql
        assert "reenabled_at" in up_sql
        assert "PRIMARY KEY (user_id, bundle_name)" in up_sql
        # Backward-compatible with previous backends: must only create a new table, never alter old ones
        assert "ALTER TABLE" not in up_sql.upper()

        executed_down = self._wire_d045_op(
            monkeypatch, sec_disable_count=0, override_count=0
        )
        d045.downgrade()
        down_sql = "\n".join(executed_down)
        assert "SET LOCAL lock_timeout = '5s'" in down_sql
        assert "DROP TABLE IF EXISTS dalton_plugin_disable_overrides" in down_sql

        # Both tables must be locked in SHARE ROW EXCLUSIVE mode BEFORE the row-count checks and DROP TABLE
        lock_disables_idx = next(
            i
            for i, s in enumerate(executed_down)
            if "LOCK TABLE user_mcp_builtin_disables IN SHARE ROW EXCLUSIVE MODE" in s
        )
        lock_overrides_idx = next(
            i
            for i, s in enumerate(executed_down)
            if "LOCK TABLE dalton_plugin_disable_overrides IN SHARE ROW EXCLUSIVE MODE" in s
        )
        count_disables_idx = next(
            i for i, s in enumerate(executed_down) if "FROM user_mcp_builtin_disables" in s
        )
        count_overrides_idx = next(
            i for i, s in enumerate(executed_down) if "FROM dalton_plugin_disable_overrides" in s
        )
        drop_idx = next(
            i for i, s in enumerate(executed_down) if "DROP TABLE IF EXISTS dalton_plugin_disable_overrides" in s
        )
        assert lock_disables_idx < lock_overrides_idx < count_disables_idx < count_overrides_idx < drop_idx
        assert not any(
            kw in s.upper()
            for s in executed_down
            for kw in ("COMMIT", "BEGIN")
        )

    def test_d045_downgrade_refuses_when_dalton_sec_disable_rows_exist(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executed_down = self._wire_d045_op(
            monkeypatch, sec_disable_count=2, override_count=0
        )
        with pytest.raises(RuntimeError, match="Refusing to downgrade d045") as exc_info:
            d045.downgrade()
        assert "dalton-sec" in str(exc_info.value)
        assert not any("DROP TABLE" in s for s in executed_down)

    def test_d045_downgrade_refuses_when_reenable_override_rows_exist(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executed_down = self._wire_d045_op(
            monkeypatch, sec_disable_count=0, override_count=1
        )
        with pytest.raises(RuntimeError, match="Refusing to downgrade d045") as exc_info:
            d045.downgrade()
        assert "dalton_plugin_disable_overrides" in str(exc_info.value)
        assert not any("DROP TABLE" in s for s in executed_down)

    def test_d045_downgrade_refuses_offline_sql_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executed_down = self._wire_d045_op(monkeypatch, as_sql=True)
        with pytest.raises(RuntimeError, match="offline SQL mode"):
            d045.downgrade()
        assert executed_down == []


# ---------------------------------------------------------------------------
# 3. Three-way plugin toggle closure (list + PATCH + tool resolution)
# ---------------------------------------------------------------------------


class TestPluginToggleThreeWayConsistency:
    def _wire_app_and_store(
        self,
        monkeypatch: pytest.MonkeyPatch,
        store: _InMemoryDisableStore,
    ) -> tuple[Any, Any]:
        conn_factory = store.make_connection_factory()
        monkeypatch.setattr(account_disables_db, "get_db_connection", conn_factory)
        monkeypatch.setattr(pdo_mod, "get_db_connection", conn_factory)

        async def _record_bump(_cur: Any, uid: str) -> None:
            store.version_bumps.append(uid)

        monkeypatch.setattr(
            account_disables_db,
            "bump_user_versions",
            create_autospec(mcp_ver_db.bump_user_versions, side_effect=_record_bump),
        )
        # Ensure live bundle_owners reflects current profile (not stale boot snapshot)
        monkeypatch.setattr(server_setup, "bundle_owners", None)

        # User-installed plugins DB stubs (no installed rows; bundled path exercised end-to-end)
        monkeypatch.setattr(
            plugins_app,
            "list_plugins",
            create_autospec(plugins_db.list_plugins, return_value=[]),
        )
        monkeypatch.setattr(
            plugins_app,
            "get_plugin",
            create_autospec(plugins_db.get_plugin, return_value=None),
        )
        monkeypatch.setattr(
            plugins_app,
            "set_plugin_enabled",
            create_autospec(plugins_db.set_plugin_enabled, return_value=None),
        )
        monkeypatch.setattr(
            plugins_app,
            "list_catalog_servers",
            create_autospec(mcp_servers_db.list_catalog_servers, return_value=[]),
        )
        monkeypatch.setattr(
            plugins_app,
            "list_all_user_skills",
            create_autospec(user_skills_db.list_all_user_skills, return_value=[]),
        )
        monkeypatch.setattr(
            plugins_app,
            "list_user_plugin_subagent_names",
            create_autospec(plugin_subagents_db.list_user_plugin_subagent_names, return_value=[]),
        )
        revoke_spy = create_autospec(oauth_lifecycle_mod.revoke_live_grants, return_value=None)
        monkeypatch.setattr(oauth_lifecycle_mod, "revoke_live_grants", revoke_spy)

        # resolve_mcp_config dependencies (empty workspace/user tiers, real account_disables & bundled owners)
        monkeypatch.setattr(
            mcp_servers_db,
            "get_workspace_servers_and_version",
            create_autospec(mcp_servers_db.get_workspace_servers_and_version, return_value=([], 1)),
        )
        monkeypatch.setattr(
            mcp_servers_db,
            "list_enabled_user_servers",
            create_autospec(mcp_servers_db.list_enabled_user_servers, return_value=[]),
        )
        monkeypatch.setattr(
            mcp_oauth_db,
            "list_connections",
            create_autospec(mcp_oauth_db.list_connections, return_value=[]),
        )
        monkeypatch.setattr(
            mcp_schemas_db,
            "get_user_tool_schemas",
            create_autospec(mcp_schemas_db.get_user_tool_schemas, return_value=[]),
        )
        monkeypatch.setattr(
            mcp_desc_db,
            "current_descriptions",
            create_autospec(mcp_desc_db.current_descriptions, return_value={}),
        )

        # Wire setup.agent_config for GET /api/v1/mcp/builtin-servers
        mcp_cfg = create_mcp_config({"tool_discovery_enabled": True, "servers": []})
        fake_agent_cfg = type("_Cfg", (), {"mcp": mcp_cfg})()
        monkeypatch.setattr(server_setup, "agent_config", fake_agent_cfg)

        app = create_test_app(plugins_app.router)
        app.include_router(mcp_builtin_app.router)
        return app, revoke_spy, fake_agent_cfg

    @pytest.mark.asyncio
    async def test_core_profile_plugin_list_patch_and_tool_resolution_stay_consistent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="core")
        store = _InMemoryDisableStore()
        app, revoke_spy, base_mcp = self._wire_app_and_store(monkeypatch, store)
        user_id = "test-user-123"

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 0) Fresh Core account: dalton-data absent everywhere; dalton-sec enabled everywhere
            resp = await client.get("/api/v1/plugins")
            assert resp.status_code == 200
            by_name = {p["name"]: p for p in resp.json()["plugins"]}
            assert "dalton-data" not in by_name
            assert "dalton-sec" in by_name
            assert by_name["dalton-sec"]["enabled"] is True

            # Toggling capability-disabled dalton-data returns 404 (both off and on)
            assert (
                await client.patch("/api/v1/plugins/dalton-data/enabled", json={"enabled": False})
            ).status_code == 404
            assert (
                await client.patch("/api/v1/plugins/dalton-data/enabled", json={"enabled": True})
            ).status_code == 404

            resolved_fresh = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
            active_names = {s.name for s in resolved_fresh.servers}
            assert "researchdata" not in active_names
            assert "dalton_sec" in active_names

            # 1) Legacy pre-split disable: ('bundle', 'dalton-data') in DB, no d045 override
            store.disables.add((user_id, "bundle", "dalton-data"))

            resp_inherited = await client.get("/api/v1/plugins")
            by_name = {p["name"]: p for p in resp_inherited.json()["plugins"]}
            assert "dalton-data" not in by_name
            assert by_name["dalton-sec"]["enabled"] is False

            # Even with legacy disable row present, PATCH dalton-data enabled=true stays 404 in Core
            assert (
                await client.patch("/api/v1/plugins/dalton-data/enabled", json={"enabled": True})
            ).status_code == 404

            resp_builtin = await client.get("/api/v1/mcp/builtin-servers")
            builtin_by_name = {s["name"]: s for s in resp_builtin.json()["servers"]}
            assert "researchdata" not in builtin_by_name
            assert builtin_by_name["dalton_sec"]["plugin_name"] == "dalton-sec"
            assert builtin_by_name["dalton_sec"]["plugin_enabled"] is False

            resolved_inherited = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
            assert "dalton_sec" not in {s.name for s in resolved_inherited.servers}

            # 2) Explicit re-enable via PATCH /api/v1/plugins/dalton-sec/enabled -> writes d045 override
            resp_on = await client.patch(
                "/api/v1/plugins/dalton-sec/enabled", json={"enabled": True}
            )
            assert resp_on.status_code == 200
            assert resp_on.json() == {"name": "dalton-sec", "enabled": True}
            assert (user_id, "dalton-sec") in store.overrides
            assert (user_id, "bundle", "dalton-data") in store.disables

            resp_list_on = await client.get("/api/v1/plugins")
            by_name = {p["name"]: p for p in resp_list_on.json()["plugins"]}
            assert by_name["dalton-sec"]["enabled"] is True

            resp_builtin_on = await client.get("/api/v1/mcp/builtin-servers")
            builtin_by_name = {s["name"]: s for s in resp_builtin_on.json()["servers"]}
            assert builtin_by_name["dalton_sec"]["plugin_enabled"] is True

            resolved_on = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
            assert "dalton_sec" in {s.name for s in resolved_on.servers}

            # 3) Explicit re-disable via PATCH /api/v1/plugins/dalton-sec/enabled -> clears override, writes disable
            resp_off = await client.patch(
                "/api/v1/plugins/dalton-sec/enabled", json={"enabled": False}
            )
            assert resp_off.status_code == 200
            assert resp_off.json() == {"name": "dalton-sec", "enabled": False}
            assert (user_id, "dalton-sec") not in store.overrides
            assert (user_id, "bundle", "dalton-sec") in store.disables
            revoke_spy.assert_awaited_with(user_id, ["dalton_sec"])

            resp_list_off = await client.get("/api/v1/plugins")
            by_name = {p["name"]: p for p in resp_list_off.json()["plugins"]}
            assert by_name["dalton-sec"]["enabled"] is False

            resolved_off = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
            assert "dalton_sec" not in {s.name for s in resolved_off.servers}

    @pytest.mark.asyncio
    async def test_everflow_profile_preserves_dalton_data_disable_while_reenabling_dalton_sec(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="dalton")
        store = _InMemoryDisableStore()
        app, _, base_mcp = self._wire_app_and_store(monkeypatch, store)
        user_id = "test-user-123"

        # Legacy disable on dalton-data disables both dalton-data and dalton-sec
        store.disables.add((user_id, "bundle", "dalton-data"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/plugins")
            by_name = {p["name"]: p for p in resp.json()["plugins"]}
            assert by_name["dalton-data"]["enabled"] is False
            assert by_name["dalton-sec"]["enabled"] is False

            resolved_before = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
            names_before = {s.name for s in resolved_before.servers}
            assert "researchdata" not in names_before
            assert "dalton_sec" not in names_before

            # Re-enable dalton-sec only -> researchdata stays withdrawn, dalton_sec returns
            resp_on = await client.patch(
                "/api/v1/plugins/dalton-sec/enabled", json={"enabled": True}
            )
            assert resp_on.status_code == 200

            resp_after = await client.get("/api/v1/plugins")
            by_name_after = {p["name"]: p for p in resp_after.json()["plugins"]}
            assert by_name_after["dalton-data"]["enabled"] is False
            assert by_name_after["dalton-sec"]["enabled"] is True

            resolved_after = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
            names_after = {s.name for s in resolved_after.servers}
            assert "researchdata" not in names_after
            assert "dalton_sec" in names_after

    @pytest.mark.asyncio
    async def test_concurrent_enable_and_disable_serialize_via_advisory_lock_and_leave_single_record(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two interleaved enable/disable toggles on the same (user_id, bundle) serialize on one
        advisory xact lock and never leave both a disable row and an override row behind;
        reads observe both tables in one single-query transaction."""
        _set_env(monkeypatch, app_env="core")
        user_id = "test-user-123"

        for first_disabled, second_disabled in ((False, True), (True, False)):
            with monkeypatch.context() as mp:
                store = _InMemoryDisableStore(interleave_yield=True)
                app, _, base_mcp = self._wire_app_and_store(mp, store)
                store.disables.add((user_id, "bundle", "dalton-data"))

                await asyncio.gather(
                    account_disables_db.set_account_disable(
                        user_id, "bundle", "dalton-sec", disabled=first_disabled
                    ),
                    account_disables_db.set_account_disable(
                        user_id, "bundle", "dalton-sec", disabled=second_disabled
                    ),
                )

                # Both paths must acquire the exact same (namespace, user_id:bundle) advisory lock
                expected_lock_key = ("dalton:plugin_bundle_toggle", f"{user_id}:dalton-sec")
                assert store.lock_events == [
                    ("acquire", expected_lock_key),
                    ("acquire", expected_lock_key),
                ]

                has_disable = (user_id, "bundle", "dalton-sec") in store.disables
                has_override = (user_id, "dalton-sec") in store.overrides
                # Strict XOR: exactly one state survives, never both
                assert has_disable ^ has_override, (
                    f"Concurrent toggle left contradictory state: disables={store.disables}, overrides={store.overrides}"
                )

                # Single-transaction single-query read side matches list & tool resolution
                tx_before = store.read_tx_count
                q_before = store.read_query_count
                disables = await account_disables_db.list_account_disables(user_id)
                assert store.read_tx_count - tx_before == 1
                assert store.read_query_count - q_before == 1

                expected_enabled = has_override and not has_disable
                assert ("dalton-sec" not in disables.bundles) is expected_enabled

                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/v1/plugins")
                    by_name = {p["name"]: p for p in resp.json()["plugins"]}
                    assert by_name["dalton-sec"]["enabled"] is expected_enabled

                resolved = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
                assert ("dalton_sec" in {s.name for s in resolved.servers}) is expected_enabled

    @pytest.mark.asyncio
    async def test_missing_overrides_table_read_falls_back_and_other_db_errors_propagate(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Upstream-only startup (no Dalton migrations): missing dalton_plugin_disable_overrides
        treats re-enable overrides as empty on read, while non-UndefinedTable DB errors propagate."""
        _set_env(monkeypatch, app_env="dalton")
        user_id = "test-user-123"

        store = _InMemoryDisableStore(missing_overrides_table=True)
        app, _, base_mcp = self._wire_app_and_store(monkeypatch, store)

        # Fresh account (no legacy disables): GET /api/v1/plugins & resolve_mcp_config succeed
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/plugins")
            assert resp.status_code == 200
            by_name = {p["name"]: p for p in resp.json()["plugins"]}
            assert by_name["dalton-sec"]["enabled"] is True

        resolved = await mcp_config_svc.resolve_mcp_config(base_mcp, user_id, "ws-1")
        assert "dalton_sec" in {s.name for s in resolved.servers}

        # With legacy ('bundle', 'dalton-data') disable and no overrides table, inheritance still applies
        store.disables.add((user_id, "bundle", "dalton-data"))
        disables = await account_disables_db.list_account_disables(user_id)
        assert "dalton-sec" in disables.bundles
        assert await pdo_mod.list_reenabled_bundles(user_id) == frozenset()

        # Also works when caller passes an existing connection (in_existing_tx=True)
        async with store.make_connection_factory()() as existing_conn:
            disables_in_tx = await account_disables_db.list_account_disables(
                user_id, conn=existing_conn
            )
            assert "dalton-sec" in disables_in_tx.bundles

        # Non-UndefinedTable DB error must NOT be swallowed
        store.missing_overrides_table = False
        store.override_query_error = OperationalError("connection dropped")
        with pytest.raises(OperationalError, match="connection dropped"):
            await account_disables_db.list_account_disables(user_id)
        with pytest.raises(OperationalError, match="connection dropped"):
            await pdo_mod.list_reenabled_bundles(user_id)

    @pytest.mark.asyncio
    async def test_missing_overrides_table_write_returns_503_and_rolls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Toggling dalton-sec when dalton_plugin_disable_overrides is absent returns HTTP 503
        instructing the operator to run Dalton migrations, without leaving partial writes."""
        _set_env(monkeypatch, app_env="core")
        user_id = "test-user-123"

        store = _InMemoryDisableStore(missing_overrides_table=True)
        app, _, _ = self._wire_app_and_store(monkeypatch, store)
        store.disables.add((user_id, "bundle", "dalton-data"))

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for desired_enabled in (True, False):
                resp = await client.patch(
                    "/api/v1/plugins/dalton-sec/enabled",
                    json={"enabled": desired_enabled},
                )
                assert resp.status_code == 503
                detail = resp.json()["detail"]
                assert "dalton_plugin_disable_overrides" in detail
                assert "alembic -c src/dalton/alembic.ini upgrade head" in detail
                # Transaction must have rolled back: no partial disable row, no version bump
                assert store.disables == {(user_id, "bundle", "dalton-data")}
                assert store.overrides == set()
                assert store.version_bumps == []

            # Non-UndefinedTable DB error still propagates as 500 (not misclassified as 503)
            store.missing_overrides_table = False
            store.override_query_error = OperationalError("disk full")
            resp_err = await client.patch(
                "/api/v1/plugins/dalton-sec/enabled",
                json={"enabled": True},
            )
            assert resp_err.status_code == 500


# ---------------------------------------------------------------------------
# 4. MCP merge filtering (src/ptc_agent/config/utils.py)
# ---------------------------------------------------------------------------


class TestMcpConfigMergeFiltering:
    def test_core_profile_filters_disabled_servers_from_bundles_and_yaml(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="core")
        cfg = create_mcp_config(
            {
                "tool_discovery_enabled": True,
                "servers": [
                    # Full definition injected by YAML must still be filtered out when capability is off
                    {
                        "name": "researchdata",
                        "transport": "http",
                        "url": "http://127.0.0.1:8960/mcp",
                        "enabled": True,
                    },
                    {"name": "price_data", "enabled": True},
                ],
            }
        )
        names = {s.name for s in cfg.servers}
        assert "researchdata" not in names
        assert "dalton_sec" in names
        assert "price_data" in names

    def test_partial_yaml_override_for_capability_disabled_server_does_not_fail(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="dalton")
        monkeypatch.setenv("DALTON_CAP_RESEARCHDATA_BRIDGE", "false")
        cfg = create_mcp_config(
            {
                "tool_discovery_enabled": True,
                "servers": [
                    # Matches agent_config.dalton.yaml partial override shape
                    {"name": "researchdata", "call_timeout_s": 625},
                ],
            }
        )
        names = {s.name for s in cfg.servers}
        assert "researchdata" not in names
        assert "dalton_sec" in names

    def test_everflow_profile_keeps_researchdata_and_dalton_sec(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="dalton")
        cfg = create_mcp_config(
            {
                "tool_discovery_enabled": True,
                "servers": [
                    {"name": "researchdata", "call_timeout_s": 625},
                ],
            }
        )
        by_name = {s.name: s for s in cfg.servers}
        assert "researchdata" in by_name
        assert by_name["researchdata"].call_timeout_s == 625
        assert "dalton_sec" in by_name
        assert by_name["dalton_sec"].args[-1] == "plugins/dalton_sec/dalton_sec_mcp_server.py"


# ---------------------------------------------------------------------------
# 5. Model overlay pruning (src/dalton/llm/overlay.py)
# ---------------------------------------------------------------------------


class TestModelOverlayCorePruning:
    def test_core_profile_keeps_generic_entries_and_drops_private_oauth_entries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Plant a fake runtime native-sync overlay to prove Core ignores it completely
        fake_sync = tmp_path / "native_sync_overlay.json"
        fake_sync.write_text(
            json.dumps(
                {
                    "models": {
                        "secret-runtime-oauth": {
                            "model_id": "secret-runtime",
                            "provider": "antigravity-oauth",
                            "visible": True,
                        }
                    },
                    "dalton_meta": {
                        "secret-runtime-oauth": {"family": "google-gemini", "billing": "subscription"}
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(overlay_mod, "NATIVE_SYNC_OVERLAY_PATH", fake_sync)
        monkeypatch.setattr(
            overlay_mod,
            "OVERLAY_SOURCES",
            [("native", overlay_mod.NATIVE_OVERLAY_PATH), ("native_sync", fake_sync)],
        )

        _set_env(monkeypatch, app_env="core")
        assert has_capability(CapabilityId.NATIVE_OAUTH_SYNC) is False

        mc = ModelConfig()
        # Private Dalton OAuth providers must NOT be injected in Core
        prov_cfg = mc.manifest.get("provider_config") or {}
        assert "antigravity" not in prov_cfg
        assert "meta" not in prov_cfg
        assert "xai-oauth" not in prov_cfg
        assert not mc.get_provider_info("antigravity-oauth")
        assert not mc.get_provider_info("muse-oauth")
        assert not mc.get_provider_info("xai-oauth")

        # Zero *-oauth models (neither Dalton-private nor upstream subscription OAuth) in Core catalog
        leaked_oauth_keys = sorted(k for k in mc.llm_config if k.endswith("-oauth"))
        assert leaked_oauth_keys == [], f"OAuth models leaked into Core catalog: {leaked_oauth_keys}"
        leaked_oauth_providers = sorted(
            k
            for k, v in mc.llm_config.items()
            if isinstance(v, dict) and str(v.get("provider") or "").endswith("-oauth")
        )
        assert leaked_oauth_providers == [], (
            f"OAuth-provider models leaked into Core catalog: {leaked_oauth_providers}"
        )
        for key in (
            "grok-4.7-oauth",
            "grok-4.6-oauth",
            "grok-build-oauth",
            "gpt-6.2-sol-oauth",
            "claude-opus-5-5-oauth",
            "gemini-4-neon-high-oauth",
            "gemini-4-carbon-medium-oauth",
            "claude-opus-5-5-high-oauth",
            "muse-wonder-1.0-oauth",
            "grok-4.7-build-fast-oauth",
            "secret-runtime-oauth",
        ):
            assert mc.get_model_config(key) is None, f"OAuth model {key} leaked into Core"

        # Generic entries from native_overlay.json and upstream cash models MUST remain available in Core
        assert mc.get_model_config("grok-4.7") is not None
        assert mc.get_model_config("grok-build-0.1") is not None
        assert mc.get_model_config("grok-build-0.1")["provider"] == "xai"
        assert mc.get_model_config("glm-5.3-plan") is not None
        assert mc.get_model_config("volcengine") is None  # provider, not model
        assert "volcengine" in prov_cfg
        assert mc.get_model_config("gemini-4-neon")["compaction_profile"] == "expansive"

        # load_dalton_meta() keeps generic cash/family metadata and drops *-oauth & runtime sync
        meta = overlay_mod.load_dalton_meta()
        assert meta["grok-4.7"]["family"] == "xai-grok"
        assert meta["claude-opus-5-5"]["family"] == "anthropic-claude"
        assert not any(k.endswith("-oauth") for k in meta)
        assert "secret-runtime-oauth" not in meta

        # Stamp changes immediately between Core and Everflow (no stale 2s cache collision)
        core_stamp = overlay_mod.current_overlay_stamp()
        assert core_stamp is not None and core_stamp.startswith("native_generic:")

        _set_env(monkeypatch, app_env="dalton")
        assert has_capability(CapabilityId.NATIVE_OAUTH_SYNC) is True
        ef_stamp = overlay_mod.current_overlay_stamp()
        assert ef_stamp is not None and ef_stamp != core_stamp
        assert "native_sync:" in ef_stamp

        mc_ef = ModelConfig()
        assert mc_ef.get_model_config("grok-4.7-oauth")["provider"] == "xai-oauth"
        assert mc_ef.get_model_config("gemini-4-neon-high-oauth") is not None
        assert mc_ef.get_model_config("muse-wonder-1.0-oauth") is not None


# ---------------------------------------------------------------------------
# 6. Standalone acceptance check script (b2_backend_capabilities.py) guards
# ---------------------------------------------------------------------------


class TestB2AcceptanceScriptGuards:
    def test_script_targets_alembic_version_dalton_and_real_workspace_mcp_route(self) -> None:
        script_src = (
            REPO_ROOT / "tests" / "standalone" / "checks" / "b2_backend_capabilities.py"
        ).read_text(encoding="utf-8")
        assert script_src.splitlines()[0].strip() == "# auth: oss"
        assert "alembic_version_dalton" in script_src
        assert "dalton_alembic_version" not in script_src
        assert "/api/v1/workspaces/{ws_id}/mcp/servers" in script_src
        assert "/mcp-servers" not in script_src

    def test_image_scan_fails_in_ci_when_image_env_missing_and_skips_locally(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.standalone.checks import b2_backend_capabilities as b2_check

        for var in ("BACKEND_IMAGE", "BACKEND_IMAGE_TAR", "EXPORT_DIR", "CI"):
            monkeypatch.delenv(var, raising=False)

        # Locally (CI unset): no-op when image env vars are omitted
        b2_check._check_image_layers_and_export()

        # In CI=true: missing BACKEND_IMAGE / BACKEND_IMAGE_TAR must fail loudly
        monkeypatch.setenv("CI", "true")
        with pytest.raises(AssertionError, match="BACKEND_IMAGE"):
            b2_check._check_image_layers_and_export()


# ---------------------------------------------------------------------------
# 7. Core subscription OAuth endpoint 404 gate (Everflow unchanged)
# ---------------------------------------------------------------------------


class TestCoreSubscriptionOAuthGate:
    @pytest.mark.asyncio
    async def test_core_profile_returns_404_for_all_subscription_oauth_endpoints(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="core")
        assert has_capability(CapabilityId.NATIVE_OAUTH_SYNC) is False

        app = create_test_app(oauth_app.router, oauth_native_app.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            core_disabled_calls = [
                ("POST", "/api/v1/oauth/codex/device/initiate", None),
                ("POST", "/api/v1/oauth/codex/device/poll", None),
                ("GET", "/api/v1/oauth/codex/status", None),
                ("DELETE", "/api/v1/oauth/codex", None),
                ("POST", "/api/v1/oauth/claude/initiate", None),
                ("POST", "/api/v1/oauth/claude/callback", {"callback_input": "code#state"}),
                ("GET", "/api/v1/oauth/claude/status", None),
                ("DELETE", "/api/v1/oauth/claude", None),
                ("GET", "/api/v1/oauth/grok/status", None),
                ("DELETE", "/api/v1/oauth/grok", None),
                ("GET", "/api/v1/oauth/muse/status", None),
                ("POST", "/api/v1/oauth/muse/device/initiate", None),
                ("GET", "/api/v1/oauth/antigravity/status", None),
                ("POST", "/api/v1/oauth/antigravity/initiate", None),
                ("GET", "/api/v1/oauth/xai/status", None),
                ("POST", "/api/v1/oauth/xai/device/initiate", None),
            ]
            for method, path, body in core_disabled_calls:
                resp = await client.request(method, path, json=body)
                assert resp.status_code == 404, f"Expected 404 for {method} {path} in Core, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_everflow_profile_keeps_subscription_oauth_endpoints_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_env(monkeypatch, app_env="dalton")
        assert has_capability(CapabilityId.NATIVE_OAUTH_SYNC) is True

        async def _no_conn(_user_id: str, _provider: str) -> None:
            return None

        async def _flag_on(_user_id: str, _feature: str) -> bool:
            return True

        monkeypatch.setattr(
            oauth_app,
            "get_oauth_connection",
            create_autospec(oauth_app.get_oauth_connection, side_effect=_no_conn),
        )
        monkeypatch.setattr(
            oauth_app,
            "user_feature_enabled",
            create_autospec(oauth_app.user_feature_enabled, side_effect=_flag_on),
        )

        app = create_test_app(oauth_app.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for key in ("codex", "claude"):
                resp = await client.get(f"/api/v1/oauth/{key}/status")
                assert resp.status_code == 200
                assert resp.json()["connected"] is False
