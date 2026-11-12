"""OC-1 batch 3: nine OpenClaw script tasks on the data-task layer.

Expected schedules are hardcoded from the OpenClaw cron export. This file does
not read ``.agy/``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import os
from pathlib import Path
import sys
from unittest.mock import create_autospec
from uuid import uuid4

import psycopg
import pytest

from src.dalton.data_jobs.batch3_messages import (
    FDE_WEEKLY_DISCORD_TARGET,
    SEMI_BOTTOM_DISCORD_TARGET,
    SUPPRESS_OUTBOX,
    adjust_batch3_payload,
    apply_confirmed_delivery_writeback,
    capture_delivery_writeback_snapshot,
    fde_weekly_failure_text,
)
from src.dalton.data_jobs.container_runner import build_path_map
from src.dalton.data_jobs.host_runner import DataTaskHostBridgeClient
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.data_jobs.notification_payloads import (
    build_data_task_delivery_plan,
    prepare_run_outbox_image_metadata,
    read_validated_run_artifact,
)
from src.dalton.data_jobs.runner_wrapper import SecretRedactor, persist_redacted_outbox
from src.dalton.data_jobs.scheduler import (
    ClaimedTaskRun,
    WRITEBACK_FAILURE_ATTEMPT_LIMIT,
    dispatch_data_task_run_notification,
    execute_claimed_host_run,
    resolve_notify_status,
    retry_unfinished_delivery_writebacks,
)
from src.dalton.database import data_task_notifications as dtn_db
from src.dalton.llm.model_prefs import (
    DALTON_NOTIFY_ROUTES_KEY,
    DALTON_NOTIFY_TARGETS_KEY,
    _new_chat_targets_in_routes,
    is_dalton_chat_target,
    is_dalton_discord_channel_target,
    is_valid_dalton_notify_target,
    is_valid_dalton_personal_target,
    validate_dalton_model_preferences,
)
from src.dalton.notify.dispatch import resolve_notify_route_targets
from src.llms.preferences import TuningError

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "openclaw_overlays"
    / "scripts"
    / "cron_command_tasks.py"
)
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

DEV = "channel:1481256083589697566"
TRACKING = "oc_397207208fb52a0b09969794ef3cd421"
SEMI_BOTTOM = "channel:1480405100672581875"
RUN_ID = "11111111-1111-4111-8111-111111111111"
WS = "/Users/everflow/.openclaw/workspace"

# Schedules, timeouts, and the original command string. ``command_argv`` is
# what Dalton runs: no login shell, and no ``--send``.
BATCH3_EXPECTATIONS: dict[str, dict[str, object]] = {
    "T20": {
        "job_key": "ai-chain-daily",
        "cron": "50 8 * * *",
        "timezone": "America/New_York",
        "timeout_seconds": 900,
        "no_output_timeout_seconds": 900,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py ai-chain-daily",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "ai-chain-daily"],
        "freeze_paths": [
            "skills/ai-chain/state/daily_push.json",
            "skills/ai-chain/state/ingest_health.json",
            "skills/ai-chain/events/bus.jsonl",
            "research-outputs/*-ai-chain-daily.md",
        ],
    },
    "T76": {
        "job_key": "us-etf-monthly-flows",
        "cron": "0 10 3 * *",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 600,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py us-etf-monthly-flows",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "us-etf-monthly-flows"],
        "freeze_paths": [],
    },
    "T42": {
        "job_key": "vol-surface-daily-snapshot",
        "cron": "10 21 * * 1-5",
        "timezone": "America/New_York",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 600,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py vol-surface-daily-snapshot",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "vol-surface-daily-snapshot"],
        "freeze_paths": [
            "skills/hedging-analysis/data/index.json",
            "skills/hedging-analysis/data/snapshots/**/*.parquet",
            "skills/hedging-analysis/data/regimes/*.parquet",
        ],
    },
    "T58": {
        "job_key": "gs-pb-weekly-rundown",
        "cron": "0 9 * * 6",
        "timezone": "America/New_York",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 300,
        "output_max_bytes": 50000,
        "runner": "host",
        "host_mode": "run",
        "command": f"{WS}/skills/company-wiki/venv/bin/python -I scripts/gs_pb_weekly.py run --json",
        "command_argv": [
            f"{WS}/skills/company-wiki/venv/bin/python",
            "-I",
            "scripts/gs_pb_weekly.py",
            "run",
            "--json",
        ],
        "freeze_paths": [
            "state/gs-pb-weekly/state.json",
            "state/gs-pb-weekly/last-run.json",
            "research-outputs/gs-pb-weekly/gs_pb_charts_daily_latest.csv",
            "research-outputs/gs-pb-weekly/gs_pb_core_weekly_latest.csv",
            "research-outputs/gs-pb-weekly/issues/*.json",
        ],
    },
    "T9": {
        "job_key": "fde-hiring-snapshot-daily",
        "cron": "20 6 * * *",
        "timezone": "America/New_York",
        "timeout_seconds": 2400,
        "no_output_timeout_seconds": None,
        "output_max_bytes": None,
        "runner": "container",
        "host_mode": None,
        "command": "sh -lc bash skills/fde-hiring-tracker/scripts/run.sh --quiet --json snapshot --concurrency 8",
        "command_argv": [
            "bash",
            "skills/fde-hiring-tracker/scripts/run.sh",
            "--quiet",
            "--json",
            "snapshot",
            "--concurrency",
            "8",
        ],
        "freeze_paths": [
            "skills/fde-hiring-tracker/state/snapshots/*.json",
        ],
    },
    "T73": {
        "job_key": "fde-hiring-weekly-report",
        "cron": "10 7 * * 1",
        "timezone": "America/New_York",
        "timeout_seconds": 900,
        "no_output_timeout_seconds": None,
        "output_max_bytes": None,
        "runner": "container",
        "host_mode": None,
        "command": "sh -lc bash scripts/fde_hiring_weekly_report.sh",
        "command_argv": ["bash", "scripts/fde_hiring_weekly_report.sh"],
        "freeze_paths": [
            "temp/fde_hiring_weekly.err",
        ],
    },
    "T36": {
        "job_key": "semi-rescue-alert-am",
        "cron": "0 8 * * 2-6",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 560,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "/opt/homebrew/bin/python3 -I scripts/semi_rescue_alert.py run --slot am --send --json",
        "command_argv": [
            "python3",
            "scripts/semi_rescue_alert.py",
            "run",
            "--slot",
            "am",
            "--json",
        ],
        "freeze_paths": [
            "research-outputs/semi-rescue-alert/**/*",
        ],
    },
    "T16": {
        "job_key": "semi-rescue-alert-pm",
        "cron": "0 21 * * 1-5",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 560,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "/opt/homebrew/bin/python3 -I scripts/semi_rescue_alert.py run --slot pm --send --json",
        "command_argv": [
            "python3",
            "scripts/semi_rescue_alert.py",
            "run",
            "--slot",
            "pm",
            "--json",
        ],
        "freeze_paths": [
            "research-outputs/semi-rescue-alert/**/*",
        ],
    },
    "T83": {
        "job_key": "semi-bottom-checklist-1030et",
        "cron": "30 10 * * 1-5",
        "timezone": "America/New_York",
        "timeout_seconds": 300,
        "no_output_timeout_seconds": 240,
        "output_max_bytes": 20000,
        "runner": "container",
        "host_mode": None,
        "command": "/opt/homebrew/bin/python3 -I scripts/semi_bottom_checklist.py run",
        "command_argv": ["python3", "scripts/semi_bottom_checklist.py", "run"],
        "freeze_paths": [],
    },
}


def _overlay():
    spec = importlib.util.spec_from_file_location("batch3_cron_command_tasks_overlay", OVERLAY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _persist(tmp_path: Path, job_key: str, stdout_text: str, *, staging: Path | None = None):
    return persist_redacted_outbox(
        outbox_path=tmp_path / "outbox.json",
        redactor=SecretRedactor(()),
        staging_outbox_path=staging,
        job_key=job_key,
        stdout_text=stdout_text,
    )


def _hand_built_semi_card(*, title: str, subtitle: str, template: str, elements: list[dict]) -> dict:
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": "半导体"},
                    "color": "indigo",
                }
            ],
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


def test_batch3_manifest_matches_openclaw_schedule() -> None:
    manifest = load_data_task_manifest()
    by_code = manifest.by_task_code()
    assert set(BATCH3_EXPECTATIONS).issubset(by_code)
    for code, expected in BATCH3_EXPECTATIONS.items():
        entry = by_code[code]
        assert entry.job_key == expected["job_key"]
        assert entry.cron == expected["cron"]
        assert entry.timezone == expected["timezone"]
        assert entry.timeout_seconds == expected["timeout_seconds"]
        assert entry.no_output_timeout_seconds == expected["no_output_timeout_seconds"]
        assert entry.output_max_bytes == expected["output_max_bytes"]
        assert entry.runner == expected["runner"]
        assert entry.host_mode == expected["host_mode"]
        if code == "T58":
            assert entry.cwd == "var/dalton/openclaw-data"
        else:
            assert entry.cwd == WS
        assert entry.command == expected["command"]
        assert list(entry.command_argv) == expected["command_argv"]
        assert "-lc" not in entry.command_argv
        assert "--send" not in entry.command_argv
        assert list(entry.freeze_paths) == expected["freeze_paths"]
        assert entry.secret_names == []
        assert entry.batch_no == 1
        assert entry.enabled is False
        assert entry.notify_enabled is False
        assert entry.title == expected["job_key"]


def test_ai_chain_and_vol_and_etf_messages_match_openclaw_helpers() -> None:
    mod = _overlay()
    assert mod.DEV == DEV
    assert mod.TRACKING == TRACKING
    assert "ai_chain_delivery_spec" in inspect.getsource(mod.Runner.plan)
    assert "format_vol_surface_row" in inspect.getsource(mod.Runner.plan)
    assert "format_vol_surface_text" in inspect.getsource(mod.Runner.plan)

    summary = "AI 链日报：🟡 一张高优先级卡片"
    spec = mod.ai_chain_delivery_spec(
        {
            "sent": False,
            "quiet": False,
            "target": DEV,
            "summary": summary,
            "high_cards": ["temp/high.png"],
        }
    )
    assert spec == [
        {"channel": "discord", "target": DEV, "text": summary, "media": None},
        {"channel": "discord", "target": DEV, "text": "", "media": "temp/high.png"},
    ]
    assert mod.ai_chain_delivery_spec(
        {"sent": True, "quiet": False, "target": DEV, "summary": summary, "high_cards": ["temp/high.png"]}
    ) == []
    assert mod.ai_chain_delivery_spec(
        {"sent": False, "quiet": True, "target": DEV, "summary": "平静", "high_cards": []}
    ) == []
    with pytest.raises(ValueError, match="投递目标与原任务不一致"):
        mod.ai_chain_delivery_spec(
            {"sent": False, "quiet": False, "target": "channel:1", "summary": summary, "high_cards": []}
        )
    assert mod.ai_chain_failure_text(RuntimeError("采集失败")) == "ai-chain-daily 执行失败：采集失败"

    row = {
        "ticker": "^SPX",
        "ok": True,
        "timestamp": "2026-11-10T16:00:00",
        "spot": 5800,
        "total_options": 1200,
        "atm_iv": {"7D": 12.5, "30D": None, "60D": 15, "90D": 16.2},
        "skew_30d_pp": -1.2,
    }
    rendered = mod.format_vol_surface_text([mod.format_vol_surface_row(row)])
    assert rendered == (
        "每日波动率曲面快照\n"
        "^SPX：数据截止 2026-11-10T16:00:00；spot 5800；期权数 1200；"
        "ATM IV 7D 12.5% / 30D 未取得 / 60D 15% / 90D 16.2%；30D skew -1.2pp\n"
        "数据缺口：无"
    )
    gap = mod.format_vol_surface_text([], error=mod.format_vol_surface_gap({"ticker": "QQQ"}))
    assert gap.endswith("数据缺口：QQQ：采集失败")

    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "美股 ETF 月度流入"}},
        "body": {"elements": [{"tag": "markdown", "content": "样本卡片"}]},
    }
    stdout = "进度\n--- 飞书卡片 JSON (Dry Run) ---\n" + json.dumps(card, ensure_ascii=False)
    assert mod.parse_etf_dry_run_card(stdout) == card
    outbox = mod.etf_dry_run_outbox(stdout)
    assert outbox["chat_id"] == TRACKING
    assert outbox["feishu_cards"] == [{"card": card, "images": [], "chat_id": TRACKING}]
    assert outbox["delivery_writeback"]["kind"] == "etf_discord_mirror"
    assert outbox["delivery_writeback"]["label"] == "ETF 月度资金流"
    assert "已生成报告并推送" not in stdout


def test_silent_jobs_produce_no_outbox(tmp_path: Path) -> None:
    vol_text = (
        "每日波动率曲面快照\n"
        "^SPX：数据截止 2026-11-10T16:00:00；spot 5800；期权数 1200；"
        "ATM IV 7D 12.5% / 30D 未取得 / 60D 15% / 90D 16.2%；30D skew -1.2pp\n"
        "数据缺口：无"
    )
    assert _persist(tmp_path, "vol-surface-daily-snapshot", vol_text) is None
    assert _persist(
        tmp_path,
        "gs-pb-weekly-rundown",
        json.dumps(
            {
                "ok": True,
                "fetched": 1,
                "errors": [],
                "rebuild": False,
                "latest_issue": "2026-11-08",
                "expected_issue": "2026-11-08",
            }
        ),
    ) is None
    assert _persist(
        tmp_path,
        "fde-hiring-snapshot-daily",
        json.dumps({"ok": True, "snapshot_date": "2026-11-11", "totals": {}, "events": [], "failures": []}),
    ) is None
    assert _persist(tmp_path, "ai-chain-daily", "NO_REPLY\n") is None
    assert _persist(tmp_path, "semi-rescue-alert-am", json.dumps({"status": "unchanged", "slot": "am"})) is None
    assert _persist(tmp_path, "semi-bottom-checklist-1030et", "NO_REPLY\n") is None
    assert _persist(tmp_path, "fde-hiring-weekly-report", "   \n") is None
    assert not (tmp_path / "outbox.json").exists()
    assert adjust_batch3_payload(
        job_key="semi-bottom-checklist-1030et",
        payload={"text": "NO_REPLY"},
        stdout_text="NO_REPLY\n",
    ) is SUPPRESS_OUTBOX


def test_fde_weekly_and_semi_bottom_keep_stdout_bytes(tmp_path: Path) -> None:
    assert FDE_WEEKLY_DISCORD_TARGET == DEV
    assert SEMI_BOTTOM_DISCORD_TARGET == SEMI_BOTTOM
    success = "FDE 招聘周报\n第二行\n"
    saved = _persist(tmp_path / "ok", "fde-hiring-weekly-report", success)
    assert saved is not None
    assert saved["text"] == success
    assert saved["feishu_message"] == success
    assert saved["channel"] == "discord"
    assert saved["target"] == DEV
    assert saved["preserve_exact_text"] is True
    assert "notify_on_failure" not in saved
    plan = build_data_task_delivery_plan(
        run_id=RUN_ID,
        job_key="fde-hiring-weekly-report",
        raw_outbox=saved,
        run_artifacts_dir=tmp_path / "ok",
    )
    assert plan.items[0].kind == "text"
    assert plan.items[0].text == success

    err = fde_weekly_failure_text("2026-11-09", "boom line\n")
    assert err == "⚠️ FDE 周报生成失败（2026-11-09），最后 15 行错误：\nboom line\n"
    failed = _persist(tmp_path / "bad", "fde-hiring-weekly-report", err)
    assert failed is not None
    assert failed["text"] == err
    assert failed["notify_on_failure"] is True
    failed_plan = build_data_task_delivery_plan(
        run_id=RUN_ID,
        job_key="fde-hiring-weekly-report",
        raw_outbox=failed,
        run_artifacts_dir=tmp_path / "bad",
    )
    assert failed_plan.items[0].text == err

    checklist = "1. SOX 仍在 200 日线上方\n"
    bottom = _persist(tmp_path / "bottom", "semi-bottom-checklist-1030et", checklist)
    assert bottom is not None
    assert bottom["text"] == checklist
    assert bottom["target"] == SEMI_BOTTOM
    assert bottom["channel"] == "discord"


def test_semi_rescue_card_matches_openclaw_card_json(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    out_dir = workspace / "research-outputs" / "semi-rescue-alert" / "2026-11-11-am"
    out_dir.mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\nfixture"
    chart = workspace / "charts" / "am.png"
    chart.parent.mkdir(parents=True)
    chart.write_bytes(png)
    (workspace / "config").mkdir()
    (workspace / "config" / "semi_rescue_alert.json").write_text(
        json.dumps(
            {
                "title": "半导体环境追踪",
                "chat_id": "oc_fixture_semi_chat",
                "layout": "compact",
                "manual_items": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out_dir / "card.md").write_text("正文一行\nIMG:charts/am.png\n", encoding="utf-8")
    snapshot = {
        "slot": "am",
        "generated_hkt": "11/11 08:00",
        "last_day": "2026-11-10",
        "generated_ts": 1731283200,
        "verdict": {"emoji": "🟢", "text": "观望", "template": "green", "g": 1, "y": 0, "r": 0},
        "indicators": [{"id": "sox", "status": "g"}],
    }
    (out_dir / "snapshot.json").write_bytes(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    adjusted = adjust_batch3_payload(
        job_key="semi-rescue-alert-am",
        payload={"status": "generated", "slot": "am", "dir": "research-outputs/semi-rescue-alert/2026-11-11-am"},
        workspace_dir=workspace,
        run_artifacts_dir=run_dir,
    )
    assert isinstance(adjusted, dict)
    expected = _hand_built_semi_card(
        title="半导体环境追踪早报｜🟢 观望",
        subtitle="11/11 08:00 香港｜美股截至 11/10 收盘",
        template="green",
        elements=[
            {"tag": "markdown", "content": "正文一行"},
            {"tag": "__img__", "path": "charts/am.png"},
        ],
    )
    assert adjusted["feishu_cards"][0]["card"] == expected
    assert adjusted["feishu_cards"][0]["chat_id"] == "oc_fixture_semi_chat"
    assert adjusted["chat_id"] == "oc_fixture_semi_chat"
    image = adjusted["feishu_cards"][0]["images"][0]
    assert image["placeholder"] == "charts/am.png"
    assert image["path"] == "images/card_0_img_0.png"
    assert image["filename"] == "am.png"
    assert image["size_bytes"] == len(png)
    assert image["sha256"] == hashlib.sha256(png).hexdigest()
    assert (run_dir / "images" / "card_0_img_0.png").read_bytes() == png
    assert adjusted["delivery_writeback"] == {
        "kind": "semi_rescue_alert",
        "dir": "research-outputs/semi-rescue-alert/2026-11-11-am",
        "slot": "am",
    }

    pm_dir = workspace / "research-outputs" / "semi-rescue-alert" / "2026-11-11-pm"
    pm_dir.mkdir()
    (pm_dir / "card.md").write_text("正文一行\n", encoding="utf-8")
    pm_snap = dict(snapshot)
    pm_snap["slot"] = "pm"
    (pm_dir / "snapshot.json").write_bytes(
        json.dumps(pm_snap, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    )
    pm = adjust_batch3_payload(
        job_key="semi-rescue-alert-pm",
        payload={"status": "generated", "slot": "pm", "dir": "research-outputs/semi-rescue-alert/2026-11-11-pm"},
        workspace_dir=workspace,
        run_artifacts_dir=run_dir,
    )
    assert isinstance(pm, dict)
    assert pm["feishu_cards"][0]["card"] == _hand_built_semi_card(
        title="半导体环境追踪晚报｜🟢 观望",
        subtitle="11/11 08:00 香港｜美股截至 11/10 收盘",
        template="green",
        elements=[{"tag": "markdown", "content": "正文一行"}],
    )
    assert pm["feishu_cards"][0]["images"] == []

    full_cfg = {
        "title": "半导体环境追踪",
        "chat_id": "oc_fixture_semi_chat",
        "layout": "full",
        "manual_items": ["人工一", "人工二"],
    }
    (workspace / "config" / "semi_rescue_alert.json").write_text(
        json.dumps(full_cfg, ensure_ascii=False),
        encoding="utf-8",
    )
    full_snap = dict(snapshot)
    full_snap["verdict"] = {"emoji": "🟢", "text": "观望", "template": "green", "g": 1, "y": 1, "r": 1}
    (out_dir / "snapshot.json").write_bytes(
        json.dumps(full_snap, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    )
    full = adjust_batch3_payload(
        job_key="semi-rescue-alert-am",
        payload={"status": "generated", "slot": "am", "dir": "research-outputs/semi-rescue-alert/2026-11-11-am"},
        workspace_dir=workspace,
        run_artifacts_dir=run_dir,
    )
    assert isinstance(full, dict)
    assert full["feishu_cards"][0]["card"]["header"]["subtitle"]["content"] == (
        "2024-11-11 08:00 香港时间｜美股截至 11/10 收盘｜自动指标 3 项 + 人工 2 项"
    )


def _openclaw_semi_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def test_writeback_only_after_confirmed_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    push = workspace / "skills" / "ai-chain" / "state" / "daily_push.json"
    push.parent.mkdir(parents=True)
    original = {"sent": False, "summary": "摘要", "target": DEV, "high_cards": []}
    original_bytes = json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8")
    push.write_bytes(original_bytes)
    outbox = {
        "delivery_writeback": {"kind": "ai_chain_daily_push", "path": "skills/ai-chain/state/daily_push.json"}
    }
    ai_run = tmp_path / "run-ai"
    captured = capture_delivery_writeback_snapshot(outbox, workspace=workspace, run_dir=ai_run)
    assert captured["writeback_status"] == "pending"
    assert (ai_run / "writeback" / "daily_push.json").read_bytes() == original_bytes
    for status in ("pending", "failed", "skipped", "preview_only"):
        apply_confirmed_delivery_writeback(captured, workspace, status, run_artifacts_dir=ai_run)
        assert push.read_bytes() == original_bytes
    apply_confirmed_delivery_writeback(captured, workspace, "dispatched", run_artifacts_dir=ai_run)
    sent_obj = {**original, "sent": True}
    sent_bytes = json.dumps(sent_obj, ensure_ascii=False, indent=2).encode("utf-8")
    assert push.read_bytes() == sent_bytes
    assert not sent_bytes.endswith(b"\n")
    apply_confirmed_delivery_writeback(captured, workspace, "dispatched", run_artifacts_dir=ai_run)
    assert push.read_bytes() == sent_bytes
    with pytest.raises(ValueError, match="快照"):
        apply_confirmed_delivery_writeback(captured, workspace, "dispatched")

    snap_dir = workspace / "research-outputs" / "semi-rescue-alert" / "am"
    snap_dir.mkdir(parents=True)
    snap = {
        "generated_hkt": "11/11 08:00",
        "slot": "am",
        "verdict": {"emoji": "🟢", "text": "观望"},
        "indicators": [{"id": "sox", "status": "g"}],
    }
    snap_bytes = _openclaw_semi_bytes(snap)
    (snap_dir / "snapshot.json").write_bytes(snap_bytes)
    news = {
        "selection": [
            {
                "title": "Chip demand rose again today",
                "link": "https://example.com/a",
                "placement": "main",
                "topic": "t1",
                "importance": 2,
            }
        ],
        "topics": [
            {
                "id": "t1",
                "items": [
                    {
                        "title": "Chip demand rose again today",
                        "link": "https://example.com/a",
                        "ts": "2026-11-10",
                        "source": "Reuters",
                    }
                ],
                "mail": [{"key": "mail-1", "text": "mailbox note"}],
            }
        ],
    }
    (snap_dir / "news.json").write_text(json.dumps(news), encoding="utf-8")
    semi_outbox = {
        "delivery_writeback": {
            "kind": "semi_rescue_alert",
            "dir": "research-outputs/semi-rescue-alert/am",
            "slot": "am",
        }
    }
    import src.dalton.data_jobs.batch3_messages as messages

    clock = {"now": 1_000.0, "calls": 0}

    def _time() -> float:
        clock["calls"] += 1
        return clock["now"]

    monkeypatch.setattr(messages.time, "time", _time)
    semi_run = tmp_path / "run-semi"
    captured_semi = capture_delivery_writeback_snapshot(
        semi_outbox, workspace=workspace, run_dir=semi_run
    )
    apply_confirmed_delivery_writeback(
        captured_semi, workspace, "dispatched", run_artifacts_dir=semi_run
    )
    state = workspace / "state" / "semi-rescue-alert"
    assert (state / "last.json").read_bytes() == snap_bytes
    history_line = json.dumps(
        {
            "t": "11/11 08:00",
            "slot": "am",
            "verdict": {"emoji": "🟢", "text": "观望"},
            "status": {"sox": "g"},
        },
        ensure_ascii=False,
        default=str,
    ) + "\n"
    history = (state / "history.jsonl").read_text(encoding="utf-8")
    assert history == history_line
    seen_record = {
        "t": "Chip demand rose again today",
        "ts": 1_000.0,
        "placement": "main",
        "topic": "t1",
        "published": "2026-11-10",
        "source": "Reuters",
        "link": "https://example.com/a",
        "importance": 2,
    }
    seen_obj = {
        "gn:https://example.com/a": seen_record,
        "mail-1": {"t": "mailbox note", "ts": 1_000.0},
    }
    assert (state / "news_seen.json").read_bytes() == _openclaw_semi_bytes(seen_obj)
    assert clock["calls"] == 1
    clock["now"] = 2_000.0
    apply_confirmed_delivery_writeback(
        captured_semi, workspace, "dispatched", run_artifacts_dir=semi_run
    )
    assert clock["calls"] == 1
    assert (state / "history.jsonl").read_text(encoding="utf-8") == history
    assert (state / "news_seen.json").read_bytes() == _openclaw_semi_bytes(seen_obj)

    later = dict(snap)
    later["generated_hkt"] = "11/11 20:00"
    later["slot"] = "pm"
    later_bytes = _openclaw_semi_bytes(later)
    (snap_dir / "snapshot.json").write_bytes(later_bytes)
    later_run = tmp_path / "run-semi-later"
    captured_later = capture_delivery_writeback_snapshot(
        semi_outbox, workspace=workspace, run_dir=later_run
    )
    apply_confirmed_delivery_writeback(
        captured_later, workspace, "dispatched", run_artifacts_dir=later_run
    )
    assert clock["calls"] == 2
    assert (state / "last.json").read_bytes() == later_bytes
    later_line = json.dumps(
        {
            "t": "11/11 20:00",
            "slot": "pm",
            "verdict": {"emoji": "🟢", "text": "观望"},
            "status": {"sox": "g"},
        },
        ensure_ascii=False,
        default=str,
    ) + "\n"
    assert (state / "history.jsonl").read_text(encoding="utf-8") == history_line + later_line
    refreshed = dict(seen_record)
    refreshed["ts"] = 2_000.0
    assert (state / "news_seen.json").read_bytes() == _openclaw_semi_bytes(
        {
            "gn:https://example.com/a": refreshed,
            "mail-1": {"t": "mailbox note", "ts": 2_000.0},
        }
    )

    with pytest.raises(ValueError):
        apply_confirmed_delivery_writeback(
            {"delivery_writeback": {"kind": "ai_chain_daily_push", "path": "../daily_push.json"}},
            workspace,
            "dispatched",
        )
    with pytest.raises(ValueError):
        apply_confirmed_delivery_writeback(
            {"delivery_writeback": {"kind": "ai_chain_daily_push", "path": "/etc/passwd"}},
            workspace,
            "dispatched",
        )
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = workspace / "linked.json"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        apply_confirmed_delivery_writeback(
            {"delivery_writeback": {"kind": "ai_chain_daily_push", "path": "linked.json"}},
            workspace,
            "dispatched",
        )


@pytest.mark.asyncio
async def test_older_semi_writeback_does_not_overwrite_a_newer_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fails, B writes, then A's retry must leave B's shared files alone."""
    import src.dalton.data_jobs.batch3_messages as messages
    import src.dalton.data_jobs.scheduler as scheduler_mod

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    workspace = path_map.backend_data_root
    snap_dir = workspace / "research-outputs" / "semi-rescue-alert" / "am"
    snap_dir.mkdir(parents=True)
    news = {
        "selection": [
            {
                "title": "Chip demand rose again today",
                "link": "https://example.com/a",
                "placement": "main",
                "topic": "t1",
                "importance": 2,
            }
        ],
        "topics": [
            {
                "id": "t1",
                "items": [
                    {
                        "title": "Chip demand rose again today",
                        "link": "https://example.com/a",
                        "ts": "2026-11-10",
                        "source": "Reuters",
                    }
                ],
                "mail": [{"key": "mail-1", "text": "mailbox note"}],
            }
        ],
    }
    (snap_dir / "news.json").write_text(json.dumps(news), encoding="utf-8")
    early = {
        "generated_hkt": "11/11 08:00",
        "slot": "am",
        "verdict": {"emoji": "🟢", "text": "观望"},
        "indicators": [{"id": "sox", "status": "g"}],
    }
    late = dict(early)
    late["generated_hkt"] = "11/11 20:00"
    late["slot"] = "pm"
    early_bytes = _openclaw_semi_bytes(early)
    late_bytes = _openclaw_semi_bytes(late)
    (snap_dir / "snapshot.json").write_bytes(early_bytes)
    a_started = "2026-11-11T08:00:00+00:00"
    b_started = "2026-11-11T20:00:00+00:00"
    a_run = path_map.backend_run_artifacts_dir(RUN_ID)
    captured_a = capture_delivery_writeback_snapshot(
        {
            "delivery_writeback": {
                "kind": "semi_rescue_alert",
                "dir": "research-outputs/semi-rescue-alert/am",
                "slot": "am",
                "run_started_at": a_started,
                "run_id": RUN_ID,
            }
        },
        workspace=workspace,
        run_dir=a_run,
    )
    (snap_dir / "snapshot.json").write_bytes(late_bytes)
    b_run = path_map.backend_run_artifacts_dir(RUN_LATE)
    captured_b = capture_delivery_writeback_snapshot(
        {
            "delivery_writeback": {
                "kind": "semi_rescue_alert",
                "dir": "research-outputs/semi-rescue-alert/am",
                "slot": "pm",
                "run_started_at": b_started,
                "run_id": RUN_LATE,
            }
        },
        workspace=workspace,
        run_dir=b_run,
    )
    assert captured_a["delivery_writeback"]["run_started_at"] == a_started
    clock = {"now": 1_000.0}

    def _time() -> float:
        return clock["now"]

    monkeypatch.setattr(messages.time, "time", _time)
    apply_confirmed_delivery_writeback(
        captured_b, workspace, "dispatched", run_artifacts_dir=b_run
    )
    state = workspace / "state" / "semi-rescue-alert"
    b_line = json.dumps(
        {
            "t": "11/11 20:00",
            "slot": "pm",
            "verdict": {"emoji": "🟢", "text": "观望"},
            "status": {"sox": "g"},
        },
        ensure_ascii=False,
        default=str,
    ) + "\n"
    a_line = json.dumps(
        {
            "t": "11/11 08:00",
            "slot": "am",
            "verdict": {"emoji": "🟢", "text": "观望"},
            "status": {"sox": "g"},
        },
        ensure_ascii=False,
        default=str,
    ) + "\n"
    seen = _openclaw_semi_bytes(
        {
            "gn:https://example.com/a": {
                "t": "Chip demand rose again today",
                "ts": 1_000.0,
                "placement": "main",
                "topic": "t1",
                "published": "2026-11-10",
                "source": "Reuters",
                "link": "https://example.com/a",
                "importance": 2,
            },
            "mail-1": {"t": "mailbox note", "ts": 1_000.0},
        }
    )
    assert (state / "last.json").read_bytes() == late_bytes
    assert (state / "history.jsonl").read_text(encoding="utf-8") == b_line
    assert (state / "news_seen.json").read_bytes() == seen
    marker = json.loads((state / "writeback_order.json").read_text(encoding="utf-8"))
    assert marker == {"started_at": b_started, "run_id": RUN_LATE}

    clock["now"] = 9_000.0
    caplog.set_level(logging.WARNING, logger="src.dalton.data_jobs.batch3_messages")
    apply_confirmed_delivery_writeback(
        captured_a, workspace, "dispatched", run_artifacts_dir=a_run
    )
    assert (state / "last.json").read_bytes() == late_bytes
    assert (state / "history.jsonl").read_text(encoding="utf-8") == b_line
    assert a_line not in (state / "history.jsonl").read_text(encoding="utf-8")
    assert (state / "news_seen.json").read_bytes() == seen
    assert json.loads((state / "writeback_order.json").read_text(encoding="utf-8")) == marker
    assert not (a_run / "writeback" / "news_ts.txt").exists()
    assert any("newer run" in record.message for record in caplog.records)

    (state / "last.json").unlink()
    apply_confirmed_delivery_writeback(
        captured_b, workspace, "dispatched", run_artifacts_dir=b_run
    )
    assert (state / "last.json").read_bytes() == late_bytes
    assert (state / "history.jsonl").read_text(encoding="utf-8") == b_line
    assert (state / "news_seen.json").read_bytes() == seen

    apply_confirmed_delivery_writeback(
        captured_a, workspace, "dispatched", run_artifacts_dir=a_run
    )
    assert (state / "last.json").read_bytes() == late_bytes

    retry_payload = dict(captured_a)
    retry_payload["writeback_status"] = "failed"
    retry_payload["writeback_attempts"] = WRITEBACK_FAILURE_ATTEMPT_LIMIT - 1
    conn = _ScriptedConn(
        [{"run_id": RUN_ID, "job_key": "semi-rescue-alert-am", "outbox_payload": retry_payload}]
    )
    retried = await retry_unfinished_delivery_writebacks(
        conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_refuse_send,
    )
    assert retried == [(RUN_ID, "done")]
    assert (state / "last.json").read_bytes() == late_bytes
    assert (state / "history.jsonl").read_text(encoding="utf-8") == b_line
    assert (state / "news_seen.json").read_bytes() == seen
    done_updates = [
        params
        for sql, params in zip(conn.sql, conn.params)
        if isinstance(params, dict) and params.get("status") == "done" and "writeback_status" in sql
    ]
    assert done_updates[-1]["attempts"] is None
    assert done_updates[-1]["reason"] is None

    class _StartedConn:
        def __init__(self, row: object) -> None:
            self.row = row
            self.sql = ""
            self.params: object = None

        def cursor(self, row_factory: object = None) -> "_StartedConn":
            return self

        async def __aenter__(self) -> "_StartedConn":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def execute(self, sql: object, params: object = None) -> None:
            self.sql = " ".join(str(sql).split())
            self.params = params

        async def fetchone(self) -> object:
            return self.row

    aware = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc)
    aware_conn = _StartedConn({"started_at": aware})
    assert await scheduler_mod._load_run_started_at(aware_conn, RUN_ID) == aware.isoformat()
    assert "SELECT started_at" in aware_conn.sql
    assert aware_conn.params == {"run_id": RUN_ID}
    naive = datetime(2026, 11, 11, 20, 0)
    assert await scheduler_mod._load_run_started_at(
        _StartedConn({"started_at": naive}),
        RUN_LATE,
    ) == naive.replace(tzinfo=timezone.utc).isoformat()
    assert await scheduler_mod._load_run_started_at(
        _StartedConn({"started_at": "2026-11-11T21:00:00Z"}),
        RUN_RETRY,
    ) == "2026-11-11T21:00:00Z"
    assert await scheduler_mod._load_run_started_at(_StartedConn((1,)), RUN_FAIL) is None
    assert await scheduler_mod._load_run_started_at(_StartedConn(None), RUN_ETF) is None


