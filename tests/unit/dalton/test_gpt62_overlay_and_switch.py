"""Tests for GPT-6.2 Astra & Sol Dalton overlay, notional pricing, and switch_model_refs."""

from __future__ import annotations

import json
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
from psycopg import AsyncConnection, AsyncCursor

from scripts.dalton.ops import switch_model_refs as sw
from src.dalton.llm import notional_pricing as np_mod
from src.dalton.llm import overlay as overlay_mod
from src.dalton.llm.notional_pricing import (
    build_notional_price_index,
    calculate_notional_cost,
    is_subscription_model,
    resolve_notional_card,
)
from src.llms.llm import LLM, ModelConfig
from src.llms.pricing_utils import (
    calculate_total_cost,
    find_model_pricing,
    resolve_pricing_identity,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_gpt62_models_from_upstream_manifest_keep_notional_and_retire_61() -> None:
    LLM._model_config = None
    mc = ModelConfig()
    upstream_models = json.loads(
        (REPO_ROOT / "src/llms/manifest/models.json").read_text(encoding="utf-8")
    )
    overlay_data = overlay_mod.load_overlay_data(overlay_mod.NATIVE_OVERLAY_PATH) or {}
    overlay_models = overlay_data.get("models") or {}
    overlay_provider_models = (overlay_data.get("providers") or {}).get("models") or {}
    dalton_meta = overlay_mod.load_dalton_meta()

    for key, expected_provider, expected_model_id, expected_speed in (
        ("gpt-6.2-astra", "openai", "gpt-6.2-astra", 3),
        ("gpt-6.2-sol", "openai", "gpt-6.2-sol", 3),
        ("gpt-6.2-astra-oauth", "codex-oauth", "gpt-6.2-astra", 3),
        ("gpt-6.2-sol-oauth", "codex-oauth", "gpt-6.2-sol", 3),
    ):
        assert key in upstream_models, f"{key} should be provided by upstream manifest"
        assert key not in overlay_models, f"{key} should not duplicate upstream in overlay"
        assert key in dalton_meta, f"{key} missing from dalton_meta"

        entry = mc.get_model_config(key)
        assert entry is not None, f"{key} missing from merged ModelConfig"
        assert entry["provider"] == expected_provider
        assert entry["model_id"] == expected_model_id
        assert entry["speed"] == expected_speed
        assert entry["intelligence"] == 5
        assert entry["context"] == 1050000
        assert entry["input_modalities"] == ["text", "image", "pdf"]
        assert entry["reasoning"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
        assert entry["reasoning"]["default"] == "medium"

    assert "openai" not in overlay_provider_models

    # Upstream retired 6.0 Astra and 6.1 Sol keys are absent from catalog and dalton_meta.
    for old_key in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-astra-oauth", "gpt-6.1-sol-oauth"):
        assert mc.get_model_config(old_key) is None
        assert old_key not in dalton_meta


@pytest.mark.parametrize(
    ("key", "expected_provider", "expected_model_id", "expected_sub", "expected_1m_total"),
    [
        ("gpt-6.2-astra", "openai", "gpt-6.2-astra", False, 76.0),
        ("gpt-6.2-sol", "openai", "gpt-6.2-sol", False, 19.0),
        ("gpt-6.2-astra-oauth", "codex-oauth", "gpt-6.2-astra", True, 76.0),
        ("gpt-6.2-sol-oauth", "codex-oauth", "gpt-6.2-sol", True, 19.0),
    ],
)
def test_gpt62_overlay_pricing_and_notional_1m_in_1m_out(
    key: str,
    expected_provider: str,
    expected_model_id: str,
    expected_sub: bool,
    expected_1m_total: float,
) -> None:
    prov, pid = resolve_pricing_identity(key)
    assert prov == expected_provider
    assert pid == expected_model_id

    card = find_model_pricing(pid, provider=prov)
    assert card is not None
    cash_res = calculate_total_cost(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing=card,
    )
    assert cash_res["total_cost"] == pytest.approx(expected_1m_total)

    idx = build_notional_price_index()
    sub = is_subscription_model(key, pricing_model_id=pid, pricing_provider=prov)
    assert sub is expected_sub
    notional_card = resolve_notional_card(
        key, pricing_model_id=pid, pricing_provider=prov, index=idx
    )
    if expected_sub:
        assert notional_card is not None
        notional_total, _ = calculate_notional_cost(
            notional_card, input_tokens=1_000_000, output_tokens=1_000_000
        )
        assert notional_total == pytest.approx(expected_1m_total)
    else:
        assert notional_card is None


def test_notional_overlay_takes_priority_over_fallback_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With live overlay: tiered notional_ref card wins over flat _FALLBACK_OAUTH_CARDS.
    live_idx = build_notional_price_index()
    assert "input_tiers" in live_idx["gpt-6.2-astra-oauth"]
    assert "input_tiers" in live_idx["gpt-6.2-sol-oauth"]

    # When dalton_meta is empty/unavailable, _FALLBACK_OAUTH_CARDS provides tier-0 rates.
    monkeypatch.setattr(
        overlay_mod,
        "load_dalton_meta",
        create_autospec(overlay_mod.load_dalton_meta, return_value={}),
    )
    fallback_idx = np_mod.build_notional_price_index()
    assert fallback_idx["gpt-6.2-astra-oauth"] == {
        "input": 8.0,
        "output": 40.0,
        "cached_input": 0.4,
    }
    assert fallback_idx["gpt-6.2-sol-oauth"] == {
        "input": 2.0,
        "output": 10.0,
        "cached_input": 0.1,
    }


def test_usage_report_prices_json_includes_gpt62_with_effective_from() -> None:
    prices_path = (
        REPO_ROOT
        / "scripts"
        / "dalton"
        / "data_assets"
        / "skills"
        / "model-usage-cost"
        / "references"
        / "prices.json"
    )
    doc = json.loads(prices_path.read_text(encoding="utf-8"))
    by_model = {
        (row["provider"], row["model"]): row
        for row in doc["prices"]
        if isinstance(row, dict)
    }
    astra = by_model[("openai", "gpt-6.2-astra")]
    sol = by_model[("openai", "gpt-6.2-sol")]
    assert astra["effective_from"] == "2026-11-17"
    assert (astra["input"], astra["cacheWrite"], astra["cacheRead"], astra["output"]) == (
        8.0,
        10.0,
        0.4,
        40.0,
    )
    assert astra["tiers"][1]["from"] == 272001
    assert astra["tiers"][1]["input"] == 16.0
    assert astra["tiers"][1]["output"] == 60.0

    assert sol["effective_from"] == "2026-11-17"
    assert (sol["input"], sol["cacheWrite"], sol["cacheRead"], sol["output"]) == (
        2.0,
        2.5,
        0.1,
        10.0,
    )
    assert sol["tiers"][1]["from"] == 272001
    assert sol["tiers"][1]["input"] == 4.0
    assert sol["tiers"][1]["output"] == 15.0


def _make_fake_conn(
    *,
    automations: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
    sql_log: list[str],
) -> Any:
    cur = create_autospec(AsyncCursor, instance=True)
    fetch_queue: list[list[dict[str, Any]]] = [automations, threads, preferences]

    async def _execute(query: Any, params: Any = None, **kwargs: Any) -> Any:
        sql = " ".join(str(query).split())
        sql_log.append(sql)
        return cur

    async def _fetchall() -> list[dict[str, Any]]:
        return fetch_queue.pop(0) if fetch_queue else []

    cur.execute.side_effect = _execute
    cur.fetchall.side_effect = _fetchall

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm
    return conn


@pytest.mark.asyncio
async def test_switch_model_refs_plan_mode_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.dalton.database import model_switch as model_switch_db
    from src.dalton.services import company_agent_writes
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db
    from src.server.services.automations import lifecycle

    sql_log: list[str] = []
    fake_conn = _make_fake_conn(
        automations=[
            {
                "automation_id": "auto-1",
                "user_id": "u-1",
                "name": "Morning Brief",
                "scope": "personal",
                "llm_model": "gpt-6.1-sol-oauth",
                "config_version": 3,
                "instruction": "Normal prompt without model name.",
            },
            {
                "automation_id": "auto-2",
                "user_id": "company-sys",
                "name": "AlphaEngine Ingest 1",
                "scope": "company",
                "llm_model": "gpt-6-astra-oauth",
                "config_version": 7,
                "instruction": "步骤1：必须是 6.1 Sol，否则停止。",
            },
        ],
        threads=[
            {
                "conversation_thread_id": "th-1",
                "owner_user_id": "u-1",
                "llm_model": "gpt-6.1-sol-oauth",
            },
            {
                "conversation_thread_id": "th-2",
                "owner_user_id": "u-1",
                "llm_model": "gpt-6.1-sol-oauth",
            },
        ],
        preferences=[
            {
                "user_id": "u-1",
                "model_preference": {
                    "preferred_model": "gpt-6.1-sol-oauth",
                    "fallback_models": ["gpt-6-astra-oauth", "gpt-6.2-sol-oauth"],
                    "profiles": {"gpt-6.1-sol-oauth": {"reasoning_effort": "high"}},
                },
                "other_preference": {},
            }
        ],
        sql_log=sql_log,
    )

    @asynccontextmanager
    async def _fake_get_db_connection(*args: Any, **kwargs: Any):
        yield fake_conn

    update_auto_spy = create_autospec(lifecycle.update_automation)
    update_company_spy = create_autospec(company_agent_writes.update_company_agent)
    reassign_spy = create_autospec(model_switch_db.reassign_planned_thread_llm_models)
    upsert_prefs_spy = create_autospec(user_db.upsert_user_preferences)

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(lifecycle, "update_automation", update_auto_spy)
    monkeypatch.setattr(company_agent_writes, "update_company_agent", update_company_spy)
    monkeypatch.setattr(model_switch_db, "reassign_planned_thread_llm_models", reassign_spy)
    monkeypatch.setattr(user_db, "upsert_user_preferences", upsert_prefs_spy)

    rc = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--from",
                "gpt-6-astra-oauth",
                "--to",
                "gpt-6.2-astra-oauth",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PLAN (dry-run, no writes)" in out
    assert "1. automations.llm_model (deleted_at IS NULL): 2 row(s)" in out
    assert "2. conversation_threads.llm_model: 2 thread(s) across 1 owner/model group(s)" in out
    assert "3. user_preferences.model_preference: 1 user(s)" in out
    assert "4. automations.instruction mentions (read-only, NOT auto-edited): 1 automation(s)" in out
    assert "必须是 6.1 Sol，否则停止" in out
    assert sw.RECOMMENDED_INSTRUCTION_REWRITE in out

    assert all(not stmt.upper().startswith("UPDATE") for stmt in sql_log)
    update_auto_spy.assert_not_called()
    update_company_spy.assert_not_called()
    reassign_spy.assert_not_called()
    upsert_prefs_spy.assert_not_called()
    assert list(tmp_path.glob("model-switch-*.json")) == []


@pytest.mark.asyncio
async def test_switch_model_refs_apply_uses_services_writes_0600_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton import access_tiers
    from src.dalton.database import model_switch as model_switch_db
    from src.dalton.services import company_agent_writes
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db
    from src.server.services.automations import lifecycle

    sql_log: list[str] = []
    fake_conn = _make_fake_conn(
        automations=[
            {
                "automation_id": "auto-1",
                "user_id": "u-1",
                "name": "Morning Brief",
                "scope": "personal",
                "llm_model": "gpt-6.1-sol-oauth",
                "config_version": 3,
                "instruction": "Normal prompt.",
            },
            {
                "automation_id": "auto-2",
                "user_id": "company-sys",
                "name": "AlphaEngine Ingest 1",
                "scope": "company",
                "llm_model": "gpt-6-astra-oauth",
                "config_version": 7,
                "instruction": "必须是 6.1 Sol，否则停止。",
            },
        ],
        threads=[
            {
                "conversation_thread_id": "th-1",
                "owner_user_id": "u-1",
                "llm_model": "gpt-6.1-sol-oauth",
            },
            {
                "conversation_thread_id": "th-2",
                "owner_user_id": "u-1",
                "llm_model": "gpt-6.1-sol-oauth",
            },
        ],
        preferences=[
            {
                "user_id": "u-1",
                "model_preference": {
                    "preferred_model": "gpt-6.1-sol-oauth",
                    "fallback_models": ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth"],
                    "profiles": {"gpt-6.1-sol-oauth": {"reasoning_effort": "high"}},
                },
                "other_preference": {},
            }
        ],
        sql_log=sql_log,
    )

    @asynccontextmanager
    async def _fake_get_db_connection(*args: Any, **kwargs: Any):
        yield fake_conn

    async def _fake_update_automation(
        automation_id: str,
        user_id: str,
        fields: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert "instruction" not in fields
        return {"automation_id": automation_id, "user_id": user_id, **fields}

    async def _fake_update_company_agent(
        principal: Any,
        automation_id: str,
        fields: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert "instruction" not in fields
        return {"automation_id": automation_id, **fields}

    update_auto_spy = create_autospec(
        lifecycle.update_automation, side_effect=_fake_update_automation
    )
    update_company_spy = create_autospec(
        company_agent_writes.update_company_agent, side_effect=_fake_update_company_agent
    )
    reassign_planned_spy = create_autospec(
        model_switch_db.reassign_planned_thread_llm_models, return_value=2
    )
    lock_prefs_spy = create_autospec(
        user_db.lock_user_preferences,
        return_value={
            "user_id": "u-1",
            "model_preference": {
                "preferred_model": "gpt-6.1-sol-oauth",
                "fallback_models": ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth"],
                "profiles": {"gpt-6.1-sol-oauth": {"reasoning_effort": "high"}},
            },
            "other_preference": {},
        },
    )
    upsert_prefs_spy = create_autospec(
        user_db.upsert_user_preferences, return_value={"user_id": "u-1"}
    )
    invalidate_spy = create_autospec(user_db.invalidate_user_prefs_cache, return_value=None)
    actor_check_spy = create_autospec(access_tiers.is_internal_or_admin, return_value=True)

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(access_tiers, "is_internal_or_admin", actor_check_spy)
    monkeypatch.setattr(lifecycle, "update_automation", update_auto_spy)
    monkeypatch.setattr(company_agent_writes, "update_company_agent", update_company_spy)
    monkeypatch.setattr(
        model_switch_db, "reassign_planned_thread_llm_models", reassign_planned_spy
    )
    monkeypatch.setattr(user_db, "lock_user_preferences", lock_prefs_spy)
    monkeypatch.setattr(user_db, "upsert_user_preferences", upsert_prefs_spy)
    monkeypatch.setattr(user_db, "invalidate_user_prefs_cache", invalidate_spy)

    rc = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--from",
                "gpt-6-astra-oauth",
                "--to",
                "gpt-6.2-astra-oauth",
                "--apply",
                "--actor-user-id",
                "admin-1",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc == 0
    actor_check_spy.assert_awaited_once_with("admin-1")

    # No direct SQL UPDATE in script cursor.
    assert all(not stmt.upper().startswith("UPDATE") for stmt in sql_log)

    update_auto_spy.assert_called_once_with(
        "auto-1",
        "u-1",
        {"llm_model": "gpt-6.2-sol-oauth"},
        expected_config_version=3,
    )
    assert update_company_spy.call_count == 1
    assert update_company_spy.call_args.args[1] == "auto-2"
    assert update_company_spy.call_args.args[2] == {"llm_model": "gpt-6.2-astra-oauth"}

    reassign_planned_spy.assert_called_once_with(
        "u-1", "gpt-6.1-sol-oauth", "gpt-6.2-sol-oauth", ["th-1", "th-2"], conn=fake_conn
    )
    lock_prefs_spy.assert_awaited_once_with("u-1", conn=fake_conn)
    upsert_prefs_spy.assert_called_once_with(
        user_id="u-1",
        model_preference={
            "preferred_model": "gpt-6.2-sol-oauth",
            "fallback_models": ["gpt-6.2-astra-oauth", "gpt-6.2-sol-oauth"],
            "profiles": {
                "gpt-6.2-sol-oauth": {"reasoning_effort": "high"},
                "gpt-6.1-sol-oauth": None,
            },
        },
        conn=fake_conn,
        expect_other_preference=None,
    )
    invalidate_spy.assert_called_once_with("u-1")

    archives = list(tmp_path.glob("model-switch-*.json"))
    assert len(archives) == 1
    mode = stat.S_IMODE(archives[0].stat().st_mode)
    assert mode == 0o600
    saved = json.loads(archives[0].read_text(encoding="utf-8"))
    assert saved["automations"][0]["llm_model"] == "gpt-6.1-sol-oauth"
    assert saved["automations"][1]["llm_model"] == "gpt-6-astra-oauth"
    assert saved["threads"][0]["planned_count"] == 2
    assert saved["user_preferences"][0]["original_model_preference"]["preferred_model"] == "gpt-6.1-sol-oauth"
    assert saved["skipped_user_preferences"] == []


@pytest.mark.asyncio
async def test_switch_model_refs_company_automation_without_actor_exits_before_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton import access_tiers
    from src.dalton.database import model_switch as model_switch_db
    from src.dalton.services import company_agent_writes
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db
    from src.server.services.automations import lifecycle

    sql_log: list[str] = []
    fake_conn = _make_fake_conn(
        automations=[
            {
                "automation_id": "auto-co-1",
                "user_id": "company-sys",
                "name": "Company Task",
                "scope": "company",
                "llm_model": "gpt-6.1-sol-oauth",
                "config_version": 2,
                "instruction": "",
            },
            {
                "automation_id": "auto-p-1",
                "user_id": "u-1",
                "name": "Personal Task",
                "scope": "personal",
                "llm_model": "gpt-6.1-sol-oauth",
                "config_version": 1,
                "instruction": "",
            },
        ],
        threads=[],
        preferences=[],
        sql_log=sql_log,
    )

    @asynccontextmanager
    async def _fake_get_db_connection(*args: Any, **kwargs: Any):
        yield fake_conn

    update_auto_spy = create_autospec(lifecycle.update_automation)
    update_company_spy = create_autospec(company_agent_writes.update_company_agent)
    reassign_spy = create_autospec(model_switch_db.reassign_planned_thread_llm_models)
    upsert_spy = create_autospec(user_db.upsert_user_preferences)

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(lifecycle, "update_automation", update_auto_spy)
    monkeypatch.setattr(company_agent_writes, "update_company_agent", update_company_spy)
    monkeypatch.setattr(model_switch_db, "reassign_planned_thread_llm_models", reassign_spy)
    monkeypatch.setattr(user_db, "upsert_user_preferences", upsert_spy)

    # 1. Missing --actor-user-id -> exits 1 before archive or any write.
    rc_missing = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--apply",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc_missing == 1
    update_auto_spy.assert_not_called()
    update_company_spy.assert_not_called()
    reassign_spy.assert_not_called()
    upsert_spy.assert_not_called()
    assert list(tmp_path.glob("model-switch-*.json")) == []

    # 2. External user passed as --actor-user-id -> rejected via is_internal_or_admin before writes.
    monkeypatch.setattr(
        access_tiers,
        "is_internal_or_admin",
        create_autospec(access_tiers.is_internal_or_admin, return_value=False),
    )
    fake_conn2 = _make_fake_conn(
        automations=[
            {
                "automation_id": "auto-co-1",
                "user_id": "company-sys",
                "name": "Company Task",
                "scope": "company",
                "llm_model": "gpt-6.1-sol-oauth",
                "config_version": 2,
                "instruction": "",
            }
        ],
        threads=[],
        preferences=[],
        sql_log=[],
    )

    @asynccontextmanager
    async def _fake_get_db_connection2(*args: Any, **kwargs: Any):
        yield fake_conn2

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection2)
    rc_external = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--apply",
                "--actor-user-id",
                "external-user-1",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc_external == 1
    update_company_spy.assert_not_called()
    assert list(tmp_path.glob("model-switch-*.json")) == []


@pytest.mark.asyncio
async def test_reassign_planned_thread_llm_models_scopes_to_planned_ids_and_rolls_back_on_mismatch() -> None:
    from src.dalton.database.model_switch import reassign_planned_thread_llm_models

    executed: list[tuple[str, Any]] = []
    tx_events: list[str] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _execute(query: Any, params: Any = None, **kwargs: Any) -> Any:
        executed.append((" ".join(str(query).split()), params))
        return cur

    cur.execute.side_effect = _execute
    cur.rowcount = 2

    @asynccontextmanager
    async def _cursor_cm(*args: Any, **kwargs: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        tx_events.append("enter")
        try:
            yield
        except BaseException:
            tx_events.append("rollback")
            raise
        else:
            tx_events.append("commit")

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    moved = await reassign_planned_thread_llm_models(
        "u-1",
        "gpt-6.1-sol-oauth",
        "gpt-6.2-sol-oauth",
        ["th-1", "th-2"],
        conn=conn,
    )
    assert moved == 2
    assert tx_events == ["enter", "commit"]
    sql, params = executed[0]
    assert "t.conversation_thread_id = ANY(%(thread_ids)s)" in sql
    assert "t.llm_model = %(old)s" in sql
    assert "w.status != 'deleted'" in sql
    assert "(t.user_id = %(user_id)s OR (t.user_id IS NULL AND w.user_id = %(user_id)s))" in sql
    assert params["thread_ids"] == ["th-1", "th-2"]

    # Rowcount mismatch (e.g. one planned thread changed concurrently) -> rolls back before commit.
    tx_events.clear()
    cur.rowcount = 1
    with pytest.raises(RuntimeError, match="Thread reassign mismatch"):
        await reassign_planned_thread_llm_models(
            "u-1",
            "gpt-6.1-sol-oauth",
            "gpt-6.2-sol-oauth",
            ["th-1", "th-2"],
            conn=conn,
        )
    assert tx_events == ["enter", "rollback"]


@pytest.mark.asyncio
async def test_switch_model_refs_preference_cas_conflict_skips_without_overwriting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.database import model_switch as model_switch_db
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db

    fake_conn = _make_fake_conn(
        automations=[],
        threads=[],
        preferences=[
            {
                "user_id": "u-conflict",
                "model_preference": {"preferred_model": "gpt-6.1-sol-oauth"},
                "other_preference": {},
            },
            {
                "user_id": "u-legacy-cas",
                "model_preference": {},
                "other_preference": {"preferred_model": "gpt-6.1-sol-oauth"},
            },
            {
                "user_id": "u-ok",
                "model_preference": {"preferred_model": "gpt-6.1-sol-oauth"},
                "other_preference": {},
            },
        ],
        sql_log=[],
    )

    @asynccontextmanager
    async def _fake_get_db_connection(*args: Any, **kwargs: Any):
        yield fake_conn

    async def _fake_lock(uid: str, *, conn: Any = None) -> dict[str, Any]:
        if uid == "u-conflict":
            # User changed preferred_model after plan was read -> CAS conflict must skip!
            return {
                "user_id": uid,
                "model_preference": {"preferred_model": "claude-opus-5-5-oauth"},
                "other_preference": {},
            }
        if uid == "u-legacy-cas":
            return {
                "user_id": uid,
                "model_preference": {},
                "other_preference": {"preferred_model": "gpt-6.1-sol-oauth"},
            }
        return {
            "user_id": uid,
            "model_preference": {"preferred_model": "gpt-6.1-sol-oauth"},
            "other_preference": {},
        }

    upserted_users: list[tuple[str, dict[str, Any]]] = []

    async def _fake_upsert(
        user_id: str,
        *,
        model_preference: dict[str, Any] | None = None,
        expect_other_preference: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if user_id == "u-legacy-cas":
            assert expect_other_preference == {"preferred_model": "gpt-6.1-sol-oauth"}
            raise user_db.PreferenceChanged(["preferred_model"])
        upserted_users.append((user_id, model_preference or {}))
        return {"user_id": user_id}

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(
        model_switch_db,
        "reassign_planned_thread_llm_models",
        create_autospec(model_switch_db.reassign_planned_thread_llm_models, return_value=0),
    )
    monkeypatch.setattr(
        user_db,
        "lock_user_preferences",
        create_autospec(user_db.lock_user_preferences, side_effect=_fake_lock),
    )
    monkeypatch.setattr(
        user_db,
        "upsert_user_preferences",
        create_autospec(user_db.upsert_user_preferences, side_effect=_fake_upsert),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_prefs_cache",
        create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
    )

    rc = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--apply",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc == 0
    assert upserted_users == [("u-ok", {"preferred_model": "gpt-6.2-sol-oauth"})]

    archives = list(tmp_path.glob("model-switch-*.json"))
    assert len(archives) == 1
    saved = json.loads(archives[0].read_text(encoding="utf-8"))
    assert [row["user_id"] for row in saved["user_preferences"]] == ["u-ok"]
    assert [row["user_id"] for row in saved["skipped_user_preferences"]] == [
        "u-conflict",
        "u-legacy-cas",
    ]


@pytest.mark.asyncio
async def test_switch_model_refs_mid_run_failure_prints_progress_summary_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from src.dalton.database import model_switch as model_switch_db
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db
    from src.server.services.automations import lifecycle

    fake_conn = _make_fake_conn(
        automations=[
            {
                "automation_id": "auto-1",
                "user_id": "u-1",
                "name": "A1",
                "scope": "personal",
                "llm_model": "gpt-6.1-sol-oauth",
                "config_version": 1,
                "instruction": "",
            },
        ],
        threads=[
            {
                "conversation_thread_id": "th-1",
                "owner_user_id": "u-1",
                "llm_model": "gpt-6.1-sol-oauth",
            },
            {
                "conversation_thread_id": "th-2",
                "owner_user_id": "u-1",
                "llm_model": "gpt-6.1-sol-oauth",
            },
        ],
        preferences=[
            {
                "user_id": "u-1",
                "model_preference": {"preferred_model": "gpt-6.1-sol-oauth"},
                "other_preference": {},
            }
        ],
        sql_log=[],
    )

    @asynccontextmanager
    async def _fake_get_db_connection(*args: Any, **kwargs: Any):
        yield fake_conn

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(
        lifecycle,
        "update_automation",
        create_autospec(
            lifecycle.update_automation,
            return_value={"automation_id": "auto-1", "llm_model": "gpt-6.2-sol-oauth"},
        ),
    )
    # Thread phase fails due to rowcount mismatch after automation already succeeded.
    monkeypatch.setattr(
        model_switch_db,
        "reassign_planned_thread_llm_models",
        create_autospec(
            model_switch_db.reassign_planned_thread_llm_models,
            side_effect=RuntimeError("Thread reassign mismatch for user=u-1"),
        ),
    )
    upsert_spy = create_autospec(user_db.upsert_user_preferences)
    monkeypatch.setattr(user_db, "upsert_user_preferences", upsert_spy)

    rc = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--apply",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "Switch Progress Summary" in captured.err
    assert "Automations completed (1): ['auto-1']" in captured.err
    assert "Thread group failed:" in captured.err
    assert "Preferences pending (1): ['u-1']" in captured.err
    upsert_spy.assert_not_called()
    assert len(list(tmp_path.glob("model-switch-*.json"))) == 1


@pytest.mark.asyncio
async def test_lifecycle_recheck_runs_real_code_after_pr384(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify recheck_locked_automation_update succeeds with only _check_claude_code_hosted mocked."""
    from src.server.models.automation import AutomationUpdate
    from src.server.services.automations import lifecycle

    hosted_spy = create_autospec(lifecycle.check_claude_code_hosted, return_value=None)
    monkeypatch.setattr(lifecycle, "check_claude_code_hosted", hosted_spy)

    ws_uuid = "11111111-1111-1111-1111-111111111111"
    update = AutomationUpdate.model_validate(
        {"llm_model": "gpt-6.2-sol-oauth"},
        context={"trigger_type": "cron"},
    )
    effective_kwargs = {"llm_model": "gpt-6.2-sol-oauth"}
    await lifecycle.recheck_locked_automation_update(
        user_id="u-1",
        update=update,
        current={"workspace_id": ws_uuid, "llm_model": "gpt-6.1-sol-oauth", "thread_strategy": "new"},
        stored_in=ws_uuid,
        stored_pin=None,
        new_cron=None,
        new_tz=None,
        pref_for_lock={},
        locked_row={
            "workspace_id": ws_uuid,
            "llm_model": "gpt-6-astra-oauth",
            "thread_strategy": "new",
            "trigger_type": "cron",
            "status": "active",
        },
        effective_kwargs=effective_kwargs,
        locked_conn=None,
    )
    hosted_spy.assert_awaited_once_with("u-1", "gpt-6.2-sol-oauth", ws_uuid, None, {})


@pytest.mark.asyncio
async def test_switch_model_refs_updates_starred_and_hidden_lists_and_skips_on_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.database import model_switch as model_switch_db
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db

    fake_conn = _make_fake_conn(
        automations=[],
        threads=[],
        preferences=[
            {
                "user_id": "u-lists-ok",
                "model_preference": {},
                "other_preference": {
                    "starred_models": ["gpt-6.1-sol-oauth", "claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"],
                    "hidden_quick_access_models": ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth", "gpt-6.2-astra-oauth"],
                },
            },
            {
                "user_id": "u-starred-conflict",
                "model_preference": {},
                "other_preference": {
                    "starred_models": ["gpt-6.1-sol-oauth"],
                },
            },
            {
                "user_id": "u-hidden-cas-race",
                "model_preference": {},
                "other_preference": {
                    "hidden_quick_access_models": ["gpt-6.1-sol-oauth"],
                },
            },
        ],
        sql_log=[],
    )

    @asynccontextmanager
    async def _fake_get_db_connection(*args: Any, **kwargs: Any):
        yield fake_conn

    async def _fake_lock(uid: str, *, conn: Any = None) -> dict[str, Any]:
        if uid == "u-starred-conflict":
            return {
                "user_id": uid,
                "model_preference": {},
                "other_preference": {
                    # Changed after plan read -> must skip!
                    "starred_models": ["gpt-6.1-sol-oauth", "grok-4.7-oauth"],
                },
            }
        if uid == "u-hidden-cas-race":
            return {
                "user_id": uid,
                "model_preference": {},
                "other_preference": {
                    "hidden_quick_access_models": ["gpt-6.1-sol-oauth"],
                },
            }
        return {
            "user_id": uid,
            "model_preference": {},
            "other_preference": {
                "starred_models": ["gpt-6.1-sol-oauth", "claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"],
                "hidden_quick_access_models": ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth", "gpt-6.2-astra-oauth"],
            },
        }

    upserted_calls: list[dict[str, Any]] = []

    async def _fake_upsert(
        user_id: str,
        *,
        model_preference: dict[str, Any] | None = None,
        other_preference: dict[str, Any] | None = None,
        expect_other_preference: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if user_id == "u-hidden-cas-race":
            assert expect_other_preference == {
                "hidden_quick_access_models": ["gpt-6.1-sol-oauth"]
            }
            raise user_db.PreferenceChanged(["hidden_quick_access_models"])
        upserted_calls.append(
            {
                "user_id": user_id,
                "model_preference": model_preference,
                "other_preference": other_preference,
                "expect_other_preference": expect_other_preference,
            }
        )
        return {"user_id": user_id}

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(
        model_switch_db,
        "reassign_planned_thread_llm_models",
        create_autospec(model_switch_db.reassign_planned_thread_llm_models, return_value=0),
    )
    monkeypatch.setattr(
        user_db,
        "lock_user_preferences",
        create_autospec(user_db.lock_user_preferences, side_effect=_fake_lock),
    )
    monkeypatch.setattr(
        user_db,
        "upsert_user_preferences",
        create_autospec(user_db.upsert_user_preferences, side_effect=_fake_upsert),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_prefs_cache",
        create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
    )

    rc = await sw._run(
        sw.build_parser().parse_args(
            [
                "--from",
                "gpt-6.1-sol-oauth",
                "--to",
                "gpt-6.2-sol-oauth",
                "--from",
                "gpt-6-astra-oauth",
                "--to",
                "gpt-6.2-astra-oauth",
                "--apply",
                "--archive-dir",
                str(tmp_path),
            ]
        )
    )
    assert rc == 0
    assert upserted_calls == [
        {
            "user_id": "u-lists-ok",
            "model_preference": None,
            "other_preference": {
                "starred_models": ["gpt-6.2-sol-oauth", "claude-opus-5-5-oauth"],
                "hidden_quick_access_models": ["gpt-6.2-astra-oauth", "gpt-6.2-sol-oauth"],
            },
            "expect_other_preference": {
                "starred_models": ["gpt-6.1-sol-oauth", "claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"],
                "hidden_quick_access_models": ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth", "gpt-6.2-astra-oauth"],
            },
        }
    ]

    archives = list(tmp_path.glob("model-switch-*.json"))
    assert len(archives) == 1
    saved = json.loads(archives[0].read_text(encoding="utf-8"))
    assert [row["user_id"] for row in saved["user_preferences"]] == ["u-lists-ok"]
    assert saved["user_preferences"][0]["original_other_preference"] == {
        "starred_models": ["gpt-6.1-sol-oauth", "claude-opus-5-5-oauth", "gpt-6.2-sol-oauth"],
        "hidden_quick_access_models": ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth", "gpt-6.2-astra-oauth"],
    }
    assert [row["user_id"] for row in saved["skipped_user_preferences"]] == [
        "u-starred-conflict",
        "u-hidden-cas-race",
    ]
