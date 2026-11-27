"""Unit tests for weekly-token-usage migration to Dalton (T1-T4)."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import AsyncConnection, AsyncCursor

from scripts.dalton.researchdata_bridge import resolve_host_run_command_spec
from src.dalton.app.internal_usage_router import (
    BY_MODEL_AGGREGATION_SQL,
    BY_MODEL_DAILY_AGGREGATION_SQL,
    fetch_usage_by_model,
)
from src.dalton.app.router import router as dalton_api_router
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.data_jobs.notification_payloads import build_data_task_delivery_plan
from src.dalton.data_jobs.runner_wrapper import resolve_task_payload_source

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_SCRIPTS_DIR = REPO_ROOT / "scripts" / "dalton" / "data_assets" / "skills" / "model-usage-cost" / "scripts"
TRACKER_PATH = REPO_ROOT / "scripts" / "dalton" / "data_assets" / "scripts" / "token_weekly_usage_tracker.py"


def _load_tracker():
    name = "dalton_token_weekly_usage_tracker_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, TRACKER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_autospec_conn(rows: list[dict]):
    conn = create_autospec(AsyncConnection, instance=True)
    cur = create_autospec(AsyncCursor, instance=True)
    captured: dict[str, object] = {}

    async def fake_execute(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return None

    cur.execute = AsyncMock(side_effect=fake_execute)
    cur.fetchall = AsyncMock(return_value=rows)

    @contextlib.asynccontextmanager
    async def cursor_cm(*_args, **_kwargs):
        yield cur

    conn.cursor = cursor_cm
    return conn, captured


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dalton_api_router)
    return app


# ---------------------------------------------------------------------------
# T1: Internal usage by-model endpoint & SQL mapping
# ---------------------------------------------------------------------------


VALID_SERVICE_TOKEN = "test-internal-service-token-1234567890abcdef"


def test_t1_usage_endpoint_requires_valid_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", VALID_SERVICE_TOKEN)
    client = TestClient(_make_app())
    url = "/api/v1/dalton/internal/usage/by-model?from=2026-11-07T00:00:00Z&to=2026-11-14T00:00:00Z"

    resp_missing = client.get(url)
    assert resp_missing.status_code == 401

    resp_wrong = client.get(url, headers={"X-Service-Token": "wrong-secret-01234567890123456789012345"})
    assert resp_wrong.status_code == 401


def test_t1_usage_endpoint_rejects_window_over_31_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", VALID_SERVICE_TOKEN)
    client = TestClient(_make_app())
    resp = client.get(
        "/api/v1/dalton/internal/usage/by-model?from=2026-10-01T00:00:00Z&to=2026-11-02T00:00:01Z",
        headers={"X-Service-Token": VALID_SERVICE_TOKEN},
    )
    assert resp.status_code == 400
    assert "31" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_t1_fetch_usage_by_model_sql_and_row_mapping() -> None:
    fake_rows = [
        {
            "model": "gemini-4-argon-high-oauth",
            "calls": 120,
            "fresh_input": 800_000,
            "cache_read": 200_000,
            "cache_write": 0,
            "output": 50_000,
            "total_tokens": 1_050_000,
            "runs": 42,
            "total_runs": 50,
        },
        {
            "model": "claude-opus-5-5",
            "calls": 30,
            "fresh_input": 100_000,
            "cache_read": 400_000,
            "cache_write": 0,
            "output": 20_000,
            "total_tokens": 520_000,
            "runs": 15,
            "total_runs": 50,
        },
    ]
    conn, captured = _make_autospec_conn(fake_rows)
    from_dt = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
    to_dt = datetime(2026, 11, 14, 0, 0, tzinfo=timezone.utc)

    result = await fetch_usage_by_model(conn, from_dt=from_dt, to_dt=to_dt)

    assert "jsonb_each" in str(captured["sql"])
    assert captured["sql"] == BY_MODEL_AGGREGATION_SQL
    assert captured["params"] == {"from_dt": from_dt, "to_dt": to_dt}
    assert result["input_includes_cached"] is True
    assert len(result["models"]) == 2
    assert result["models"][0]["model"] == "gemini-4-argon-high-oauth"
    assert result["models"][0]["fresh_input"] == 800_000
    assert result["totals"] == {
        "calls": 150,
        "fresh_input": 900_000,
        "cache_read": 600_000,
        "cache_write": 0,
        "output": 70_000,
        "total_tokens": 1_570_000,
        "runs": 50,
    }


def test_t1_endpoint_returns_mapped_payload_via_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", VALID_SERVICE_TOKEN)
    fake_rows = [
        {
            "model": "deepseek-flash",
            "calls": 5,
            "fresh_input": 1000,
            "cache_read": 500,
            "cache_write": 0,
            "output": 250,
            "total_tokens": 1750,
            "runs": 2,
            "total_runs": 2,
        }
    ]
    conn, _ = _make_autospec_conn(fake_rows)

    @contextlib.asynccontextmanager
    async def fake_pool_conn():
        yield conn

    with patch("src.dalton.app.internal_usage_router.pool.get_db_connection", fake_pool_conn):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/v1/dalton/internal/usage/by-model?from=2026-11-07T00:00:00Z&to=2026-11-14T00:00:00Z",
            headers={"X-Service-Token": VALID_SERVICE_TOKEN},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"][0]["model"] == "deepseek-flash"
    assert body["totals"]["runs"] == 2
    assert "user_id" not in json.dumps(body)


@pytest.mark.asyncio
async def test_r3_bucket_day_sql_and_endpoint_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_rows = [
        {
            "date": "2026-11-30",
            "model": "gemini-4-argon-high-oauth",
            "calls": 10,
            "fresh_input": 1_000_000,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "total_tokens": 1_000_000,
            "runs": 4,
            "total_runs": 7,
        },
        {
            "date": "2026-12-01",
            "model": "gemini-4-argon-high-oauth",
            "calls": 15,
            "fresh_input": 1_000_000,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "total_tokens": 1_000_000,
            "runs": 3,
            "total_runs": 7,
        },
    ]
    conn, captured = _make_autospec_conn(fake_rows)
    from_dt = datetime(2026, 11, 28, 4, 0, tzinfo=timezone.utc)
    to_dt = datetime(2026, 12, 5, 4, 0, tzinfo=timezone.utc)

    result = await fetch_usage_by_model(conn, from_dt=from_dt, to_dt=to_dt, bucket="day")
    assert captured["sql"] == BY_MODEL_DAILY_AGGREGATION_SQL
    assert "AT TIME ZONE 'UTC'" in str(captured["sql"])
    assert result["bucket"] == "day"
    assert result["timezone"] == "UTC"
    assert [m["date"] for m in result["models"]] == ["2026-11-30", "2026-12-01"]
    assert result["totals"]["runs"] == 7

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", VALID_SERVICE_TOKEN)

    @contextlib.asynccontextmanager
    async def fake_pool_conn():
        yield conn

    with patch("src.dalton.app.internal_usage_router.pool.get_db_connection", fake_pool_conn):
        client = TestClient(_make_app())
        ok_resp = client.get(
            "/api/v1/dalton/internal/usage/by-model?from=2026-11-28T04:00:00Z&to=2026-12-05T04:00:00Z&bucket=day",
            headers={"X-Service-Token": VALID_SERVICE_TOKEN},
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json()["timezone"] == "UTC"
        bad_resp = client.get(
            "/api/v1/dalton/internal/usage/by-model?from=2026-11-28T04:00:00Z&to=2026-12-05T04:00:00Z&bucket=week",
            headers={"X-Service-Token": VALID_SERVICE_TOKEN},
        )
        assert bad_resp.status_code == 400


# ---------------------------------------------------------------------------
# T3: Weekly card layout, outbox payload & --self-test without ~/.openclaw
# ---------------------------------------------------------------------------


def test_t3_card_sections_and_outbox_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _load_tracker()
    raw_data = {
        "version": "3.1.0",
        "price_catalog": {
            "generated": "2026-11-14",
            "effective_notes": ["gemini-4-argon (2026-12-01 起): 12-01 起涨价"],
        },
        "dalton_prod": {"ok": True, "error": None, "runs": 88, "calls": 210, "models": 1},
        "windows": [
            {
                "days": 7.0,
                "from": "2026-11-07T04:00:00+00:00",
                "to": "2026-11-14T04:00:00+00:00",
                "rows": [
                    {
                        "source": "openclaw",
                        "provider": "openai",
                        "model": "gpt-6-luna",
                        "calls": 10,
                        "fresh": 10_000,
                        "cache_write": 0,
                        "cache_read": 20_000,
                        "output": 2_000,
                        "cost_usd": 3.5,
                    },
                    {
                        "source": "Dalton 生产（订阅）",
                        "provider": "google",
                        "model": "gemini-4-argon-high-oauth",
                        "calls": 1,
                        "runs": 88,
                        "fresh": 500_000,
                        "cache_write": 0,
                        "cache_read": 100_000,
                        "output": 50_000,
                        "cost_usd": 1.51,
                    },
                    {
                        "source": "muse-cli(手动)",
                        "provider": "muse-cli-gateway",
                        "model": "muse-wonder-full-1.0",
                        "calls": 3,
                        "fresh": 100_000,
                        "cache_write": 0,
                        "cache_read": 50_000,
                        "output": 10_000,
                        "cost_usd": None,
                        "unpriced_tokens": 160_000,
                        "no_public_price": True,
                        "reference_cost_usd": 0.61,
                        "reference_model": "muse-wonder-1.0",
                    },
                ],
            }
        ],
    }
    stats = tracker.process_usage_stats(raw_data)
    card = tracker.build_card(stats)
    card_text = json.dumps(card, ensure_ascii=False)
    assert "本机命令行工具" in card_text
    assert "Dalton 生产" in card_text
    assert "合计" in card_text
    assert "无公开价模型" in card_text
    assert "参考价估算" in card_text
    assert "2026-11-14" in card_text
    assert "12-01 起涨价" in card_text

    outbox_path = tmp_path / "outbox.json"
    tracker.write_outbox_card(outbox_path, card)
    outbox = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert isinstance(outbox.get("feishu_cards"), list)
    assert len(outbox["feishu_cards"]) == 1
    assert outbox["feishu_cards"][0]["card"] == card

    plan = build_data_task_delivery_plan(
        run_id=str(uuid.uuid4()),
        job_key="weekly-token-usage",
        raw_outbox=outbox,
        run_artifacts_dir=tmp_path,
    )
    assert len(plan.items) == 1


def test_t3_dry_run_prints_card_json_and_outbox_mode_writes_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tracker = _load_tracker()
    snap = tmp_path / "usage.json"
    snap.write_text(
        json.dumps(
            {
                "version": "3.1.0",
                "price_catalog": {"generated": "2026-11-14", "effective_notes": []},
                "dalton_prod": {"ok": False, "error": "HTTP 502: Bad Gateway", "runs": 0, "calls": 0, "models": 0},
                "windows": [
                    {
                        "days": 7.0,
                        "from": "2026-11-07T00:00:00+00:00",
                        "to": "2026-11-14T00:00:00+00:00",
                        "rows": [
                            {
                                "source": "openclaw",
                                "provider": "openai",
                                "model": "gpt-6-luna",
                                "calls": 2,
                                "fresh": 1000,
                                "cache_write": 0,
                                "cache_read": 0,
                                "output": 100,
                                "cost_usd": 0.01,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with patch.object(sys, "argv", ["token_weekly_usage_tracker.py", "--input-json", str(snap), "--dry-run"]):
        assert tracker.main() == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["schema"] == "2.0"
    assert "Dalton 生产用量读取失败：HTTP 502: Bad Gateway" in json.dumps(captured, ensure_ascii=False)

    outbox_path = tmp_path / "stage" / "outbox.json"
    with patch.dict("os.environ", {"DALTON_OUTBOX_FILE": str(outbox_path)}), \
         patch.object(sys, "argv", ["token_weekly_usage_tracker.py", "--input-json", str(snap)]):
        assert tracker.main() == 0
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["feishu_cards"][0]["card"] == captured


def test_t3_self_test_runs_without_openclaw_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _load_tracker()
    empty_home = tmp_path / "no-openclaw-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    assert tracker.self_test() == 0


def test_r2_argon_enum_1417_and_d_variant_merge_in_ranking_vendor_and_card(monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime as dt

    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_sources

    tracker = _load_tracker()
    now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=usage_sources.UTC).timestamp()
    e = usage_sources.Evidence(now - 7 * 86400, now)
    e.put("agy", "r1", now - 30, "antigravity-cli-gateway", "gemini-4-argon", "agy-cli(含网关)", (1_000_000, 0, 0, 0), conversation="c1", enum=1417, unit="model-response")
    e.put("agy", "r2", now - 20, "antigravity-cli-gateway", "agy-enum-1417", "agy-cli(含网关)", (2_000_000, 0, 0, 0), conversation="c2", enum=1417, unit="model-response")
    e.put("agy", "r3", now - 10, "antigravity-cli-gateway", "gemini-4-argon-d", "agy-cli(含网关)", (3_000_000, 0, 0, 0), conversation="c3", enum=1417, unit="model-response")

    records, held = usage_sources.reconcile(e)
    assert held == []
    prices = cli.load_prices()
    snapshot = {
        "version": "3.1.0",
        "price_catalog": {"generated": prices.get("generated"), "effective_notes": prices.get("effective_notes", [])},
        "dalton_prod": {"ok": True, "runs": 0, "calls": 0, "models": 0},
        "coverage": {"issues": e.issues, "stats": dict(e.stats)},
        "windows": [cli.window_result(records, now, 7, prices, by_model=True)],
    }

    parsed = tracker.process_usage_stats(snapshot)
    all_rows = parsed["top10"] + parsed["tail"]
    assert len(all_rows) == 1
    assert all_rows[0]["model"] == "Gemini 4 Argon"
    assert all_rows[0]["series_id"] == "antigravity-oauth:gemini-4-argon"
    assert all_rows[0]["total_tokens"] == 6_000_000
    assert all_rows[0]["cost_usd"] == pytest.approx(12.0)
    assert list(all_rows[0]["effort_breakdown"].keys()) == ["unknown"]
    assert all_rows[0]["effort_breakdown"]["unknown"]["total_tokens"] == 6_000_000

    assert len(parsed["prov_list"]) == 1
    assert parsed["prov_list"][0]["tokens"] == 6_000_000
    assert parsed["prov_list"][0]["cost"] == pytest.approx(12.0)
    assert parsed["unpriced_tokens"] == 0
    assert parsed["unpriced_models"] == []

    card_text = json.dumps(tracker.build_card(parsed), ensure_ascii=False)
    assert "Gemini 4 Argon" in card_text
    assert "档位未知" in card_text
    assert "agy-enum-1417" not in card_text
    assert "gemini-4-argon-d" not in card_text
    assert "**无公开价模型**：本期无" in card_text
    assert "模型待识别" not in card_text


def _build_cli_snapshot(
    cli,
    usage_sources,
    e,
    now_ts: float,
    *,
    days: int = 7,
) -> dict:
    records, held = usage_sources.reconcile(e)
    prices, catalog_meta = cli.load_price_catalog()
    return {
        "version": cli.VERSION,
        "generated_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "price_catalog": catalog_meta,
        "dalton_prod": e.dalton_prod or {"ok": False, "error": "not queried", "models": 0, "runs": 0, "calls": 0},
        "windows": [cli.window_result(records, now_ts, days, prices, by_model=True)],
        "coverage": {
            "status": "partial" if e.issues or held else "local-observations-only",
            "issues": dict(sorted(e.issues.items())),
            "stats": dict(e.stats),
            "unresolved_aggregate_records": len(held),
            "unresolved_aggregate_tokens_not_additive": sum(sum(r["v"]) for r in held),
        },
        "notes": [],
    }


def test_r3_dalton_prod_success_with_calls_gt_one_builds_card_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_sources

    tracker = _load_tracker()
    since_ts = datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc).timestamp()
    until_ts = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc).timestamp()
    e = usage_sources.Evidence(since_ts, until_ts)

    prod_payload = {
        "bucket": "day",
        "timezone": "UTC",
        "models": [
            {
                "date": "2026-11-12",
                "model": "claude-opus-5-5-oauth",
                "calls": 5908,
                "fresh_input": 58_364_563,
                "cache_read": 977_747_079,
                "cache_write": 0,
                "output": 6_494_623,
                "total_tokens": 1_042_606_265,
                "runs": 980,
            },
            {
                "date": "2026-11-13",
                "model": "gemini-4-argon-high-oauth",
                "calls": 320,
                "fresh_input": 10_000_000,
                "cache_read": 40_000_000,
                "cache_write": 0,
                "output": 1_200_000,
                "total_tokens": 51_200_000,
                "runs": 114,
            },
        ],
        "totals": {
            "calls": 6228,
            "fresh_input": 68_364_563,
            "cache_read": 1_017_747_079,
            "cache_write": 0,
            "output": 7_694_623,
            "total_tokens": 1_093_806_265,
            "runs": 1094,
        },
    }
    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DALTON_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok")
    with patch.object(usage_sources, "fetch_dalton_prod_by_model", autospec=True, return_value=prod_payload):
        assert usage_sources.scan_dalton_prod(e, window_days=7.0, window_since=since_ts, window_until=until_ts)

    snapshot = _build_cli_snapshot(cli, usage_sources, e, until_ts, days=7)
    stats = tracker.process_usage_stats(snapshot)
    assert stats["total_records"] == 2
    assert stats["total_calls"] == 6228
    assert stats["dalton_stats"]["runs"] == 1094
    assert stats["dalton_stats"]["api_calls"] == 6228
    card = tracker.build_card(stats)
    card_text = json.dumps(card, ensure_ascii=False)
    assert "claude-opus-5-5-oauth" in card_text
    assert "1,094 次运行 / 6,228 次调用" in card_text


def test_r3_cross_price_change_day_costs_two_plus_four_dollars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_sources

    tracker = _load_tracker()
    since_ts = datetime(2026, 11, 28, 4, 0, tzinfo=timezone.utc).timestamp()
    until_ts = datetime(2026, 12, 5, 4, 0, tzinfo=timezone.utc).timestamp()
    e = usage_sources.Evidence(since_ts, until_ts)

    prod_payload = {
        "bucket": "day",
        "timezone": "UTC",
        "models": [
            {
                "date": "2026-11-30",
                "model": "gemini-4-argon-oauth",
                "calls": 5,
                "fresh_input": 1_000_000,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
                "total_tokens": 1_000_000,
                "runs": 3,
            },
            {
                "date": "2026-12-01",
                "model": "gemini-4-argon-oauth",
                "calls": 8,
                "fresh_input": 1_000_000,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
                "total_tokens": 1_000_000,
                "runs": 4,
            },
        ],
        "totals": {
            "calls": 13,
            "fresh_input": 2_000_000,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "total_tokens": 2_000_000,
            "runs": 7,
        },
    }
    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DALTON_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok")
    with patch.object(usage_sources, "fetch_dalton_prod_by_model", autospec=True, return_value=prod_payload):
        assert usage_sources.scan_dalton_prod(e, window_days=7.0, window_since=since_ts, window_until=until_ts)

    snapshot = _build_cli_snapshot(cli, usage_sources, e, until_ts, days=7)
    stats = tracker.process_usage_stats(snapshot)
    assert stats["dalton_stats"]["cost"] == pytest.approx(6.0)
    assert stats["total_cost"] == pytest.approx(6.0)


def test_r3_local_and_dalton_same_model_split_then_merged_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_sources

    tracker = _load_tracker()
    since_ts = datetime(2026, 11, 7, 4, 0, tzinfo=timezone.utc).timestamp()
    until_ts = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc).timestamp()
    e = usage_sources.Evidence(since_ts, until_ts)

    e.put("claude", "local-1", until_ts - 120, "anthropic", "claude-opus-5-5", "openclaw", (100, 0, 0, 0))
    prod_payload = {
        "bucket": "day",
        "timezone": "UTC",
        "models": [
            {
                "date": "2026-11-12",
                "model": "claude-opus-5-5",
                "calls": 20,
                "fresh_input": 1000,
                "cache_read": 0,
                "cache_write": 0,
                "output": 0,
                "total_tokens": 1000,
                "runs": 5,
            }
        ],
        "totals": {
            "calls": 20,
            "fresh_input": 1000,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "total_tokens": 1000,
            "runs": 5,
        },
    }
    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DALTON_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok")
    with patch.object(usage_sources, "fetch_dalton_prod_by_model", autospec=True, return_value=prod_payload):
        assert usage_sources.scan_dalton_prod(e, window_days=7.0, window_since=since_ts, window_until=until_ts)

    snapshot = _build_cli_snapshot(cli, usage_sources, e, until_ts, days=7)
    stats = tracker.process_usage_stats(snapshot)
    assert stats["local_stats"]["tokens"] == 100
    assert stats["dalton_stats"]["tokens"] == 1000
    assert stats["total_tokens"] == 1100
    assert len(stats["top10"]) == 1
    assert stats["top10"][0]["model"] == "claude-opus-5-5"
    assert stats["top10"][0]["total_tokens"] == 1100


def test_r3_collector_status_stale_or_error_and_coverage_issues_rendered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    tracker = _load_tracker()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(state_dir))

    base_snapshot = {
        "version": "3.1.0",
        "price_catalog": {"generated": "2026-11-14", "effective_notes": []},
        "dalton_prod": {"ok": True, "error": None, "runs": 0, "calls": 0, "models": 0},
        "coverage": {
            "status": "partial",
            "issues": {"agy_db_unreadable": 2, "conflicting_identity_usage": 3},
            "unresolved_aggregate_records": 1,
            "unresolved_aggregate_tokens_not_additive": 500,
        },
        "windows": [
            {
                "days": 7.0,
                "from": "2026-11-07T04:00:00+00:00",
                "to": "2026-11-14T04:00:00+00:00",
                "rows": [
                    {
                        "source": "openclaw",
                        "provider": "openai",
                        "model": "gpt-6-luna",
                        "records": 2,
                        "calls": 2,
                        "fresh": 1000,
                        "cache_write": 0,
                        "cache_read": 0,
                        "output": 100,
                        "cost_usd": 0.01,
                    }
                ],
                "totals": {
                    "tokens": 1100,
                    "records": 2,
                    "calls": 2,
                    "priced_cost_usd": 0.01,
                    "unpriced_tokens": 0,
                },
            }
        ],
    }

    # 0. No collector-status.json at all (collector never ran, or the state dir is wrong)
    missing_text = json.dumps(tracker.build_card(tracker.process_usage_stats(base_snapshot)), ensure_ascii=False)
    assert "找不到采集器状态文件，无法确认采集器在持续运行" in missing_text

    now_utc = datetime.now(timezone.utc)
    # 1. Stale collector-status.json (25 minutes old)
    (state_dir / "collector-status.json").write_text(
        json.dumps({"ok": True, "collected_at": (now_utc - timedelta(minutes=25)).isoformat()}),
        encoding="utf-8",
    )
    stale_card = tracker.build_card(tracker.process_usage_stats(base_snapshot))
    stale_text = json.dumps(stale_card, ensure_ascii=False)
    assert "本机采集器 25 分钟未更新，Grok 等需持续采集的来源可能缺失" in stale_text
    assert "Antigravity 库不可读 2 项" in stale_text
    assert "同标识冲突记录 3 项" in stale_text

    # 2. Recent collector-status.json with ok=False
    (state_dir / "collector-status.json").write_text(
        json.dumps({"ok": False, "error": "sqlite lock", "collected_at": (now_utc - timedelta(minutes=2)).isoformat()}),
        encoding="utf-8",
    )
    err_text = json.dumps(tracker.build_card(tracker.process_usage_stats(base_snapshot)), ensure_ascii=False)
    assert "本机采集器状态异常（sqlite lock），Grok 等需持续采集的来源可能缺失" in err_text

    # 3. Healthy & recent collector-status.json
    (state_dir / "collector-status.json").write_text(
        json.dumps({"ok": True, "collected_at": (now_utc - timedelta(minutes=2)).isoformat()}),
        encoding="utf-8",
    )
    ok_text = json.dumps(tracker.build_card(tracker.process_usage_stats(base_snapshot)), ensure_ascii=False)
    assert "本机采集器告警" not in ok_text
    assert "Grok 等需持续采集的来源可能缺失" not in ok_text


# ---------------------------------------------------------------------------
# T4: Manifest fields & host bridge acceptance
# ---------------------------------------------------------------------------


def test_t4_manifest_and_payload_source(tmp_path: Path) -> None:
    manifest = load_data_task_manifest()
    by_key = manifest.by_job_key()
    assert "weekly-token-usage" in by_key
    entry = by_key["weekly-token-usage"]

    assert entry.runner == "host"
    assert entry.host_mode == "run"
    assert entry.cwd == "var/dalton/openclaw-data"
    assert entry.cron == "0 12 * * 6"
    assert entry.timezone == "Asia/Hong_Kong"
    assert entry.enabled is False
    assert entry.notify_enabled is False
    assert entry.secret_names == ["INTERNAL_SERVICE_TOKEN"]
    assert entry.env == {
        "DALTON_BACKEND_URL": "http://127.0.0.1:8000",
    }
    assert entry.timeout_seconds == 900
    assert entry.command_argv == [
        "bash",
        "skills/model-usage-cost/scripts/run.sh",
        "--script",
        "scripts/token_weekly_usage_tracker.py",
    ]
    assert resolve_task_payload_source("weekly-token-usage") == "outbox_file"
    assert resolve_task_payload_source(entry.task_code) == "outbox_file"

    spec = resolve_host_run_command_spec(
        job_key="weekly-token-usage",
        command_argv=entry.command_argv,
        cwd=entry.cwd,
        data_root=tmp_path,
        ocw_root=tmp_path,
    )
    assert spec["command_argv"] == entry.command_argv
    assert spec["cwd"] == str(tmp_path)


def test_default_state_dir_follows_pointer_file(tmp_path, monkeypatch) -> None:
    """State dir priority: DALTON_USAGE_STATE_DIR env -> ~/.config/dalton/usage-state-dir -> ~/.openclaw/usage-accounting."""
    import importlib

    scripts = Path(__file__).resolve().parents[3] / "scripts/dalton/data_assets/skills/model-usage-cost/scripts"
    monkeypatch.syspath_prepend(str(scripts))
    cli = importlib.import_module("model_usage_cost_cli")
    usage_state = importlib.import_module("usage_state")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DALTON_USAGE_STATE_DIR", raising=False)
    monkeypatch.delenv("DALTON_USAGE_STATE_POINTER_FILE", raising=False)

    legacy = fake_home / ".openclaw" / "usage-accounting"
    assert cli.default_state_dir() == legacy  # no pointer -> legacy dir

    new_dir = tmp_path / "new"
    usage_state.write_usage_state_pointer(new_dir)
    assert cli.default_state_dir() == new_dir.resolve()  # pointer -> pointed dir

    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(tmp_path / "x"))
    assert cli.default_state_dir() == tmp_path / "x"  # env overrides pointer


def test_data_copy_card_senders_split_oversized_cards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token_weekly_usage_tracker, cron_command_tasks.etf_dry_run_outbox, and eve_tracking_mirror split oversized cards."""
    import importlib.util
    from argparse import Namespace

    monkeypatch.setenv("DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID", "oc_placeholder_tracking")

    repo_root = Path(__file__).resolve().parents[3]
    data_scripts = repo_root / "scripts" / "dalton" / "data_assets" / "scripts"
    rr_scripts = repo_root / "scripts" / "dalton" / "research_runtime" / "scripts"

    send_spec = importlib.util.spec_from_file_location(
        "_data_feishu_send_card", data_scripts / "feishu_send_card.py"
    )
    assert send_spec and send_spec.loader
    send_mod = importlib.util.module_from_spec(send_spec)
    send_spec.loader.exec_module(send_mod)

    # Build a >30 KB Schema 2.0 card
    big_md = "\n\n".join(
        f"## 板块 {i}\n\n" + ("这是用于测试 data-copy 自动拆卡的长段落内容。" * 60)
        for i in range(1, 14)
    )
    oversized_card = send_mod.markdown_to_card_elements(big_md, "每周 Token 用量与成本")
    assert send_mod.feishu_card_gateway_utf8_bytes(oversized_card) > send_mod.FEISHU_CARD_MAX_BYTES

    # 1. token_weekly_usage_tracker.write_outbox_card
    tracker_spec = importlib.util.spec_from_file_location(
        "_weekly_tracker", data_scripts / "token_weekly_usage_tracker.py"
    )
    assert tracker_spec and tracker_spec.loader
    tracker_mod = importlib.util.module_from_spec(tracker_spec)
    tracker_spec.loader.exec_module(tracker_mod)
    outbox_file = tmp_path / "weekly_outbox.json"
    tracker_mod.write_outbox_card(outbox_file, oversized_card)
    outbox_data = json.loads(outbox_file.read_text(encoding="utf-8"))
    assert len(outbox_data["feishu_cards"]) > 1
    for item in outbox_data["feishu_cards"]:
        assert send_mod.is_feishu_card_within_envelope(item["card"])

    # 2. cron_command_tasks.etf_dry_run_outbox
    cron_spec = importlib.util.spec_from_file_location(
        "_cron_tasks", data_scripts / "cron_command_tasks.py"
    )
    assert cron_spec and cron_spec.loader
    cron_mod = importlib.util.module_from_spec(cron_spec)
    cron_spec.loader.exec_module(cron_mod)
    banner = cron_mod.ETF_DRY_RUN_BANNER
    stdout = f"log line\n{banner}\n" + json.dumps(oversized_card, ensure_ascii=False)
    etf_outbox = cron_mod.etf_dry_run_outbox(stdout)
    assert len(etf_outbox["feishu_cards"]) > 1
    for item in etf_outbox["feishu_cards"]:
        assert send_mod.is_feishu_card_within_envelope(item["card"])

    # 3. eve_tracking_mirror._load_cards
    mirror_spec = importlib.util.spec_from_file_location(
        "_eve_mirror", rr_scripts / "eve_tracking_mirror.py"
    )
    assert mirror_spec and mirror_spec.loader
    mirror_mod = importlib.util.module_from_spec(mirror_spec)
    mirror_spec.loader.exec_module(mirror_mod)
    md_file = tmp_path / "eve.md"
    md_file.write_text(big_md, encoding="utf-8")
    old_sys_path = list(sys.path)
    sys.path.insert(0, str(rr_scripts))
    try:
        sys.modules.pop("feishu_send_card", None)
        loaded_cards = mirror_mod._load_cards(
            Namespace(card_json_file=None, file=str(md_file), title="Eve 跟踪", subtitle="")
        )
    finally:
        sys.path[:] = old_sys_path
        sys.modules.pop("feishu_send_card", None)
    assert len(loaded_cards) > 1
    for card_item in loaded_cards:
        assert send_mod.is_feishu_card_within_envelope(card_item)