def test_notify_on_failure_status_matrix() -> None:
    assert resolve_notify_status(
        terminal_status="failed",
        notify_enabled=True,
        has_outbox_payload=True,
        is_trial=True,
        notify_on_failure=True,
    ) == "preview_only"
    assert resolve_notify_status(
        terminal_status="failed",
        notify_enabled=False,
        has_outbox_payload=True,
        is_trial=False,
        notify_on_failure=True,
    ) == "preview_only"
    assert resolve_notify_status(
        terminal_status="failed",
        notify_enabled=True,
        has_outbox_payload=True,
        is_trial=False,
        notify_on_failure=True,
    ) == "pending_dispatch"
    assert resolve_notify_status(
        terminal_status="failed",
        notify_enabled=True,
        has_outbox_payload=True,
        is_trial=False,
        notify_on_failure=False,
    ) == "skipped"
    assert resolve_notify_status(
        terminal_status="timed_out",
        notify_enabled=True,
        has_outbox_payload=True,
        notify_on_failure=True,
    ) == "skipped"
    assert resolve_notify_status(
        terminal_status="succeeded",
        notify_enabled=True,
        has_outbox_payload=True,
    ) == "pending_dispatch"
    assert resolve_notify_status(
        terminal_status="succeeded",
        notify_enabled=False,
        has_outbox_payload=True,
        is_trial=True,
    ) == "preview_only"


