"""Tests for Item 3: hk-momentum failure details & per-index actual data dates propagated to monitor."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.dalton.data_jobs.evaluators import TaskEvaluationContext, evaluate_task_run
import src.dalton.monitors.db as monitor_db
import src.dalton.monitors.service as monitor_service


def test_evaluate_task_run_extracts_hk_momentum_failure_details_and_index_dates() -> None:
    """When hk-momentum exits non-zero with structured JSON on stdout, evaluator must surface
    failure details and per-index actual data dates rather than raw 'exit_code_1'."""
    stdout_payload = {
        "ok": False,
        "publish_ok": False,
        "last_date": "2026-04-16",
        "stale": True,
        "stale_reason": "smallcap 持有成分缺少必要价格 1 只 (如 00626)",
        "index_dates": {
            "smallcap": "2026-04-16",
            "nonggt": "2026-04-17",
            "hsi": "2026-04-17",
        },
        "missing_held_by_index": {"smallcap": ["00626"], "nonggt": []},
        "failure_summary": (
            "smallcap 持有成分缺少必要价格 1 只 (如 00626) | "
            "数据日期: smallcap=2026-04-16, nonggt=2026-04-17, hsi=2026-04-17"
        ),
    }
    ctx = TaskEvaluationContext(
        job_key="hk-momentum",
        exit_code=1,
        stdout=json.dumps(stdout_payload, ensure_ascii=False) + "\n",
    )
    res = evaluate_task_run(ctx)
    assert res.ok is False
    assert res.status == "failed"
    assert res.failure_reason is not None
    assert res.failure_reason != "exit_code_1"
    assert "00626" in res.failure_reason
    assert "smallcap=2026-04-16" in res.failure_reason
    assert "nonggt=2026-04-17" in res.failure_reason


@pytest.mark.asyncio
async def test_maybe_publish_data_job_monitor_enriches_hk_momentum_exit_code_with_dates_and_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if caller passes 'exit_code_1', maybe_publish_data_job_monitor reads hk_momentum's
    index_meta.json and records both failure details (missing constituents) and per-index dates."""
    st_dir = tmp_path / "state" / "monitor-feeds" / "hk_momentum"
    st_dir.mkdir(parents=True, exist_ok=True)
    (st_dir / "index_meta.json").write_text(
        json.dumps(
            {
                "last_date": "2026-04-16",
                "stale": True,
                "stale_reason": "smallcap 持有成分缺少必要价格 2 只 (如 00626, 01234)",
                "index_dates": {
                    "smallcap": "2026-04-16",
                    "nonggt": "2026-04-17",
                    "hsi": "2026-04-17",
                },
                "missing_held_by_index": {
                    "smallcap": ["00626", "01234"],
                    "nonggt": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(monitor_service, "has_capability", lambda _cap: True)
    monkeypatch.setattr(monitor_service, "is_artifact_publishing_enabled", lambda: True)
    fake_binding = SimpleNamespace(job_key="hk-momentum", slug="hk-momentum")
    monkeypatch.setattr(
        monitor_service,
        "get_registered_monitor_bindings",
        lambda _jk: [fake_binding],
    )
    fake_monitor = SimpleNamespace(monitor_id="mon-hk-1", slug="hk-momentum")
    get_mon_spy = create_autospec(
        monitor_db.get_monitor_by_data_job,
        return_value=fake_monitor,
    )
    record_fail_spy = create_autospec(monitor_db.record_monitor_publish_failure)
    monkeypatch.setattr(monitor_db, "get_monitor_by_data_job", get_mon_spy)
    monkeypatch.setattr(monitor_db, "record_monitor_publish_failure", record_fail_spy)

    await monitor_service.maybe_publish_data_job_monitor(
        job_key="hk-momentum",
        run_id="run-hk-fail-1",
        terminal_status="failed",
        failure_reason="exit_code_1",
        data_root=tmp_path,
    )

    record_fail_spy.assert_awaited_once()
    msg = str(record_fail_spy.await_args.kwargs["error_message"])
    assert msg != "exit_code_1"
    assert "00626" in msg
    assert "smallcap=2026-04-16" in msg
    assert "nonggt=2026-04-17" in msg
    assert "hsi=2026-04-17" in msg


def test_hk_momentum_run_result_includes_index_dates_and_failure_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hk_momentum.run.run_daily_job exposes per-index actual data dates and missing-constituent
    failure details in its returned JSON payload when publish_ok is False."""
    import scripts.dalton.monitor_feeds.hk_momentum.run as hk_run

    st_dir = tmp_path / "state"
    out_dir = tmp_path / "out"
    st_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    fake_computed = {
        "series": {
            "dates": ["2026-04-15", "2026-04-16"],
            "smallcap": [100.0, 101.2],
            "nonggt": [100.0, 102.5],
            "hsi": [100.0, 100.8],
        },
        "constituents": {"smallcap": [], "nonggt": []},
        "meta": {
            "last_date": "2026-04-16",
            "launch_date": "2026-01-01",
            "stale": True,
            "stale_reason": "smallcap 持有成分缺少必要价格 1 只 (如 00626)",
            "index_dates": {
                "smallcap": "2026-04-16",
                "nonggt": "2026-04-17",
                "hsi": "2026-04-17",
            },
            "missing_held_by_index": {"smallcap": ["00626"], "nonggt": []},
            "completeness_ok": False,
            "ok": False,
            "source_statuses": {
                "hsi_history": {
                    "source": "hsi_history",
                    "ok": True,
                    "stale": False,
                    "reason": None,
                    "label": "恒生指数历史",
                },
                "index_compute": {
                    "source": "index_compute",
                    "ok": False,
                    "stale": True,
                    "reason": "smallcap 持有成分缺少必要价格 1 只 (如 00626)",
                    "label": "动量日度计算",
                },
            },
        },
        "can_freeze": False,
    }

    compute_spy = create_autospec(hk_run.compute_all, return_value=fake_computed)
    render_spy = create_autospec(
        hk_run.write_hkmom_panel,
        side_effect=lambda *, output_dir, **kw: (
            (Path(output_dir) / "index.html").write_text("<html>ok</html>", encoding="utf-8")
            and (Path(output_dir) / "index.html")
        ),
    )
    monkeypatch.setattr(hk_run, "compute_all", compute_spy)
    monkeypatch.setattr(hk_run, "write_hkmom_panel", render_spy)

    res = hk_run.run_daily_job(
        state_dir=st_dir,
        output_dir=out_dir,
        skip_fetch=True,
    )
    assert res["publish_ok"] is False
    assert res["index_dates"] == {
        "smallcap": "2026-04-16",
        "nonggt": "2026-04-17",
        "hsi": "2026-04-17",
    }
    assert res["missing_held_by_index"] == {"smallcap": ["00626"], "nonggt": []}
    summary = str(res.get("failure_summary") or "")
    assert "00626" in summary
    assert "smallcap=2026-04-16" in summary
    assert "nonggt=2026-04-17" in summary


def test_hk_momentum_month_end_compute_failure_carries_current_run_diagnostics_and_preserves_previous_published_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a month-end rebalance day when compute_all fails completeness, run_daily_job must:
    1. Propagate THIS run's per-index dates and missing_held_by_index into failure_summary & index_meta.json
       (not the stale prev_index_meta dates).
    2. Preserve the previous published frozen_history.json, series.csv, and prev last_date."""
    from datetime import date
    import scripts.dalton.monitor_feeds.hk_momentum.run as hk_run

    st_dir = tmp_path / "state"
    out_dir = tmp_path / "out"
    st_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Seed previous published artifacts (March 31)
    frozen_seed = {"2026-03": {"smallcap": ["00001"], "nonggt": ["00002"]}}
    (st_dir / "frozen_history.json").write_text(json.dumps(frozen_seed), encoding="utf-8")
    (st_dir / "series.csv").write_text("date,smallcap,nonggt,hsi\n2026-03-31,1000,1000,1000\n", encoding="utf-8")
    (st_dir / "index_meta.json").write_text(
        json.dumps(
            {
                "last_date": "2026-03-31",
                "launch_date": "2026-01-01",
                "index_dates": {
                    "smallcap": "2026-03-31",
                    "nonggt": "2026-03-31",
                    "hsi": "2026-03-31",
                },
            }
        ),
        encoding="utf-8",
    )

    # This run (2026-04-30, month-end rebalance day): smallcap lagged at 2026-04-29 missing 00626,
    # while nonggt & hsi reached 2026-04-30.
    fake_computed = {
        "series": {
            "dates": ["2026-03-31", "2026-04-30"],
            "smallcap": [1000.0, 1010.0],
            "nonggt": [1000.0, 1020.0],
            "hsi": [1000.0, 1005.0],
        },
        "constituents": {"smallcap": [], "nonggt": []},
        "meta": {
            "last_date": "2026-04-29",
            "launch_date": "2026-01-01",
            "stale": True,
            "stale_reason": "smallcap 持有成分缺少必要价格 1 只 (如 00626)",
            "index_dates": {
                "smallcap": "2026-04-29",
                "nonggt": "2026-04-30",
                "hsi": "2026-04-30",
            },
            "missing_held_by_index": {"smallcap": ["00626"], "nonggt": []},
            "completeness_ok": False,
            "ok": False,
            "source_statuses": {
                "hsi_history": {"source": "hsi_history", "ok": True, "stale": False, "reason": None, "label": "恒生指数历史"},
                "index_compute": {
                    "source": "index_compute",
                    "ok": False,
                    "stale": True,
                    "reason": "smallcap 持有成分缺少必要价格 1 只 (如 00626)",
                    "label": "动量日度计算",
                },
            },
        },
        "can_freeze": False,
    }

    monkeypatch.setattr(hk_run, "is_trading_day", create_autospec(hk_run.is_trading_day, return_value=True))
    monkeypatch.setattr(hk_run, "last_trading_day_of_month", create_autospec(hk_run.last_trading_day_of_month, return_value=date(2026, 4, 30)))
    monkeypatch.setattr(hk_run, "compute_all", create_autospec(hk_run.compute_all, return_value=fake_computed))
    monkeypatch.setattr(
        hk_run,
        "write_hkmom_panel",
        create_autospec(
            hk_run.write_hkmom_panel,
            side_effect=lambda *, output_dir, **kw: (
                (Path(output_dir) / "index.html").write_text("<html>prev</html>", encoding="utf-8")
                and (Path(output_dir) / "index.html")
            ),
        ),
    )

    res = hk_run.run_daily_job(
        state_dir=st_dir,
        output_dir=out_dir,
        skip_fetch=True,
        as_of_date=date(2026, 4, 30),
    )

    assert res["ok"] is False
    assert res["month_end_failed"] is True
    # Previous published files & last_date preserved!
    assert res["last_date"] == "2026-03-31"
    assert json.loads((st_dir / "frozen_history.json").read_text(encoding="utf-8")) == frozen_seed
    assert "2026-04-30" not in (st_dir / "series.csv").read_text(encoding="utf-8")
    # THIS run's diagnostic dates & missing constituents propagated!
    assert res["index_dates"] == {
        "smallcap": "2026-04-29",
        "nonggt": "2026-04-30",
        "hsi": "2026-04-30",
    }
    assert res["missing_held_by_index"] == {"smallcap": ["00626"], "nonggt": []}
    summary = str(res.get("failure_summary") or "")
    assert "00626" in summary
    assert "smallcap=2026-04-29" in summary
    assert "nonggt=2026-04-30" in summary
    saved_meta = json.loads((st_dir / "index_meta.json").read_text(encoding="utf-8"))
    assert saved_meta["last_date"] == "2026-03-31"
    assert saved_meta["index_dates"]["smallcap"] == "2026-04-29"
    assert saved_meta["index_dates"]["nonggt"] == "2026-04-30"


def test_hk_failure_summary_and_db_clip_preserve_index_dates_under_1000_chars() -> None:
    """r4 Med #5: even when the raw error body exceeds 1000 chars, both _format_hk_failure_summary
    and record_monitor_publish_failure's _clip_publish_error preserve trailing `| 数据日期: ...`."""
    from scripts.dalton.monitor_feeds.hk_momentum import run as hk_run
    from src.dalton.monitors import db as monitor_db

    huge_reason = "采集异常: " + ("x" * 1500)
    dates = {"smallcap": "2026-04-29", "nonggt": "2026-04-30", "hsi": "2026-04-30"}
    formatted = hk_run._format_hk_failure_summary(
        stale_reason=huge_reason,
        index_dates=dates,
    )
    assert formatted is not None
    assert len(formatted) <= 1000
    assert "| 数据日期: smallcap=2026-04-29, nonggt=2026-04-30, hsi=2026-04-30" in formatted

    # Also test DB-layer defense-in-depth clipping on an unclipped >1000-char error with date suffix
    raw_overlong = ("E" * 1300) + " | 数据日期: smallcap=2026-04-29, nonggt=2026-04-30"
    clipped = monitor_db._clip_publish_error(raw_overlong, limit=1000)
    assert len(clipped) <= 1000
    assert clipped.endswith(" | 数据日期: smallcap=2026-04-29, nonggt=2026-04-30")