# ---------------------------------------------------------------------------
# §5: Weekly token report Antigravity series merge, closure, idempotency & card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_internal_usage_endpoint_exposes_antigravity_series_and_keeps_raw_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rows = [
        {
            "date": "2026-11-18",
            "model": "gemini-4-neon-high-oauth",
            "calls": 100,
            "fresh_input": 500_000,
            "cache_read": 200_000,
            "cache_write": 0,
            "output": 40_000,
            "total_tokens": 740_000,
            "runs": 20,
            "total_runs": 35,
        },
        {
            "date": "2026-11-18",
            "model": "gemini-4-neon-medium-oauth",
            "calls": 50,
            "fresh_input": 250_000,
            "cache_read": 100_000,
            "cache_write": 0,
            "output": 20_000,
            "total_tokens": 370_000,
            "runs": 15,
            "total_runs": 35,
        },
    ]
    conn, _ = _make_autospec_conn(fake_rows)
    from_dt = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc)
    to_dt = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)

    result = await fetch_usage_by_model(conn, from_dt=from_dt, to_dt=to_dt, bucket="day")
    assert [m["model"] for m in result["models"]] == [
        "gemini-4-neon-high-oauth",
        "gemini-4-neon-medium-oauth",
    ]
    series = result.get("antigravity_series")
    assert isinstance(series, list) and len(series) == 7
    series_ids = {s["series_id"] for s in series}
    assert "antigravity-oauth:gemini-4-neon" in series_ids
    assert "antigravity-oauth:claude-opus-5-5" in series_ids
    assert "antigravity-oauth:gpt-oss-120b" in series_ids

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", VALID_SERVICE_TOKEN)

    @contextlib.asynccontextmanager
    async def fake_pool_conn():
        yield conn

    with patch("src.dalton.app.internal_usage_router.pool.get_db_connection", fake_pool_conn):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/v1/dalton/internal/usage/by-model?from=2026-11-14T04:00:00Z&to=2026-11-21T04:00:00Z&bucket=day",
            headers={"X-Service-Token": VALID_SERVICE_TOKEN},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["antigravity_series"]) == 7
    assert body["models"][0]["model"] == "gemini-4-neon-high-oauth"