def test_discord_channel_route_is_not_a_personal_or_feishu_target() -> None:
    channel = "discord:channel:1481256083589697566"
    assert is_valid_dalton_notify_target(channel) is True
    assert is_valid_dalton_personal_target(channel) is False
    assert is_dalton_chat_target(channel) is False
    assert is_dalton_discord_channel_target(channel) is True
    assert is_valid_dalton_notify_target("discord:channel:1234") is False
    assert rb.parse_notify_target(channel) == ("discord", "channel:1481256083589697566")
    assert rb.parse_notify_target("discord:user:123") == ("discord", "user:123")
    with pytest.raises(ValueError):
        rb.parse_notify_target("discord:channel:1234")

    bag = {
        DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
        DALTON_NOTIFY_ROUTES_KEY: {"data:ai-chain-daily": [channel]},
    }
    validate_dalton_model_preferences(bag)
    assert bag[DALTON_NOTIFY_ROUTES_KEY]["data:ai-chain-daily"] == [channel]
    with pytest.raises(TuningError, match="仅支持 feishu:user:<open_id> 或 discord:user:<数字id>"):
        validate_dalton_model_preferences({DALTON_NOTIFY_TARGETS_KEY: [channel]})
    with pytest.raises(TuningError, match="discord:channel:<数字id>"):
        validate_dalton_model_preferences(
            {
                DALTON_NOTIFY_TARGETS_KEY: ["feishu:user:ou_me_1"],
                DALTON_NOTIFY_ROUTES_KEY: {"data:ai-chain-daily": ["discord:channel:abc"]},
            }
        )
    assert _new_chat_targets_in_routes({"data:ai-chain-daily": [channel]}) == set()
    automation = {
        "name": "data:ai-chain-daily",
        "route_key": "data:ai-chain-daily",
        "metadata": {"dalton_kind": "data_task"},
    }
    routes = {"data:ai-chain-daily": [channel, "feishu:user:ou_me_1"]}
    personal = ["feishu:user:ou_me_1"]
    assert resolve_notify_route_targets(
        automation, routes, personal, include_chat_targets=True
    ) == ["feishu:user:ou_me_1", channel]
    assert resolve_notify_route_targets(
        automation, routes, personal, include_chat_targets=False
    ) == ["feishu:user:ou_me_1"]


