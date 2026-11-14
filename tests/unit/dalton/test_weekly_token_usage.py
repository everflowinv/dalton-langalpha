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
SKILL_SCRIPTS_DIR = REPO_ROOT / "scripts" / "dalton" / "openclaw_overlays" / "skills" / "model-usage-cost" / "scripts"
TRACKER_PATH = REPO_ROOT / "scripts" / "dalton" / "openclaw_overlays" / "scripts" / "token_weekly_usage_tracker.py"


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
    assert all_rows[0]["model"] == "gemini-4-argon"
    assert all_rows[0]["total_tokens"] == 6_000_000
    assert all_rows[0]["cost_usd"] == pytest.approx(12.0)

    assert len(parsed["prov_list"]) == 1
    assert parsed["prov_list"][0]["tokens"] == 6_000_000
    assert parsed["prov_list"][0]["cost"] == pytest.approx(12.0)
    assert parsed["unpriced_tokens"] == 0
    assert parsed["unpriced_models"] == []

    card_text = json.dumps(tracker.build_card(parsed), ensure_ascii=False)
    assert "gemini-4-argon" in card_text
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
    assert entry.env == {"DALTON_BACKEND_URL": "http://127.0.0.1:8000"}
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