def _make_realistic_series_snapshot(include_series: bool = True) -> dict:
    from src.dalton.llm.antigravity_series import build_antigravity_series_catalog
    from src.llms.llm import LLM

    rows = [
        # --- Antigravity Neon: Dalton prod (high + xhigh) + local (high + medium + unknown) ---
        {
            "source": "Dalton 生产（订阅）",
            "provider": "google",
            "model": "gemini-4-neon-high-oauth",
            "dalton_prod": True,
            "records": 7,
            "calls": 200,
            "runs": 60,
            "fresh": 600_000,
            "cache_write": 0,
            "cache_read": 1_200_000,
            "output": 100_000,
            "cost_usd": 3.48,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "Dalton 生产（订阅）",
            "provider": "google",
            "model": "gemini-4-neon-xhigh-oauth",
            "dalton_prod": True,
            "records": 3,
            "calls": 40,
            "runs": 12,
            "fresh": 200_000,
            "cache_write": 0,
            "cache_read": 400_000,
            "output": 50_000,
            "cost_usd": 1.41,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "openclaw",
            "provider": "antigravity-cli-gateway",
            "model": "gemini-4-neon",
            "effort": "high",
            "dalton_prod": False,
            "records": 25,
            "calls": 25,
            "runs": 0,
            "fresh": 100_000,
            "cache_write": 0,
            "cache_read": 200_000,
            "output": 20_000,
            "cost_usd": 0.63,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "openclaw",
            "provider": "antigravity-cli-gateway",
            "model": "gemini-4-neon",
            "effort": "medium",
            "dalton_prod": False,
            "records": 15,
            "calls": 15,
            "runs": 0,
            "fresh": 80_000,
            "cache_write": 0,
            "cache_read": 100_000,
            "output": 10_000,
            "cost_usd": 0.405,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "agy-cli(含网关)",
            "provider": "antigravity-cli-gateway",
            "model": "gemini-4-neon",
            "dalton_prod": False,
            "records": 10,
            "calls": 10,
            "runs": 0,
            "fresh": 50_000,
            "cache_write": 0,
            "cache_read": 50_000,
            "output": 5_000,
            "cost_usd": 0.2325,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        # --- Claude Opus 5.5 via Antigravity (3 tiers + local low) ---
        {
            "source": "Dalton 生产（订阅）",
            "provider": "claude-cli-gateway",
            "model": "claude-opus-5-5-low-oauth",
            "dalton_prod": True,
            "records": 2,
            "calls": 18,
            "runs": 6,
            "fresh": 100_000,
            "cache_write": 10_000,
            "cache_read": 300_000,
            "output": 15_000,
            "cost_usd": 0.84,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "Dalton 生产（订阅）",
            "provider": "claude-cli-gateway",
            "model": "claude-opus-5-5-medium-oauth",
            "dalton_prod": True,
            "records": 4,
            "calls": 35,
            "runs": 10,
            "fresh": 150_000,
            "cache_write": 20_000,
            "cache_read": 500_000,
            "output": 25_000,
            "cost_usd": 1.36,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "Dalton 生产（订阅）",
            "provider": "claude-cli-gateway",
            "model": "claude-opus-5-5-high-oauth",
            "dalton_prod": True,
            "records": 5,
            "calls": 45,
            "runs": 14,
            "fresh": 200_000,
            "cache_write": 30_000,
            "cache_read": 700_000,
            "output": 40_000,
            "cost_usd": 1.98,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        {
            "source": "openclaw",
            "provider": "antigravity-cli-gateway",
            "model": "claude-opus-5-5",
            "effort": "low",
            "dalton_prod": False,
            "records": 5,
            "calls": 5,
            "runs": 0,
            "fresh": 50_000,
            "cache_write": 0,
            "cache_read": 100_000,
            "output": 5_000,
            "cost_usd": 0.32,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        # --- Anthropic official Claude Opus 5.5 (MUST NOT merge into Antigravity) ---
        {
            "source": "Dalton 生产（订阅）",
            "provider": "claude-cli-gateway",
            "model": "claude-opus-5-5-oauth",
            "dalton_prod": True,
            "records": 7,
            "calls": 500,
            "runs": 120,
            "fresh": 400_000,
            "cache_write": 50_000,
            "cache_read": 2_000_000,
            "output": 80_000,
            "cost_usd": 4.0,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        # --- Local direct Anthropic Claude Code (MUST NOT merge into Antigravity) ---
        {
            "source": "claude-code(手动)",
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            "dalton_prod": False,
            "records": 20,
            "calls": 20,
            "runs": 0,
            "fresh": 60_000,
            "cache_write": 0,
            "cache_read": 200_000,
            "output": 10_000,
            "cost_usd": 0.48,
            "unpriced_tokens": 0,
            "no_public_price": False,
        },
        # --- Unpriced + reference_only model (non-Antigravity) ---
        {
            "source": "muse-cli(手动)",
            "provider": "muse-cli-gateway",
            "model": "muse-wonder-full-1.0",
            "dalton_prod": False,
            "records": 6,
            "calls": 6,
            "runs": 0,
            "fresh": 100_000,
            "cache_write": 0,
            "cache_read": 50_000,
            "output": 10_000,
            "cost_usd": None,
            "unpriced_tokens": 160_000,
            "no_public_price": True,
            "reference_cost_usd": 0.61,
            "reference_model": "muse-wonder-1.0",
            "price_note": "无公开价",
        },
        # --- Unpriced + reference_only Antigravity series (GPT-OSS 120B, no public price in prices.json) ---
        {
            "source": "Dalton 生产（订阅）",
            "provider": "openai",
            "model": "gpt-oss-120b-medium-oauth",
            "dalton_prod": True,
            "records": 2,
            "calls": 12,
            "runs": 4,
            "fresh": 40_000,
            "cache_write": 0,
            "cache_read": 10_000,
            "output": 5_000,
            "cost_usd": None,
            "unpriced_tokens": 55_000,
            "no_public_price": True,
            "reference_cost_usd": 0.08,
            "reference_model": "gpt-oss-120b-groq",
            "price_note": "参考估算",
        },
    ]

    total_fresh = sum(r["fresh"] for r in rows)
    total_cw = sum(r["cache_write"] for r in rows)
    total_cr = sum(r["cache_read"] for r in rows)
    total_out = sum(r["output"] for r in rows)
    total_tokens = total_fresh + total_cw + total_cr + total_out
    total_records = sum(r["records"] for r in rows)
    total_calls = sum(r["calls"] for r in rows)
    total_cost = round(sum(r["cost_usd"] for r in rows if r["cost_usd"] is not None), 6)
    total_unpriced = sum(r["unpriced_tokens"] for r in rows)

    dalton_rows = [r for r in rows if r["dalton_prod"]]
    snap = {
        "ok": True,
        "version": "3.1.0",
        "price_catalog": {"generated": "2026-11-14", "effective_notes": []},
        "dalton_prod": {
            "ok": True,
            "error": None,
            "runs": sum(r["runs"] for r in dalton_rows),
            "calls": sum(r["calls"] for r in dalton_rows),
            "models": len({r["model"] for r in dalton_rows}),
        },
        "collector_status": {"ok": True, "fresh": True},
        "windows": [
            {
                "days": 7.0,
                "from": "2026-11-14T04:00:00+00:00",
                "to": "2026-11-21T04:00:00+00:00",
                "row_count": len(rows),
                "rows": rows,
                "totals": {
                    "fresh": total_fresh,
                    "cache_write": total_cw,
                    "cache_read": total_cr,
                    "output": total_out,
                    "tokens": total_tokens,
                    "records": total_records,
                    "calls": total_calls,
                    "priced_cost_usd": total_cost,
                    "unpriced_tokens": total_unpriced,
                },
            }
        ],
    }
    if include_series:
        snap["antigravity_series"] = build_antigravity_series_catalog(LLM.get_model_config())
    return snap


def test_s5_series_merge_preserves_totals_source_subtotals_unpriced_and_effort_closure() -> None:
    tracker = _load_tracker()
    snap = _make_realistic_series_snapshot(include_series=True)
    raw_rows = snap["windows"][0]["rows"]

    # Expected pre-merge totals and source subtotals
    raw_local = [r for r in raw_rows if not r["dalton_prod"]]
    raw_dalton = [r for r in raw_rows if r["dalton_prod"]]
    exp_local_tokens = sum(r["fresh"] + r["cache_write"] + r["cache_read"] + r["output"] for r in raw_local)
    exp_dalton_tokens = sum(r["fresh"] + r["cache_write"] + r["cache_read"] + r["output"] for r in raw_dalton)
    exp_local_cost = sum(r["cost_usd"] for r in raw_local if r["cost_usd"] is not None)
    exp_dalton_cost = sum(r["cost_usd"] for r in raw_dalton if r["cost_usd"] is not None)
    exp_ref_cost = sum(r.get("reference_cost_usd") or 0.0 for r in raw_rows)

    stats = tracker.process_usage_stats(snap)
    merged_rows = stats["top10"] + stats["tail"]

    # 1. Exact global totals equality
    assert stats["total_tokens"] == exp_local_tokens + exp_dalton_tokens
    assert stats["total_calls"] == sum(r["calls"] for r in raw_rows)
    assert stats["total_records"] == sum(r["records"] for r in raw_rows)
    assert stats["total_cost"] == pytest.approx(exp_local_cost + exp_dalton_cost, rel=0, abs=1e-9)
    assert stats["unpriced_tokens"] == sum(r["unpriced_tokens"] for r in raw_rows)
    assert sum((m.get("reference_cost_usd") or 0.0) for m in stats["unpriced_models"]) == pytest.approx(exp_ref_cost, rel=0, abs=1e-9)

    # 2. Source subtotals unchanged (no cross-source contamination)
    assert stats["local_stats"]["tokens"] == exp_local_tokens
    assert stats["local_stats"]["calls"] == sum(r["calls"] for r in raw_local)
    assert stats["local_stats"]["records"] == sum(r["records"] for r in raw_local)
    assert stats["local_stats"]["cost"] == pytest.approx(exp_local_cost, rel=0, abs=1e-9)
    assert stats["local_stats"]["unpriced_tokens"] == sum(r["unpriced_tokens"] for r in raw_local)

    assert stats["dalton_stats"]["tokens"] == exp_dalton_tokens
    assert stats["dalton_stats"]["calls"] == sum(r["calls"] for r in raw_dalton)
    assert stats["dalton_stats"]["records"] == sum(r["records"] for r in raw_dalton)
    assert stats["dalton_stats"]["cost"] == pytest.approx(exp_dalton_cost, rel=0, abs=1e-9)
    assert stats["dalton_stats"]["unpriced_tokens"] == sum(r["unpriced_tokens"] for r in raw_dalton)

    # 3. Expected merged row names:
    # Neon (merged), Claude Opus 5.5 (Antigravity) (merged), claude-opus-5-5-oauth (independent!),
    # claude-opus-5-5 (local Anthropic independent!), muse-wonder-full-1.0, GPT-OSS 120B (Antigravity)
    by_model = {r["model"]: r for r in merged_rows}
    assert set(by_model.keys()) == {
        "Gemini 4 Neon",
        "Claude Opus 5.5 (Antigravity)",
        "claude-opus-5-5-oauth",
        "claude-opus-5-5",
        "muse-wonder-full-1.0",
        "GPT-OSS 120B (Antigravity)",
    }

    # Anthropic official OAuth & local Claude Code stay independent (no series_id / no effort_breakdown)
    assert "series_id" not in by_model["claude-opus-5-5-oauth"]
    assert "effort_breakdown" not in by_model["claude-opus-5-5-oauth"]
    assert by_model["claude-opus-5-5-oauth"]["calls"] == 500
    assert "series_id" not in by_model["claude-opus-5-5"]

    # Neon effort breakdown: medium, high, xhigh, unknown (in canonical order)
    neon = by_model["Gemini 4 Neon"]
    assert neon["series_id"] == "antigravity-oauth:gemini-4-neon"
    assert list(neon["effort_breakdown"].keys()) == ["medium", "high", "xhigh", "unknown"]
    assert neon["effort_breakdown"]["unknown"]["total_tokens"] == 105_000
    assert neon["effort_breakdown"]["unknown"]["calls"] == 10

    # Antigravity Opus effort breakdown: low (Dalton + local combined!), medium, high
    agy_opus = by_model["Claude Opus 5.5 (Antigravity)"]
    assert agy_opus["series_id"] == "antigravity-oauth:claude-opus-5-5"
    assert list(agy_opus["effort_breakdown"].keys()) == ["low", "medium", "high"]
    assert agy_opus["effort_breakdown"]["low"]["calls"] == 18 + 5

    # GPT-OSS 120B keeps unpriced & reference_cost_usd in both merged row and effort_breakdown
    gpt_oss = by_model["GPT-OSS 120B (Antigravity)"]
    assert gpt_oss["cost_usd"] is None
    assert gpt_oss["unpriced_tokens"] == 55_000
    assert gpt_oss["reference_cost_usd"] == pytest.approx(0.08)
    assert gpt_oss["effort_breakdown"]["medium"]["unpriced_tokens"] == 55_000
    assert gpt_oss["effort_breakdown"]["medium"]["reference_cost_usd"] == pytest.approx(0.08)

    # 4. Closure invariant for EVERY merged series row
    for r in merged_rows:
        bd = r.get("effort_breakdown")
        if not bd:
            continue
        assert sum(b["total_tokens"] for b in bd.values()) == r["total_tokens"]
        assert sum(b["calls"] for b in bd.values()) == r["calls"]
        assert sum(b["records"] for b in bd.values()) == r["records"]
        assert sum(b["unpriced_tokens"] for b in bd.values()) == r["unpriced_tokens"]
        if r["cost_usd"] is None:
            assert all(b["cost_usd"] is None for b in bd.values())
        else:
            assert sum((b["cost_usd"] or 0.0) for b in bd.values()) == pytest.approx(r["cost_usd"], rel=0, abs=1e-9)
        if r["reference_cost_usd"] is None:
            assert all(b["reference_cost_usd"] is None for b in bd.values())
        else:
            assert sum((b["reference_cost_usd"] or 0.0) for b in bd.values()) == pytest.approx(
                r["reference_cost_usd"], rel=0, abs=1e-9
            )


def test_s5_merge_rows_by_model_is_idempotent() -> None:
    tracker = _load_tracker()
    snap = _make_realistic_series_snapshot(include_series=True)
    raw_rows = [dict(r) for r in snap["windows"][0]["rows"]]
    for r in raw_rows:
        r["total_tokens"] = r["fresh"] + r["cache_write"] + r["cache_read"] + r["output"]
        r["prompt_tokens"] = r["fresh"] + r["cache_write"] + r["cache_read"]
        r["norm_provider"] = tracker.normalize_provider(r["provider"], r["model"])

    series_index = tracker.build_series_index(snap["antigravity_series"])
    merged_once = tracker.merge_rows_by_model(raw_rows, series_index)
    merged_twice = tracker.merge_rows_by_model(merged_once, series_index)
    merged_thrice = tracker.merge_rows_by_model(merged_twice, series_index)

    assert merged_twice == merged_once
    assert merged_thrice == merged_once


def test_s5_input_json_offline_uses_embedded_series_and_falls_back_for_legacy_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tracker = _load_tracker()

    # 1. Embedded custom series in --input-json takes precedence over external file
    custom_snap = _make_realistic_series_snapshot(include_series=False)
    custom_snap["antigravity_series"] = [
        {
            "series_id": "antigravity-oauth:gemini-4-nova",
            "series_prefix": "gemini-4-nova",
            "provider": "antigravity-oauth",
            "display_name": "Gemini 4 Nova",
            "qualified_display_name": "Gemini 4 Nova (Antigravity)",
            "default_effort": "high",
            "default_model": "gemini-4-nova-high-oauth",
            "efforts": ["low", "high"],
            "variants": {
                "low": "gemini-4-nova-low-oauth",
                "high": "gemini-4-nova-high-oauth",
            },
        }
    ]
    custom_snap["windows"][0]["rows"] = [
        {
            "source": "Dalton 生产（订阅）",
            "provider": "google",
            "model": "gemini-4-nova-high-oauth",
            "dalton_prod": True,
            "records": 1,
            "calls": 10,
            "runs": 3,
            "fresh": 100_000,
            "cache_write": 0,
            "cache_read": 0,
            "output": 10_000,
            "cost_usd": 0.5,
            "unpriced_tokens": 0,
        },
        {
            "source": "Dalton 生产（订阅）",
            "provider": "google",
            "model": "gemini-4-nova-low-oauth",
            "dalton_prod": True,
            "records": 1,
            "calls": 5,
            "runs": 2,
            "fresh": 50_000,
            "cache_write": 0,
            "cache_read": 0,
            "output": 5_000,
            "cost_usd": 0.25,
            "unpriced_tokens": 0,
        },
    ]
    custom_snap["windows"][0]["row_count"] = 2
    custom_snap["windows"][0]["totals"] = {
        "tokens": 165_000,
        "records": 2,
        "calls": 15,
        "priced_cost_usd": 0.75,
        "unpriced_tokens": 0,
    }
    snap_with_series = tmp_path / "with_series.json"
    snap_with_series.write_text(json.dumps(custom_snap, ensure_ascii=False), encoding="utf-8")

    with patch.object(sys, "argv", ["token_weekly_usage_tracker.py", "--input-json", str(snap_with_series), "--json"]):
        assert tracker.main() == 0
    out_embedded = json.loads(capsys.readouterr().out)
    assert len(out_embedded["top10"]) == 1
    assert out_embedded["top10"][0]["model"] == "Gemini 4 Nova"
    assert list(out_embedded["top10"][0]["effort_breakdown"].keys()) == ["low", "high"]

    # 2. Legacy snapshot WITHOUT antigravity_series falls back to synced references/antigravity_series.json
    import os
    import subprocess

    seeded_target = tmp_path / "seeded_openclaw_data"
    seed_env = {
        **os.environ,
        "DALTON_DATA_TASK_BBG_HOST": "bbg@test.example",
        "DALTON_DATA_TASK_BBG_CSV_REMOTE": "C:/test/bbg.csv",
        "DALTON_DATA_TASK_BBG_XLSX_REMOTE": "C:/test/bbg.xlsx",
        "DALTON_DATA_TASK_BBG_REFRESH_CMD": "cmd /c test.bat",
        "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID": "oc_placeholder_tracking",
        "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID": "oc_placeholder_semi",
        "DALTON_DATA_TASK_FEISHU_ASIA_TARGET": "chat:oc_placeholder_asia",
        "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET": "oc_placeholder_megacap",
        "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID": "oc_placeholder_sentiment",
        "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET": "user:ou_placeholder_ashare",
        "DALTON_DATA_TASK_DISCORD_DM_TARGET": "user:100000000000000001",
        "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL": "channel:100000000000000002",
        "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL": "channel:100000000000000003",
        "DALTON_SEC_USER_AGENT": "TestBot test@example.com",
        "DALTON_DATA_TASK_GS_PB_PYTHON": "/opt/test/gs-pb/bin/python",
    }
    seed_res = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts/dalton/seed_openclaw_data.sh"), "--apply", "--target", str(seeded_target)],
        env=seed_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert seed_res.returncode == 0, seed_res.stdout + seed_res.stderr
    assert (seeded_target / "skills/model-usage-cost/references/antigravity_series.json").is_file()
    assert (seeded_target / "skills/model-usage-cost/scripts/antigravity_series.py").is_file()

    legacy_snap = _make_realistic_series_snapshot(include_series=False)
    assert "antigravity_series" not in legacy_snap
    legacy_path = tmp_path / "legacy_snap.json"
    legacy_path.write_text(json.dumps(legacy_snap, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(seeded_target))
    monkeypatch.delenv("DALTON_ANTIGRAVITY_SERIES_FILE", raising=False)
    with patch.object(sys, "argv", ["token_weekly_usage_tracker.py", "--input-json", str(legacy_path), "--json"]):
        assert tracker.main() == 0
    out_legacy = json.loads(capsys.readouterr().out)
    legacy_models = {r["model"] for r in out_legacy["top10"] + out_legacy["tail"]}
    assert "Gemini 4 Neon" in legacy_models
    assert "Claude Opus 5.5 (Antigravity)" in legacy_models
    assert "claude-opus-5-5-oauth" in legacy_models


def test_s5_card_renders_effort_breakdown_in_top10_and_tail_including_unknown_and_unpriced() -> None:
    tracker = _load_tracker()
    snap = _make_realistic_series_snapshot(include_series=True)
    # Add 7 extra dummy models with higher token counts so GPT-OSS 120B (Antigravity) falls into tail (> rank 10)
    extra_rows = []
    for idx in range(7):
        extra_rows.append(
            {
                "source": "openclaw",
                "provider": "openai",
                "model": f"gpt-6-extra-{idx}",
                "dalton_prod": False,
                "records": 10,
                "calls": 10,
                "runs": 0,
                "fresh": 200_000 + idx * 10_000,
                "cache_write": 0,
                "cache_read": 0,
                "output": 10_000,
                "cost_usd": 1.0,
                "unpriced_tokens": 0,
            }
        )
    snap["windows"][0]["rows"].extend(extra_rows)
    snap["windows"][0]["row_count"] = len(snap["windows"][0]["rows"])
    all_rows = snap["windows"][0]["rows"]
    snap["windows"][0]["totals"] = {
        "tokens": sum(r["fresh"] + r["cache_write"] + r["cache_read"] + r["output"] for r in all_rows),
        "records": sum(r["records"] for r in all_rows),
        "calls": sum(r["calls"] for r in all_rows),
        "priced_cost_usd": round(sum(r["cost_usd"] for r in all_rows if r["cost_usd"] is not None), 6),
        "unpriced_tokens": sum(r["unpriced_tokens"] for r in all_rows),
    }

    stats = tracker.process_usage_stats(snap)
    assert any(r["model"] == "GPT-OSS 120B (Antigravity)" for r in stats["tail"])

    card = tracker.build_card(stats)
    card_text = json.dumps(card, ensure_ascii=False)
    assert "Gemini 4 Neon" in card_text
    assert "Claude Opus 5.5 (Antigravity)" in card_text
    assert "claude-opus-5-5-oauth" in card_text
    assert "档位明细:" in card_text
    assert "high " in card_text
    assert "medium " in card_text
    assert "xhigh " in card_text
    assert "档位未知 " in card_text
    # Tail unpriced Antigravity series displays breakdown with unpriced + reference estimate
    assert "GPT-OSS 120B (Antigravity)" in card_text
    assert "无公开价，参考 $0.08" in card_text


# ---------------------------------------------------------------------------
# Round 2 review regression tests (Codex + Carbon findings 1-7)
# ---------------------------------------------------------------------------


def test_r2_gpt_oss_120b_local_agy_and_dalton_prod_merge_into_single_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #1 (Codex #1 / Carbon #2): GPT-OSS 120B local agy + Dalton prod unify vendor & merge into 1 row."""
    import sqlite3

    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_sources
    from test_usage_accounting import pb

    tracker = _load_tracker()
    since_ts = datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc).timestamp()
    until_ts = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc).timestamp()

    # Real SQLite agy-db file for local GPT-OSS 120B
    agy_db = tmp_path / "conv_gpt_oss.db"
    c = sqlite3.connect(agy_db)
    c.execute("CREATE TABLE steps(idx INTEGER, metadata BLOB)")
    c.execute("CREATE TABLE gen_metadata(data BLOB)")
    meta = pb([(19, b"gpt-oss-120b-medium"), (20, pb([(1, b"model_enum"), (2, b"MODEL_PLACEHOLDER_M499")]))])
    c.execute("INSERT INTO gen_metadata VALUES (?)", (pb([(1, meta)]),))
    usage = pb([(1, 1499), (2, 40_000), (3, 5_000), (5, 10_000), (11, b"resp-gptoss-1")])
    c.execute("INSERT INTO steps VALUES (1, ?)", (pb([(8, pb([(1, int(until_ts - 600))])), (9, usage)]),))
    c.commit()
    c.close()

    roots = {k: [] for k in usage_sources.ROOTS}
    roots["agy-db"] = [str(agy_db)]
    e = usage_sources.Evidence(since_ts, until_ts)
    with patch.object(usage_sources, "ROOTS", roots):
        usage_sources.scan_agy(e)

    prod_payload = {
        "bucket": "day",
        "timezone": "UTC",
        "antigravity_series": usage_sources.load_antigravity_series_catalog(),
        "models": [
            {
                "date": "2026-11-19",
                "model": "gpt-oss-120b-medium-oauth",
                "calls": 12,
                "fresh_input": 80_000,
                "cache_read": 20_000,
                "cache_write": 0,
                "output": 10_000,
                "total_tokens": 110_000,
                "runs": 4,
            }
        ],
        "totals": {
            "calls": 12,
            "fresh_input": 80_000,
            "cache_read": 20_000,
            "cache_write": 0,
            "output": 10_000,
            "total_tokens": 110_000,
            "runs": 4,
        },
    }
    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DALTON_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok")
    with patch.object(usage_sources, "fetch_dalton_prod_by_model", autospec=True, return_value=prod_payload):
        assert usage_sources.scan_dalton_prod(e, window_days=7.0, window_since=since_ts, window_until=until_ts)

    snapshot = _build_cli_snapshot(cli, usage_sources, e, until_ts, days=7)
    snapshot["antigravity_series"] = e.antigravity_series
    stats = tracker.process_usage_stats(snapshot)
    all_rows = stats["top10"] + stats["tail"]
    gpt_rows = [r for r in all_rows if r.get("series_id") == "antigravity-oauth:gpt-oss-120b"]
    assert len(gpt_rows) == 1
    row = gpt_rows[0]
    assert row["model"] == "GPT-OSS 120B (Antigravity)"
    assert row["norm_provider"] == "OpenAI"
    assert row["total_tokens"] == 55_000 + 110_000
    assert row["calls"] == 1 + 12
    assert set(row["providers"]) == {"antigravity-cli-gateway", "openai"}
    assert list(row["effort_breakdown"].keys()) == ["medium"]
    assert row["effort_breakdown"]["medium"]["total_tokens"] == 165_000

    # Re-merging a non-Gemini series row without pre-filled norm_provider still resolves Anthropic (not Google)
    rem = tracker.merge_rows_by_model(
        [
            {
                "series_id": "antigravity-oauth:claude-opus-5-5",
                "model": "Claude Opus 5.5 (Antigravity)",
                "provider": "antigravity-cli-gateway",
                "source": "openclaw",
                "records": 1,
                "calls": 1,
                "fresh": 1000,
                "cache_write": 0,
                "cache_read": 0,
                "output": 100,
                "cost_usd": 0.01,
            }
        ],
        snapshot["antigravity_series"],
    )
    assert len(rem) == 1
    assert rem[0]["norm_provider"] == "Anthropic"


def test_r3_exact_field_by_field_parity_against_legacy_baseline_fixture(
    tmp_path: Path,
) -> None:
    """Round 3/4 Decision #2: frozen legacy baseline vs new --json totals, subtotals & card overview are strictly == (no git dependency)."""
    import os
    import subprocess

    fixture_path = REPO_ROOT / "tests" / "fixtures" / "dalton" / "weekly_token_usage" / "legacy_parity_cases.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert set(cases.keys()) == {"case1_5k_plus_3cr", "case2_single_5k", "case3_full_multisource"}

    env = dict(os.environ, OPENCLAW_WORKSPACE=str(REPO_ROOT / "scripts/dalton/data_assets"))
    strict_fields = (
        "total_tokens",
        "total_fresh",
        "total_cw",
        "total_cr",
        "total_out",
        "total_prompt",
        "total_records",
        "total_calls",
        "total_cost",
        "unpriced_tokens",
        "cache_hit_rate",
        "local_stats",
        "dalton_stats",
        "dalton_prod",
        "estimated_split_records",
        "time_estimated_records",
        "tier_context_unknown_records",
        "provider_reported_cost_records",
    )

    for name, bundle in cases.items():
        snap_file = tmp_path / f"{name}.json"
        snap_file.write_text(json.dumps(bundle["snapshot"], ensure_ascii=False), encoding="utf-8")
        legacy_j = bundle["legacy_json"]
        legacy_overview = bundle["legacy_card_overview"]

        new_j = json.loads(
            subprocess.check_output(
                [sys.executable, str(TRACKER_PATH), "--input-json", str(snap_file), "--json"],
                text=True,
                env=env,
            )
        )
        new_c = json.loads(
            subprocess.check_output(
                [sys.executable, str(TRACKER_PATH), "--input-json", str(snap_file), "--dry-run"],
                text=True,
                env=env,
            )
        )

        for field in strict_fields:
            assert new_j[field] == legacy_j[field], (
                f"[{name}] {field} mismatch: new={new_j[field]!r} vs legacy={legacy_j[field]!r}"
            )
        assert sum((m.get("reference_cost_usd") or 0.0) for m in new_j["unpriced_models"]) == sum(
            (m.get("reference_cost_usd") or 0.0) for m in legacy_j["unpriced_models"]
        )
        assert new_c["body"]["elements"][0]["content"] == legacy_overview, f"[{name}] overview card mismatch"

        if name == "case1_5k_plus_3cr":
            assert new_j["local_stats"]["cost"] == 0.015
            assert new_j["dalton_stats"]["cost"] == 0.015
            assert new_j["total_cost"] == 0.03
        elif name == "case2_single_5k":
            assert "$0.01" in json.dumps(new_c, ensure_ascii=False)
        elif name == "case3_full_multisource":
            assert len(new_j["top10"] + new_j["tail"]) < len(legacy_j["top10"] + legacy_j["tail"])
            by_m = {r["model"]: r for r in new_j["top10"] + new_j["tail"]}
            assert "Gemini 4 Neon" in by_m
            assert "Claude Opus 5.5 (Antigravity)" in by_m
            assert "claude-opus-5-5-oauth" in by_m
            assert "GPT-OSS 120B (Antigravity)" in by_m


def test_r4_card_series_and_effort_breakdown_usd_are_strictly_self_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Item #1: displayed card USD for effort buckets MUST sum strictly to the displayed series USD."""
    from decimal import Decimal
    import re
    import sqlite3

    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_sources
    from test_usage_accounting import pb

    tracker = _load_tracker()
    until_ts = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc).timestamp()
    since_ts = until_ts - 7 * 86400

    def parse_cents(usd_str: str) -> int:
        return int((Decimal(usd_str.lstrip("$").replace(",", "")) * 100).to_integral_exact())

    def assert_card_series_breakdown_consistent(card: dict, expected_series_usd: str | None = None) -> None:
        checked = 0
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                content = el.get("content", "")
                if "· 档位明细:" in content:
                    checked += 1
                    series_m = re.search(r"· 参考成本: (\$[0-9,]+\.\d{2})", content)
                    assert series_m is not None, f"Missing series USD in: {content}"
                    series_usd = series_m.group(1)
                    if expected_series_usd is not None:
                        assert series_usd == expected_series_usd, f"Expected {expected_series_usd}, got {series_usd}"
                    bd_line = content.split("· 档位明细:", 1)[1]
                    bucket_usds = re.findall(r"(\$[0-9,]+\.\d{2})", bd_line)
                    assert bucket_usds, f"No bucket USDs in: {bd_line}"
                    assert sum(parse_cents(u) for u in bucket_usds) == parse_cents(series_usd), (
                        f"Card mismatch: series={series_usd} vs buckets={bucket_usds} in {content}"
                    )
        assert checked > 0

    # Part A: Exact Codex R3 repro via real SQLite agy-db -> scan_agy -> reconcile -> window_result -> tracker -> card
    # Local Neon high: 5,000 fresh + 2 cache_read ($0.0150003); medium: 1 cache_read ($0.00000015); low: 0 fresh + 1 cr ($0.00000015 -> wait: 2 other tiers)
    def make_db(name: str, model_b: bytes, m_code: int, fresh: int, cr: int, ts_val: float, resp_b: bytes) -> str:
        p = tmp_path / name
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE steps(idx INTEGER, metadata BLOB)")
        c.execute("CREATE TABLE gen_metadata(data BLOB)")
        meta = pb([(19, model_b), (20, pb([(1, b"model_enum"), (2, f"MODEL_PLACEHOLDER_M{m_code}".encode())]))])
        c.execute("INSERT INTO gen_metadata VALUES (?)", (pb([(1, meta)]),))
        usage = pb([(1, 1000 + m_code), (2, fresh), (3, 0), (5, cr), (11, resp_b)])
        c.execute("INSERT INTO steps VALUES (1, ?)", (pb([(8, pb([(1, int(ts_val))])), (9, usage)]),))
        c.commit()
        c.close()
        return str(p)

    # Case A1: 5,000 fresh + 2 cr (high) + 1 cr (medium) -> total $0.01500045 -> row $0.015 -> card series $0.01, high $0.01, medium $0.00
    e_a1 = usage_sources.Evidence(since_ts, until_ts)
    roots_a1 = {k: [] for k in usage_sources.ROOTS}
    roots_a1["agy-db"] = [
        make_db("a1_hi.db", b"gemini-4-neon-high", 420, 5_000, 2, until_ts - 300, b"a1-hi"),
        make_db("a1_med.db", b"gemini-4-neon-medium", 421, 0, 1, until_ts - 200, b"a1-med"),
    ]
    with patch.object(usage_sources, "ROOTS", roots_a1):
        usage_sources.scan_agy(e_a1)
    snap_a1 = _build_cli_snapshot(cli, usage_sources, e_a1, until_ts, days=7)
    snap_a1["antigravity_series"] = usage_sources.load_antigravity_series_catalog()
    snap_a1["dalton_prod"] = {"ok": True, "error": None, "runs": 0, "calls": 0, "models": 0}
    snap_a1["collector_status"] = {"ok": True, "fresh": True}
    stats_a1 = tracker.process_usage_stats(snap_a1)
    card_a1 = tracker.build_card(stats_a1)
    assert_card_series_breakdown_consistent(card_a1, expected_series_usd="$0.01")
    card_a1_text = json.dumps(card_a1, ensure_ascii=False)
    assert "high 5.0K (1 次, $0.01)" in card_a1_text
    assert "medium 1 (1 次, $0.00)" in card_a1_text
    assert "$0.02" not in card_a1_text

    # Case A2: high 5,000 fresh + 2 other tiers (medium 1 cr + low 1 cr -> total $0.0150003 -> row $0.015 -> card $0.01)
    e_a2 = usage_sources.Evidence(since_ts, until_ts)
    roots_a2 = {k: [] for k in usage_sources.ROOTS}
    roots_a2["agy-db"] = [
        make_db("a2_hi.db", b"gemini-4-neon-high", 420, 5_000, 0, until_ts - 300, b"a2-hi"),
        make_db("a2_med.db", b"gemini-4-neon-medium", 421, 0, 1, until_ts - 200, b"a2-med"),
        make_db("a2_lo.db", b"gemini-4-neon-low", 422, 0, 1, until_ts - 100, b"a2-lo"),
    ]
    with patch.object(usage_sources, "ROOTS", roots_a2):
        usage_sources.scan_agy(e_a2)
    snap_a2 = _build_cli_snapshot(cli, usage_sources, e_a2, until_ts, days=7)
    snap_a2["antigravity_series"] = usage_sources.load_antigravity_series_catalog()
    snap_a2["dalton_prod"] = {"ok": True, "error": None, "runs": 0, "calls": 0, "models": 0}
    snap_a2["collector_status"] = {"ok": True, "fresh": True}
    card_a2 = tracker.build_card(tracker.process_usage_stats(snap_a2))
    assert_card_series_breakdown_consistent(card_a2, expected_series_usd="$0.01")

    # Part B: Sub-cent carry cases across multiple Dalton prod tiers (3x$0.004=$0.012->$0.01; 2x$0.014=$0.028->$0.03; $0.075+$0.06+$0.03=$0.165->$0.16)
    series_cat = usage_sources.load_antigravity_series_catalog()
    for label, tier_costs, expected_disp in (
        ("three_x_004", [("high", 0.004), ("medium", 0.004), ("low", 0.004)], "$0.01"),
        ("two_x_014", [("high", 0.014), ("medium", 0.014)], "$0.03"),
        ("half_cent_165", [("high", 0.075), ("medium", 0.06), ("low", 0.03)], "$0.16"),
    ):
        rows = [
            {
                "source": "Dalton 生产（订阅）",
                "provider": "google",
                "model": f"gemini-4-neon-{eff}-oauth",
                "dalton_prod": True,
                "records": 1,
                "calls": 1,
                "runs": 1,
                "fresh": 1000 * (idx + 1),
                "cache_write": 0,
                "cache_read": 0,
                "output": 0,
                "cost_usd": c_val,
            }
            for idx, (eff, c_val) in enumerate(tier_costs)
        ]
        snap_b = {
            "ok": True,
            "version": "3.1.0",
            "price_catalog": {"generated": "2026-11-14", "effective_notes": []},
            "antigravity_series": series_cat,
            "dalton_prod": {"ok": True, "error": None, "runs": len(rows), "calls": len(rows), "models": len(rows)},
            "collector_status": {"ok": True, "fresh": True},
            "windows": [
                {
                    "days": 7.0,
                    "from": "2026-11-14T04:00:00+00:00",
                    "to": "2026-11-21T04:00:00+00:00",
                    "row_count": len(rows),
                    "rows": rows,
                    "totals": {
                        "tokens": sum(r["fresh"] for r in rows),
                        "records": len(rows),
                        "calls": len(rows),
                        "priced_cost_usd": round(sum(c for _, c in tier_costs), 6),
                        "unpriced_tokens": 0,
                    },
                }
            ],
        }
        card_b = tracker.build_card(tracker.process_usage_stats(snap_b))
        assert_card_series_breakdown_consistent(card_b, expected_series_usd=expected_disp)


def test_r2_tracker_loads_only_sibling_usage_sources_and_rejects_version_mismatch(
    tmp_path: Path,
) -> None:
    """Round 3 Decision #4: tracker only loads sibling usage_sources.py, never falls back, and checks version."""
    import os
    import shutil
    import subprocess

    stale_ws = tmp_path / "stale_openclaw_data"
    stale_scripts = stale_ws / "skills" / "model-usage-cost" / "scripts"
    stale_scripts.mkdir(parents=True)
    (stale_scripts / "usage_sources.py").write_text("# legacy usage_sources without series helpers\n", encoding="utf-8")
    (stale_scripts / "usage_state.py").write_text(
        "from pathlib import Path\ndef resolve_usage_state_dir(): return Path('/tmp')\n",
        encoding="utf-8",
    )

    snap = _make_realistic_series_snapshot(include_series=True)
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ)
    env["OPENCLAW_WORKSPACE"] = str(stale_ws)
    env["DALTON_DATA_ROOT"] = str(stale_ws)
    env.pop("DALTON_ANTIGRAVITY_SERIES_FILE", None)

    # Part A: Running repo tracker with stale OPENCLAW_WORKSPACE MUST succeed using repo's sibling usage_sources.py
    ok_res = subprocess.run(
        [sys.executable, str(TRACKER_PATH), "--input-json", str(snap_path), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert ok_res.returncode == 0, ok_res.stdout + ok_res.stderr
    parsed = json.loads(ok_res.stdout)
    assert any(r["model"] == "Gemini 4 Neon" for r in parsed["top10"])

    # Part B: Isolated copy with NO sibling usage_sources.py MUST fail without falling back to OPENCLAW_WORKSPACE
    isolated_dir = tmp_path / "isolated" / "scripts"
    isolated_dir.mkdir(parents=True)
    isolated_tracker = isolated_dir / "token_weekly_usage_tracker.py"
    shutil.copy2(TRACKER_PATH, isolated_tracker)
    err_no_sibling = subprocess.run(
        [sys.executable, str(isolated_tracker), "--input-json", str(snap_path), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert err_no_sibling.returncode != 0
    assert "找不到同目录匹配版本的 usage_sources.py" in err_no_sibling.stderr
    assert "AttributeError" not in err_no_sibling.stderr

    # Part C: Sibling usage_sources.py from Round 1 (has the 3 Round 1 functions, but missing API version 2 / retired helper)
    r1_root = tmp_path / "round1_tree"
    (r1_root / "scripts").mkdir(parents=True)
    r1_skill = r1_root / "skills" / "model-usage-cost" / "scripts"
    r1_skill.mkdir(parents=True)
    r1_tracker = r1_root / "scripts" / "token_weekly_usage_tracker.py"
    shutil.copy2(TRACKER_PATH, r1_tracker)
    (r1_skill / "usage_state.py").write_text(
        "from pathlib import Path\ndef resolve_usage_state_dir(): return Path('/tmp')\n",
        encoding="utf-8",
    )
    (r1_skill / "usage_sources.py").write_text(
        "def load_antigravity_series_module(): return None\n"
        "def load_antigravity_series_catalog(path=None): return []\n"
        "def _parse_antigravity_key(k): return None\n",
        encoding="utf-8",
    )
    err_r1 = subprocess.run(
        [sys.executable, str(r1_tracker), "--input-json", str(snap_path), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert err_r1.returncode != 0
    assert "usage_sources.py 版本不匹配" in err_r1.stderr
    assert "版本为 0，要求 >= 2" in err_r1.stderr


def test_unpriced_reference_cost_rounds_after_series_merge_across_efforts() -> None:
    """Follow-up #6: reference-only estimates for unpriced multi-effort series allocate cents from
    the merged series total so per-effort displayed reference estimates sum to the series row."""
    from decimal import Decimal
    import re

    tracker = _load_tracker()

    def parse_cents(usd_str: str) -> int:
        return int((Decimal(usd_str.lstrip("$").replace(",", "")) * 100).to_integral_exact())

    custom_series = [
        {
            "series_id": "antigravity-oauth:unpriced-series",
            "series_prefix": "unpriced-series",
            "display_name": "Unpriced Series",
            "qualified_display_name": "Unpriced Series (Antigravity)",
            "vendor": "openai",
            "variants": {
                "high": "unpriced-series-high-oauth",
                "medium": "unpriced-series-medium-oauth",
                "low": "unpriced-series-low-oauth",
            },
        }
    ]

    for tier_refs, expected_series_usd in (
        # 3 x $0.004 = $0.012 -> merged $0.01 (per-tier rounding would yield $0.00+$0.00+$0.00=$0.00)
        ([("high", 0.004), ("medium", 0.004), ("low", 0.004)], "$0.01"),
        # 2 x $0.014 = $0.028 -> merged $0.03 (per-tier rounding would yield $0.01+$0.01=$0.02)
        ([("high", 0.014), ("medium", 0.014)], "$0.03"),
        # 2 x $0.016 = $0.032 -> merged $0.03 (per-tier rounding would yield $0.02+$0.02=$0.04)
        ([("high", 0.016), ("medium", 0.016)], "$0.03"),
    ):
        rows = [
            {
                "source": "dalton-prod",
                "provider": "antigravity-oauth",
                "model": f"unpriced-series-{effort}-oauth",
                "dalton_prod": True,
                "records": 1,
                "calls": 10,
                "runs": 1,
                "fresh": 1000,
                "cache_write": 0,
                "cache_read": 0,
                "output": 200,
                "cost_usd": None,
                "unpriced_tokens": 1200,
                "no_public_price": True,
                "reference_cost_usd": ref_val,
                "reference_model": "gpt-ref",
                "estimated_split": False,
            }
            for effort, ref_val in tier_refs
        ]
        snap = {
            "version": "3.1.0",
            "price_catalog": {"generated": "2026-11-21", "effective_notes": []},
            "antigravity_series": custom_series,
            "dalton_prod": {"ok": True, "error": None, "runs": len(rows), "calls": 10 * len(rows), "models": len(rows)},
            "collector_status": {"ok": True, "fresh": True},
            "windows": [
                {
                    "days": 7.0,
                    "from": "2026-11-14T00:00:00+00:00",
                    "to": "2026-11-21T00:00:00+00:00",
                    "rows": rows,
                }
            ],
        }
        card = tracker.build_card(tracker.process_usage_stats(snap))
        series_el = next(
            el["content"]
            for el in card["body"]["elements"]
            if el.get("tag") == "markdown" and "Unpriced Series (Antigravity)" in el.get("content", "")
            and "· 档位明细:" in el.get("content", "")
        )
        m_series = re.search(r"参考价估算 (\$[0-9,]+\.\d{2})", series_el)
        assert m_series is not None, series_el
        assert m_series.group(1) == expected_series_usd, series_el
        bd_line = series_el.split("· 档位明细:", 1)[1]
        bucket_refs = re.findall(r"无公开价，参考 (\$[0-9,]+\.\d{2})", bd_line)
        assert len(bucket_refs) == len(tier_refs), bd_line
        assert sum(parse_cents(u) for u in bucket_refs) == parse_cents(expected_series_usd), (
            f"Series {expected_series_usd} != buckets {bucket_refs} in {series_el}"
        )