def test_host_command_spec_accepts_only_manifest_host_entries(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ocw = tmp_path / "ocw"
    data.mkdir()
    ocw.mkdir()
    manifest_argv = list(
        load_data_task_manifest().by_job_key()["gs-pb-weekly-rundown"].command_argv
    )
    data_copy = "var/dalton/openclaw-data"
    bbg = ["bash", str(data / "scripts" / "bbg_remote_refresh.sh")]
    for job_key in ("bbg-data-pull-asia", "bbg-data-pull-us"):
        spec = rb.resolve_host_run_command_spec(
            job_key=job_key,
            command_argv=manifest_argv,
            cwd=data_copy,
            data_root=data,
            ocw_root=ocw,
        )
        assert spec["command_argv"] == bbg
        assert spec["cwd"] == str(data)
        assert spec["workspace_env"] == str(data)

    accepted = rb.resolve_host_run_command_spec(
        job_key="gs-pb-weekly-rundown",
        command_argv=manifest_argv,
        cwd=data_copy,
        data_root=data,
        ocw_root=ocw,
    )
    assert accepted["command_argv"] == manifest_argv
    assert accepted["cwd"] == str(data)
    assert accepted["workspace_env"] == str(data)

    container_argv = ["python3", "scripts/cron_command_tasks.py", "ai-chain-daily"]
    rejected = (
        ("not-a-real-job", manifest_argv, data_copy),
        ("ai-chain-daily", container_argv, WS),
        ("gs-pb-weekly-rundown", None, data_copy),
        ("gs-pb-weekly-rundown", [], data_copy),
        ("gs-pb-weekly-rundown", ["ok", ""], data_copy),
        ("gs-pb-weekly-rundown", ["bad\x00bin"], data_copy),
        ("gs-pb-weekly-rundown", [*manifest_argv, "--extra"], data_copy),
        ("gs-pb-weekly-rundown", manifest_argv, str(ocw)),
        ("gs-pb-weekly-rundown", manifest_argv, str(tmp_path / "elsewhere")),
        ("gs-pb-weekly-rundown", manifest_argv, data_copy + "/"),
    )
    for job_key, argv, cwd in rejected:
        with pytest.raises(rb.HostCommandRejected):
            rb.resolve_host_run_command_spec(
                job_key=job_key,
                command_argv=argv,
                cwd=cwd,
                data_root=data,
                ocw_root=ocw,
            )


@pytest.mark.asyncio
async def test_host_run_rejects_non_manifest_command_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.data_tasks_data_root.mkdir()
    bridge = rb.ResearchDataBridge(cfg)
    launched: list[object] = []

    async def _boom(*args, **kwargs):
        launched.append(args)
        raise AssertionError("host command was launched")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    status, body = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-reject",
            "job_key": "gs-pb-weekly-rundown",
            "host_mode": "run",
            "timeout": 30,
            "no_output_timeout": 15,
            "command_argv": ["python", "-c", "print(1)"],
            "cwd": "var/dalton/openclaw-data",
        }
    )
    assert status == 400
    assert body == {"ok": False, "rejected": True, "error": "host_command_rejected"}
    assert launched == []


@pytest.mark.asyncio
async def test_host_mirror_is_limited_to_bbg_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[tuple] = []

    async def _boom(*args: object, **_kwargs: object) -> None:
        launched.append(tuple(args))
        raise RuntimeError("stop-after-launch")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    async def _call(root: Path, body: dict) -> tuple[int, dict]:
        cfg = rb.BridgeConfig()
        cfg.data_tasks_data_root = root
        root.mkdir()
        bridge = rb.ResearchDataBridge(cfg)
        return await bridge.handle_data_task_host_run(body)

    rejected = (
        ("gs-pb-weekly-rundown", {"host_mode": "mirror"}),
        ("gs-pb-weekly-rundown", {}),
        ("not-a-host-job", {"host_mode": "mirror"}),
        ("ai-chain-daily", {"host_mode": "mirror"}),
    )
    for index, (job_key, extra) in enumerate(rejected):
        launched.clear()
        root = tmp_path / f"reject-{index}"
        status, body = await _call(
            root,
            {
                "generation": 1,
                "run_id": f"reject-{index}",
                "job_key": job_key,
                "timeout": 30,
                "no_output_timeout": 15,
                **extra,
            },
        )
        assert status == 400
        assert body == {"ok": False, "rejected": True, "error": "host_command_rejected"}
        assert launched == []
        assert not (root / ".dalton-runs").exists()

    for index, job_key in enumerate(("bbg-data-pull-asia", "bbg-data-pull-us")):
        launched.clear()
        root = tmp_path / f"allow-{index}"
        with pytest.raises(RuntimeError, match="stop-after-launch"):
            await _call(
                root,
                {
                    "generation": 1,
                    "run_id": f"allow-{index}",
                    "job_key": job_key,
                    "host_mode": "mirror",
                    "timeout": 30,
                    "no_output_timeout": 15,
                },
            )
        assert len(launched) == 1
        blob = next(arg for arg in launched[0] if isinstance(arg, str) and '"mode"' in arg)
        parsed = json.loads(blob)
        assert parsed["mode"] == "mirror"
        assert "command_argv" not in parsed


@pytest.mark.asyncio
async def test_scheduler_forwards_host_argv_only_for_non_bbg_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.data_jobs.scheduler as scheduler_mod

    seen: list[dict] = []

    async def _capture(**kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop-after-args")

    async def _started(*_args, **_kwargs):
        return True

    async def _lock(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler_mod, "start_claimed_host_run", _started)
    monkeypatch.setattr(scheduler_mod, "check_scheduler_lock", _lock)
    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.run_host_task.side_effect = _capture
    now = datetime(2026, 11, 8, 14, 0, tzinfo=timezone.utc)
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")

    def _claimed(job_key: str, argv: list[str]) -> ClaimedTaskRun:
        return ClaimedTaskRun(
            run_id=str(uuid4()),
            job_key=job_key,
            generation=3,
            runner="host",
            command_argv=argv,
            workdir=WS,
            timeout_seconds=60,
            no_output_timeout_seconds=30,
            required_secrets=[],
            cos_mode="skip",
            notify_enabled=False,
            scheduled_slot_utc=now,
            deadline_utc=now + timedelta(minutes=10),
            timezone="America/New_York",
            host_mode="run",
        )

    gs_argv = ["python", "-I", "scripts/gs_pb_weekly.py", "run", "--json"]
    manifest_entry = load_data_task_manifest().by_job_key()["gs-pb-weekly-rundown"]
    with pytest.raises(RuntimeError, match="stop-after-args"):
        await execute_claimed_host_run(
            object(),
            lock_conn=object(),
            host_client=host_client,
            claimed=_claimed("gs-pb-weekly-rundown", gs_argv),
            path_map=path_map,
        )
    assert seen[-1]["command_argv"] == list(manifest_entry.command_argv)
    assert seen[-1]["cwd"] == "var/dalton/openclaw-data"

    drifted = ["python", "not-in-manifest"]
    with pytest.raises(RuntimeError, match="stop-after-args"):
        await execute_claimed_host_run(
            object(),
            lock_conn=object(),
            host_client=host_client,
            claimed=_claimed("not-a-host-job", drifted),
            path_map=path_map,
        )
    assert seen[-1]["command_argv"] == drifted
    assert seen[-1]["cwd"] == WS

    with pytest.raises(RuntimeError, match="stop-after-args"):
        await execute_claimed_host_run(
            object(),
            lock_conn=object(),
            host_client=host_client,
            claimed=_claimed("bbg-data-pull-us", ["bash", "scripts/bbg_remote_refresh.sh"]),
            path_map=path_map,
        )
    assert "command_argv" not in seen[-1]
    assert "cwd" not in seen[-1]


@pytest.mark.asyncio
async def test_authorize_failure_notify_and_skip_discord_group_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Settings:
        enabled = True

    monkeypatch.setattr(dtn_db, "get_dalton_settings", lambda: _Settings())
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-batch3")
    monkeypatch.delenv("DALTON_DISCORD_BOT_TOKEN", raising=False)

    no_report = create_autospec(dtn_db._authorize_saved_report_in_tx, return_value=None)
    monkeypatch.setattr(dtn_db, "_authorize_saved_report_in_tx", no_report)
    verify = create_autospec(
        dtn_db._verify_group_target_deliverable,
        return_value=(False, "not bound", None),
    )
    monkeypatch.setattr(dtn_db, "_verify_group_target_deliverable", verify)
    eligible = create_autospec(
        dtn_db.evaluate_user_notify_eligibility,
        return_value=(True, None, ["discord:channel:1481256083589697566", "feishu:chat:oc_abc"]),
    )
    monkeypatch.setattr(dtn_db, "evaluate_user_notify_eligibility", eligible)

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    rows: dict[str, dict] = {}

    async def _execute(sql, _params=None):
        norm = " ".join(str(sql).split())
        cursor.fetchone.return_value = rows["run"] if "FROM dalton_data_task_runs" in norm else None

    cursor.execute.side_effect = _execute

    def _row(*, status: str, outbox: object, notify_status: str = "pending_dispatch") -> dict:
        return {
            "run_id": RUN_ID,
            "job_key": "ai-chain-daily",
            "status": status,
            "notify_status": notify_status,
            "outbox_payload": outbox,
            "log_tail": None,
            "def_job_key": "ai-chain-daily",
            "task_enabled": True,
            "task_notify_enabled": True,
        }

    rows["run"] = _row(status="failed", outbox={"notify_on_failure": False})
    denied = await dtn_db._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=RUN_ID,
        target="discord:channel:1481256083589697566",
        require_plan=False,
    )
    assert denied.allowed is False
    assert denied.deny_code == "run_not_succeeded"

    rows["run"] = _row(status="failed", outbox=json.dumps({"notify_on_failure": True, "text": "失败"}))
    allowed = await dtn_db._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=RUN_ID,
        target="discord:channel:1481256083589697566",
        require_plan=False,
    )
    assert allowed.allowed is True
    assert allowed.platform == "discord"
    assert allowed.recipient_id == "1481256083589697566"
    assert verify.call_args_list == []
    assert no_report.call_count >= 1
    assert eligible.call_count >= 1

    rows["run"] = _row(status="succeeded", outbox={"text": "ok"})
    feishu = await dtn_db._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=RUN_ID,
        target="feishu:chat:oc_abc",
        require_plan=False,
    )
    assert feishu.allowed is False
    assert feishu.deny_code == "not_allowed"
    assert [call.kwargs["platform"] for call in verify.call_args_list] == ["feishu"]
    assert os.environ["DALTON_PM_USER_IDS"] == "pm-batch3"


CHANNEL_TARGET = "discord:channel:1481256083589697566"
USER_TARGET = "discord:user:1481256083589697566"
RUN_FAIL = "22222222-2222-4222-8222-222222222222"
RUN_RETRY = "33333333-3333-4333-8333-333333333333"
RUN_LATE = "44444444-4444-4444-8444-444444444444"
RUN_ETF = "55555555-5555-4555-8555-555555555555"
RUN_ETF_FAIL = "66666666-6666-4666-8666-666666666666"
RUN_ETF_SKIP = "77777777-7777-4777-8777-777777777777"


class _ScriptedConn:
    def __init__(self, rows: list | None = None) -> None:
        self.rows = list(rows or [])
        self.sql: list[str] = []
        self.params: list[object] = []

    def cursor(self, row_factory: object = None) -> "_ScriptedCursor":
        return _ScriptedCursor(self)

    def transaction(self) -> "_ScriptedConn":
        return self

    async def __aenter__(self) -> "_ScriptedConn":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _ScriptedCursor:
    def __init__(self, conn: _ScriptedConn) -> None:
        self.conn = conn
        self.rowcount = 1

    async def __aenter__(self) -> "_ScriptedCursor":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, sql: object, params: object = None) -> None:
        self.conn.sql.append(" ".join(str(sql).split()))
        self.conn.params.append(params)

    async def fetchall(self) -> list:
        return list(self.conn.rows)

    async def fetchone(self) -> tuple[int]:
        return (1,)


class _LockMustNotOpen:
    def cursor(self, **_kwargs: object) -> object:
        raise AssertionError("empty writeback scan must not take the scheduler lock")


def _persisted_writeback_statuses(conn: _ScriptedConn) -> list[str]:
    found: list[str] = []
    for sql, params in zip(conn.sql, conn.params):
        if "writeback_status" in sql and isinstance(params, dict) and "status" in params:
            found.append(str(params["status"]))
    return found


def _ai_chain_original() -> dict:
    return {"sent": False, "summary": "摘要", "target": DEV, "high_cards": []}


def _stage_ai_chain(workspace: Path, run_dir: Path, payload: dict) -> dict:
    push = workspace / "skills" / "ai-chain" / "state" / "daily_push.json"
    push.parent.mkdir(parents=True, exist_ok=True)
    push.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    return capture_delivery_writeback_snapshot(
        {
            "text": "摘要",
            "delivery_writeback": {
                "kind": "ai_chain_daily_push",
                "path": "skills/ai-chain/state/daily_push.json",
            },
        },
        workspace=workspace,
        run_dir=run_dir,
    )


async def _already_sent(_run_id: str) -> list[dict[str, str]]:
    return [{"channel": "discord", "target": "channel:1481256083589697566", "status": "sent"}]


async def _allow_eligibility(*_args: object, **_kwargs: object) -> tuple[bool, None, list[str]]:
    return True, None, []


async def _refuse_send(*_args: object, **_kwargs: object) -> dict[str, object]:
    raise AssertionError("confirmed delivery must not send the notification again")


def test_ai_chain_markdown_high_cards_are_discord_file_attachments(tmp_path: Path) -> None:
    overlay = _overlay()
    workspace = tmp_path / "ws"
    cards = workspace / "skills" / "ai-chain" / "state" / "cards"
    cards.mkdir(parents=True)
    first_name = "nvda-high.md"
    second_name = "amd-high.md"
    first_body = "# NVDA\n\n高影响：供应约束还在。\n"
    second_body = "# AMD\n\n第二张高影响卡片。\n"
    (cards / first_name).write_text(first_body, encoding="utf-8")
    (cards / second_name).write_text(second_body, encoding="utf-8")
    spec = overlay.ai_chain_delivery_spec(
        {
            "sent": False,
            "summary": "摘要一行",
            "target": DEV,
            "high_cards": [
                f"skills/ai-chain/state/cards/{first_name}",
                f"skills/ai-chain/state/cards/{second_name}",
            ],
        }
    )
    run_dir = workspace / ".dalton-runs" / RUN_ID
    run_rel = tuple(run_dir.relative_to(workspace).parts)
    items: list[dict] = []
    for pos, entry in enumerate(spec):
        item = {
            "index": pos,
            "channel": entry["channel"],
            "target": entry["target"],
            "text": entry["text"],
            "preserve_exact_text": True,
        }
        media = entry.get("media")
        if media:
            src = overlay._validate_workspace_chart(media, workspace, workspace)
            original_name = Path(src.name).name
            retained = overlay._ensure_safe_workspace_subpath(
                workspace, (*run_rel, "attachments", str(pos), original_name)
            )
            retained.parent.mkdir(parents=True, exist_ok=True)
            chart_bytes, chart_sha = overlay._atomic_copy_chart(src, retained)
            item["media"] = f"attachments/{pos}/{original_name}"
            item["filename"] = original_name
            item["chart_bytes"] = chart_bytes
            item["chart_sha256"] = chart_sha
        items.append(item)
    assert [item.get("filename") for item in items] == [None, first_name, second_name]
    assert (run_dir / "attachments" / "1" / first_name).read_text(encoding="utf-8") == first_body
    assert (run_dir / "attachments" / "2" / second_name).read_text(encoding="utf-8") == second_body

    prepared = prepare_run_outbox_image_metadata(
        {"text": "摘要一行", "items": items},
        run_artifacts_dir=run_dir,
        job_key="ai-chain-daily",
    )
    assert prepared is not None
    plan = build_data_task_delivery_plan(
        run_id=RUN_ID,
        job_key="ai-chain-daily",
        raw_outbox=prepared,
        run_artifacts_dir=run_dir,
    )
    assert [item.kind for item in plan.items] == ["text", "text_and_file", "text_and_file"]
    assert plan.items[0].text == "摘要一行"
    loaded = []
    for item in plan.items[1:]:
        assert item.artifact is not None
        assert item.artifact.filename.endswith(".md")
        assert item.artifact.mime_type == "text/markdown"
        loaded.append(
            read_validated_run_artifact(
                run_id=RUN_ID,
                artifact_id=item.artifact.artifact_id,
                plan=plan,
                run_artifacts_dir=run_dir,
            )
        )
    assert [item.filename for item in loaded] == [first_name, second_name]
    assert [item.mime_type for item in loaded] == ["text/markdown", "text/markdown"]
    assert loaded[0].content == first_body.encode("utf-8")
    assert loaded[1].content == second_body.encode("utf-8")
    assert loaded[0].content.startswith(b"# NVDA")


@pytest.mark.asyncio
async def test_confirmed_dispatch_retries_writeback_without_resending(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    workspace = path_map.backend_data_root
    original = _ai_chain_original()
    sent_bytes = json.dumps({**original, "sent": True}, ensure_ascii=False, indent=2).encode("utf-8")
    captured = _stage_ai_chain(workspace, path_map.backend_run_artifacts_dir(RUN_ID), original)
    conn = _ScriptedConn()
    status = await dispatch_data_task_run_notification(
        conn,
        lock_conn=_ScriptedConn(),
        run_id=RUN_ID,
        job_key="ai-chain-daily",
        db_outbox_payload=captured,
        path_map=path_map,
        evaluate_eligibility_fn=_allow_eligibility,
        fetch_deliveries_fn=_already_sent,
        post_notify_fn=_refuse_send,
    )
    push = workspace / "skills" / "ai-chain" / "state" / "daily_push.json"
    assert status == "dispatched"
    assert push.read_bytes() == sent_bytes
    assert _persisted_writeback_statuses(conn) == ["done"]

    failed_conn = _ScriptedConn()
    failed_status = await dispatch_data_task_run_notification(
        failed_conn,
        lock_conn=_ScriptedConn(),
        run_id=RUN_FAIL,
        job_key="ai-chain-daily",
        db_outbox_payload=captured,
        path_map=path_map,
        evaluate_eligibility_fn=_allow_eligibility,
        fetch_deliveries_fn=_already_sent,
        post_notify_fn=_refuse_send,
    )
    assert failed_status == "dispatched"
    assert push.read_bytes() == sent_bytes
    assert _persisted_writeback_statuses(failed_conn) == ["failed"]

    push.write_bytes(json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8"))
    interrupted = _stage_ai_chain(workspace, path_map.backend_run_artifacts_dir(RUN_RETRY), original)
    retry_conn = _ScriptedConn(
        [
            {
                "run_id": RUN_RETRY,
                "job_key": "ai-chain-daily",
                "outbox_payload": interrupted,
            }
        ]
    )
    retried = await retry_unfinished_delivery_writebacks(
        retry_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_refuse_send,
    )
    assert retried == [(RUN_RETRY, "done")]
    assert push.read_bytes() == sent_bytes
    assert _persisted_writeback_statuses(retry_conn) == ["done"]
    assert any("IN ('pending', 'failed')" in sql for sql in retry_conn.sql)
    again = await retry_unfinished_delivery_writebacks(
        retry_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_refuse_send,
    )
    assert again == [(RUN_RETRY, "done")]
    assert push.read_bytes() == sent_bytes

    push.write_bytes(json.dumps(original, ensure_ascii=False, indent=2).encode("utf-8"))
    late = _stage_ai_chain(workspace, path_map.backend_run_artifacts_dir(RUN_LATE), original)
    later_bytes = json.dumps({"sent": False, "summary": "后一次运行"}, ensure_ascii=False).encode("utf-8")
    push.write_bytes(later_bytes)
    late_conn = _ScriptedConn(
        [{"run_id": RUN_LATE, "job_key": "ai-chain-daily", "outbox_payload": late}]
    )
    caplog.set_level(logging.WARNING, logger="src.dalton.data_jobs.batch3_messages")
    overwritten = await retry_unfinished_delivery_writebacks(
        late_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_refuse_send,
    )
    assert overwritten == [(RUN_LATE, "done")]
    assert push.read_bytes() == later_bytes
    assert _persisted_writeback_statuses(late_conn) == ["done"]
    assert any("does not match snapshot" in record.message for record in caplog.records)

    empty = _ScriptedConn([])
    assert await retry_unfinished_delivery_writebacks(
        empty,
        lock_conn=_LockMustNotOpen(),
        path_map=path_map,
        post_notify_fn=_refuse_send,
    ) == []


def _writeback_updates(conn: _ScriptedConn) -> list[dict]:
    found: list[dict] = []
    for sql, params in zip(conn.sql, conn.params):
        if "writeback_status" in sql and isinstance(params, dict) and "status" in params:
            found.append(params)
    return found


@pytest.mark.asyncio
async def test_writeback_failure_stops_after_attempt_limit(tmp_path: Path) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    workspace = path_map.backend_data_root
    original = _ai_chain_original()
    captured = _stage_ai_chain(workspace, path_map.backend_run_artifacts_dir(RUN_ID), original)
    push = workspace / "skills" / "ai-chain" / "state" / "daily_push.json"
    original_bytes = push.read_bytes()
    crossing = dict(captured)
    crossing["writeback_status"] = "failed"
    crossing["writeback_attempts"] = WRITEBACK_FAILURE_ATTEMPT_LIMIT - 1
    posted: list[object] = []

    async def _must_not_post(*_args: object, **_kwargs: object) -> dict[str, object]:
        posted.append(True)
        raise AssertionError("an abandoned writeback must not send")

    crossing_conn = _ScriptedConn(
        [{"run_id": RUN_FAIL, "job_key": "ai-chain-daily", "outbox_payload": crossing}]
    )
    assert await retry_unfinished_delivery_writebacks(
        crossing_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_must_not_post,
    ) == [(RUN_FAIL, "abandoned")]
    assert posted == []
    assert push.read_bytes() == original_bytes
    crossing_update = _writeback_updates(crossing_conn)[-1]
    assert crossing_update["status"] == "abandoned"
    assert crossing_update["attempts"] == WRITEBACK_FAILURE_ATTEMPT_LIMIT
    assert "stopped retrying" in str(crossing_update["reason"])

    state = workspace / "state" / "semi-rescue-alert"
    state.mkdir(parents=True)
    old_last = b'{"slot":"old"}\n'
    (state / "last.json").write_bytes(old_last)
    snap_dir = workspace / "research-outputs" / "semi-rescue-alert" / "am"
    snap_dir.mkdir(parents=True)
    newer = {
        "generated_hkt": "11/11 20:00",
        "slot": "pm",
        "verdict": {"emoji": "🟢", "text": "观望"},
        "indicators": [{"id": "sox", "status": "g"}],
    }
    (snap_dir / "snapshot.json").write_bytes(_openclaw_semi_bytes(newer))
    (snap_dir / "news.json").write_text("{}", encoding="utf-8")
    exhausted_capture = capture_delivery_writeback_snapshot(
        {
            "delivery_writeback": {
                "kind": "semi_rescue_alert",
                "dir": "research-outputs/semi-rescue-alert/am",
                "slot": "pm",
                "run_started_at": "2026-11-11T21:00:00+00:00",
                "run_id": RUN_RETRY,
            }
        },
        workspace=workspace,
        run_dir=path_map.backend_run_artifacts_dir(RUN_RETRY),
    )
    exhausted_capture["writeback_status"] = "failed"
    exhausted_capture["writeback_attempts"] = WRITEBACK_FAILURE_ATTEMPT_LIMIT
    exhausted_conn = _ScriptedConn(
        [
            {
                "run_id": RUN_RETRY,
                "job_key": "semi-rescue-alert-am",
                "outbox_payload": exhausted_capture,
            }
        ]
    )
    assert await retry_unfinished_delivery_writebacks(
        exhausted_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_must_not_post,
    ) == [(RUN_RETRY, "abandoned")]
    assert posted == []
    assert (state / "last.json").read_bytes() == old_last
    assert not (state / "history.jsonl").exists()
    assert not (state / "news_seen.json").exists()
    exhausted_update = _writeback_updates(exhausted_conn)[-1]
    assert exhausted_update["status"] == "abandoned"
    assert exhausted_update["attempts"] == WRITEBACK_FAILURE_ATTEMPT_LIMIT
    assert "stopped retrying" in str(exhausted_update["reason"])


def _write_etf_mirror_helper(workspace: Path) -> None:
    script = workspace / "scripts" / "eve_tracking_mirror.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "CONFIG = {\n"
        "    'enabled': True,\n"
        f"    'source_chat_id': {TRACKING!r},\n"
        "    'targets': [\n"
        "        {'channel': 'discord', 'target': 'user:1481256083589697566'},\n"
        "        {'channel': 'discord', 'target': 'channel:1480405100672581875'},\n"
        "        {'channel': 'feishu', 'target': 'chat:oc_not_discord'},\n"
        "    ],\n"
        "}\n"
        "def load_config():\n"
        "    return CONFIG\n"
        "def should_mirror(chat_id, receive_id_type='chat_id', config=None):\n"
        "    config = CONFIG if config is None else config\n"
        "    return bool(config.get('enabled')) and chat_id == config.get('source_chat_id') and receive_id_type in (None, '', 'chat_id')\n"
        "def card_to_text(card):\n"
        "    title = card['header']['title']['content']\n"
        "    return title + '\\n\\n同步自飞书 Eve tracking 群', []\n"
        "def chunk(text, max_chars=1900, max_lines=17):\n"
        "    return [text]\n",
        encoding="utf-8",
    )


def _etf_outbox(*, chat_id: str = TRACKING) -> dict:
    return {
        "text": "飞书卡片",
        "feishu_cards": [
            {
                "card": {"header": {"title": {"content": "ETF 月度资金流"}}, "body": {"elements": []}},
                "images": [],
                "chat_id": chat_id,
            }
        ],
        "delivery_writeback": {
            "kind": "etf_discord_mirror",
            "chat_id": chat_id,
            "receive_id_type": "chat_id",
            "label": "ETF 月度资金流",
        },
    }


@pytest.mark.asyncio
async def test_etf_discord_mirror_is_sent_only_after_feishu_confirm(tmp_path: Path) -> None:
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    workspace = path_map.backend_data_root
    _write_etf_mirror_helper(workspace)
    expected_text = "ETF 月度资金流\n\n同步自飞书 Eve tracking 群"
    expected_targets = [USER_TARGET, "discord:channel:1480405100672581875"]
    captured = capture_delivery_writeback_snapshot(
        _etf_outbox(),
        workspace=workspace,
        run_dir=path_map.backend_run_artifacts_dir(RUN_ETF),
    )
    spec = captured["delivery_writeback"]
    assert spec["mirror_skipped"] is False
    assert spec["targets"] == expected_targets
    mirror = json.loads((path_map.backend_run_artifacts_dir(RUN_ETF) / "writeback" / "etf_mirror.json").read_text(encoding="utf-8"))
    assert mirror["messages"] == [expected_text]
    assert mirror["targets"] == expected_targets
    etf_dir = path_map.backend_run_artifacts_dir(RUN_ETF)
    plan = build_data_task_delivery_plan(
        run_id=RUN_ETF,
        job_key="us-etf-monthly-flows",
        raw_outbox=captured,
        run_artifacts_dir=etf_dir,
        delivery_platform="discord",
        delivery_target=expected_targets[0],
    )
    assert [item.kind for item in plan.items] == ["text"]
    assert plan.items[0].text == expected_text
    other_plan = build_data_task_delivery_plan(
        run_id=RUN_ETF,
        job_key="us-etf-monthly-flows",
        raw_outbox=captured,
        run_artifacts_dir=etf_dir,
        delivery_platform="discord",
        delivery_target=CHANNEL_TARGET,
    )
    assert [item.kind for item in other_plan.items] == ["feishu_card"]
    assert all(item.text != expected_text for item in other_plan.items)
    untargeted = build_data_task_delivery_plan(
        run_id=RUN_ETF,
        job_key="us-etf-monthly-flows",
        raw_outbox=captured,
        run_artifacts_dir=etf_dir,
        delivery_platform="discord",
    )
    assert [item.kind for item in untargeted.items] == ["feishu_card"]
    posted: list[tuple[list[str], str | None]] = []

    async def _post(targets: list[str], run_id: str | None = None) -> dict[str, object]:
        posted.append((list(targets), run_id))
        return {"ok": True, "status": "sent"}

    conn = _ScriptedConn(
        [{"run_id": RUN_ETF, "job_key": "us-etf-monthly-flows", "outbox_payload": captured}]
    )
    assert await retry_unfinished_delivery_writebacks(
        conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_post,
    ) == [(RUN_ETF, "done")]
    assert posted == [(expected_targets, RUN_ETF)]

    failed_posts: list[list[str]] = []

    async def _fail_post(targets: list[str], run_id: str | None = None) -> dict[str, object]:
        failed_posts.append(list(targets))
        return {"ok": False, "status": "failed", "error": "discord down"}

    failed_capture = capture_delivery_writeback_snapshot(
        _etf_outbox(),
        workspace=workspace,
        run_dir=path_map.backend_run_artifacts_dir(RUN_ETF_FAIL),
    )
    failed_conn = _ScriptedConn(
        [{"run_id": RUN_ETF_FAIL, "job_key": "us-etf-monthly-flows", "outbox_payload": failed_capture}]
    )
    assert await retry_unfinished_delivery_writebacks(
        failed_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_fail_post,
    ) == [(RUN_ETF_FAIL, "failed")]
    assert failed_posts == [expected_targets]

    wrong = capture_delivery_writeback_snapshot(
        _etf_outbox(chat_id="oc_other_chat"),
        workspace=workspace,
        run_dir=tmp_path / "wrong-chat",
    )
    assert wrong["delivery_writeback"]["mirror_skipped"] is True
    (workspace / "scripts" / "eve_tracking_mirror.py").unlink()
    skipped = capture_delivery_writeback_snapshot(
        _etf_outbox(),
        workspace=workspace,
        run_dir=path_map.backend_run_artifacts_dir(RUN_ETF_SKIP),
    )
    assert skipped["delivery_writeback"]["mirror_skipped"] is True
    skipped_file = path_map.backend_run_artifacts_dir(RUN_ETF_SKIP) / "writeback" / "etf_mirror.json"
    skipped_record = json.loads(skipped_file.read_text(encoding="utf-8"))
    skipped_record["skipped"] = True
    skipped_record["targets"] = expected_targets
    skipped_file.write_text(json.dumps(skipped_record), encoding="utf-8")
    stale = dict(skipped)
    stale_spec = dict(skipped["delivery_writeback"])
    stale_spec["mirror_skipped"] = False
    stale_spec["targets"] = expected_targets
    stale["delivery_writeback"] = stale_spec
    skipped_plan = build_data_task_delivery_plan(
        run_id=RUN_ETF_SKIP,
        job_key="us-etf-monthly-flows",
        raw_outbox=stale,
        run_artifacts_dir=path_map.backend_run_artifacts_dir(RUN_ETF_SKIP),
        delivery_platform="discord",
        delivery_target=expected_targets[0],
    )
    assert [item.kind for item in skipped_plan.items] == ["feishu_card"]

    async def _skip_must_not_send(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("a skipped ETF mirror must not send")

    skip_conn = _ScriptedConn(
        [{"run_id": RUN_ETF_SKIP, "job_key": "us-etf-monthly-flows", "outbox_payload": skipped}]
    )
    assert await retry_unfinished_delivery_writebacks(
        skip_conn,
        lock_conn=_ScriptedConn(),
        path_map=path_map,
        post_notify_fn=_skip_must_not_send,
    ) == [(RUN_ETF_SKIP, "done")]


@pytest.mark.asyncio
async def test_discord_channel_target_must_be_guild_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert dtn_db.discord_channel_is_guild_text({"type": 0, "guild_id": "700000001"}) is True
    assert dtn_db.discord_channel_is_guild_text({"type": 5, "guild_id": "700000001"}) is True
    assert dtn_db.discord_channel_is_guild_text({"type": 1, "guild_id": "700000001"}) is False
    assert dtn_db.discord_channel_is_guild_text({"type": 3, "guild_id": "700000001"}) is False
    assert dtn_db.discord_channel_is_guild_text({"type": 0, "guild_id": "  "}) is False
    assert dtn_db.discord_channel_is_guild_text({"type": True, "guild_id": "700000001"}) is False
    assert dtn_db.discord_channel_is_guild_text(None) is False

    class _Settings:
        enabled = True

    monkeypatch.setattr(dtn_db, "get_dalton_settings", lambda: _Settings())
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-batch3")
    monkeypatch.delenv("DALTON_DISCORD_BOT_TOKEN", raising=False)
    unchecked, unchecked_message = await dtn_db.verify_discord_channel_is_guild_text("1481256083589697566")
    assert unchecked is True
    assert unchecked_message is None

    monkeypatch.setattr(dtn_db, "_authorize_saved_report_in_tx", create_autospec(dtn_db._authorize_saved_report_in_tx, return_value=None))
    group_verify = create_autospec(dtn_db._verify_group_target_deliverable, return_value=(False, "not bound", None))
    monkeypatch.setattr(dtn_db, "_verify_group_target_deliverable", group_verify)
    monkeypatch.setattr(
        dtn_db,
        "evaluate_user_notify_eligibility",
        create_autospec(
            dtn_db.evaluate_user_notify_eligibility,
            return_value=(True, None, [CHANNEL_TARGET, USER_TARGET]),
        ),
    )
    fetched: list[tuple[str, str]] = []
    payloads = [
        {"type": 1, "guild_id": "700000001"},
        {"type": 3},
        {"type": 0, "guild_id": ""},
        {"type": 0, "guild_id": "700000001"},
        {"type": 5, "guild_id": "700000001"},
    ]

    def _fetch(channel_id: str, token: str) -> dict:
        fetched.append((channel_id, token))
        return payloads.pop(0)

    monkeypatch.setenv("DALTON_DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(dtn_db, "_fetch_discord_channel_sync", _fetch)
    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    rows = {
        "run": {
            "run_id": RUN_ID,
            "job_key": "ai-chain-daily",
            "status": "succeeded",
            "notify_status": "pending_dispatch",
            "outbox_payload": {"text": "ok"},
            "log_tail": None,
            "def_job_key": "ai-chain-daily",
            "task_enabled": True,
            "task_notify_enabled": True,
        }
    }

    async def _execute(sql: object, _params: object = None) -> None:
        norm = " ".join(str(sql).split())
        cursor.fetchone.return_value = rows["run"] if "FROM dalton_data_task_runs" in norm else None

    cursor.execute.side_effect = _execute
    personal = await dtn_db._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=RUN_ID,
        target=USER_TARGET,
        require_plan=False,
    )
    assert personal.allowed is False
    assert personal.deny_code == "not_linked"
    assert fetched == []
    assert group_verify.call_count == 0

    for expected in ("Discord channel target must be a guild text channel.",) * 3:
        denied = await dtn_db._authorize_run_and_target_in_tx(
            cursor,
            object(),
            run_id=RUN_ID,
            target=CHANNEL_TARGET,
            require_plan=False,
        )
        assert denied.allowed is False
        assert denied.deny_code == "not_allowed"
        assert denied.deny_message == expected
    for _case in (0, 5):
        allowed = await dtn_db._authorize_run_and_target_in_tx(
            cursor,
            object(),
            run_id=RUN_ID,
            target=CHANNEL_TARGET,
            require_plan=False,
        )
        assert allowed.allowed is True
        assert allowed.platform == "discord"
        assert allowed.recipient_id == "1481256083589697566"
    assert [item[0] for item in fetched] == ["1481256083589697566"] * 5
    assert {item[1] for item in fetched} == {"bot-token"}
    assert group_verify.call_count == 0
