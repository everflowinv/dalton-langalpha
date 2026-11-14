"""OC-1 batch 4: seven OpenClaw watchlist scans on the data-task layer.

Expected schedules are hardcoded from the OpenClaw cron export. This file does
not read ``.agy/``.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec

import psycopg
import pytest

from src.dalton.data_jobs.batch3_messages import (
    A_SHARE_FEISHU_DM,
    E_WATCH_DISCORD_DM,
    FDE_WEEKLY_DISCORD_TARGET,
    SEMI_BOTTOM_DISCORD_TARGET,
    SUPPRESS_OUTBOX,
    adjust_batch3_payload,
    apply_confirmed_delivery_writeback,
    canonical_route_target,
    capture_delivery_writeback_snapshot,
    fde_weekly_failure_text,
)
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.data_jobs.notification_payloads import (
    MARKDOWN_ARTIFACT_MIME,
    PLAIN_TEXT_ARTIFACT_MIME,
    _detect_validated_artifact_mime,
    build_data_task_delivery_plan,
    prepare_run_outbox_image_metadata,
    read_validated_run_artifact,
)
from src.dalton.data_jobs.runner_wrapper import (
    CHILD_SITE_DIR,
    SecretRedactor,
    build_child_env,
    persist_redacted_outbox,
    resolve_task_payload_source,
)
from src.dalton.data_jobs.scheduler import resolve_notify_status
from src.dalton.database import data_task_notifications as dtn_db
from src.dalton.llm.model_prefs import (
    is_valid_dalton_notify_target,
    is_valid_dalton_personal_target,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_SCRIPT = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "openclaw_overlays"
    / "scripts"
    / "cron_command_tasks.py"
)
WS = "/Users/everflow/.openclaw/workspace"
RUN_ID = "11111111-1111-4111-8111-111111111111"
V_DM = "user:1134818575211839498"
ASIA = "chat:oc_a6b59323f034cb246c80e1b3684e71d1"
DISCORD_DM = "discord:user:1134818575211839498"
FEISHU_DM = "feishu:user:ou_75b483fb76c31370c715e3a3df1e0a61"
FEISHU_CHAT = "feishu:chat:oc_a6b59323f034cb246c80e1b3684e71d1"
ATTACHMENT_TEXT = "📎 e-watch 全文（含未触发清单与口径）"
A_SHARE_FAIL = "底部放量监控：腾讯行情已连续约 16 分钟取不到数据，这段时间的放量没法提醒，恢复后自动继续。\n"

# Schedules, timeouts, and the original command string. ``command_argv`` is what
# Dalton runs: no login shell. a-share keeps a non-login ``sh -c`` for ``2>>``.
BATCH4_EXPECTATIONS: dict[str, dict[str, object]] = {
    "T35": {
        "job_key": "e-watch-daily",
        "cron": "0 8 * * 1-6",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 1800,
        "no_output_timeout_seconds": 1800,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py e-watch-daily",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "e-watch-daily"],
        "freeze_paths": [
            "temp/e_watch_latest.txt",
            "state/e_watch_signal_state.json",
            "state/e_watch_daily_history.json",
        ],
        "payload_source": "outbox_file",
    },
    "T2": {
        "job_key": "e-watch-intraday",
        "cron": "5,35 * * * *",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 300,
        "no_output_timeout_seconds": None,
        "output_max_bytes": None,
        "runner": "container",
        "host_mode": None,
        "command": "sh -lc python3 scripts/e_watch_monitor.py --mode intraday",
        "command_argv": ["python3", "scripts/e_watch_monitor.py", "--mode", "intraday"],
        "freeze_paths": [],
        "payload_source": "stdout_text",
    },
    "T41": {
        "job_key": "pt-watchlist-asia",
        "cron": "8 10 * * 1-5",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 600,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py pt-watchlist-asia",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "pt-watchlist-asia"],
        "freeze_paths": [
            "temp/pt_watchlist_latest.txt",
            "state/pt_watchlist_signal_state.json",
        ],
        "payload_source": "outbox_file",
    },
    "T23": {
        "job_key": "pt-watchlist-us",
        "cron": "12 22 * * 1-5",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 600,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py pt-watchlist-us",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "pt-watchlist-us"],
        "freeze_paths": [
            "temp/pt_watchlist_latest.txt",
            "state/pt_watchlist_signal_state.json",
        ],
        "payload_source": "outbox_file",
    },
    "T43": {
        "job_key": "china-2c-hk-monitor",
        "cron": "10 10 * * 1-5",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 600,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py china-2c-hk-monitor",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "china-2c-hk-monitor"],
        "freeze_paths": [
            "state/china_2c_monitor_state.json",
            "temp/china_2c_monitor_latest.txt",
        ],
        "payload_source": "outbox_file",
    },
    "T24": {
        "job_key": "china-2c-us-monitor",
        "cron": "15 22 * * 1-5",
        "timezone": "Asia/Hong_Kong",
        "timeout_seconds": 600,
        "no_output_timeout_seconds": 600,
        "output_max_bytes": 50000,
        "runner": "container",
        "host_mode": None,
        "command": "python3 scripts/cron_command_tasks.py china-2c-us-monitor",
        "command_argv": ["python3", "scripts/cron_command_tasks.py", "china-2c-us-monitor"],
        "freeze_paths": [
            "state/china_2c_monitor_state.json",
            "temp/china_2c_monitor_latest.txt",
        ],
        "payload_source": "outbox_file",
    },
    "T38": {
        "job_key": "a-share-bottom-volume-watch",
        "cron": "*/2 9-11,13-15 * * 1-5",
        "timezone": "Asia/Shanghai",
        "timeout_seconds": 120,
        "no_output_timeout_seconds": None,
        "output_max_bytes": None,
        "runner": "container",
        "host_mode": None,
        "command": (
            "sh -lc 'mkdir -p state/a-share-volume-watch && "
            "python3 scripts/a_share_volume_watch.py 2>>state/a-share-volume-watch/stderr.log'"
        ),
        "command_argv": [
            "sh",
            "-c",
            "mkdir -p state/a-share-volume-watch && "
            "python3 scripts/a_share_volume_watch.py 2>>state/a-share-volume-watch/stderr.log",
        ],
        "freeze_paths": [],
        "payload_source": "stdout_text",
    },
}


def _overlay():
    spec = importlib.util.spec_from_file_location("batch4_cron_command_tasks_overlay", OVERLAY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _persist(tmp_path: Path, job_key: str, stdout_text: str, *, staging: Path | None = None):
    outbox_path = tmp_path / "outbox.json"
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    return persist_redacted_outbox(
        outbox_path=outbox_path,
        redactor=SecretRedactor(()),
        staging_outbox_path=staging,
        job_key=job_key,
        stdout_text=stdout_text,
    )


def _text_plan(tmp_path: Path, job_key: str, payload: dict):
    return build_data_task_delivery_plan(
        run_id=RUN_ID,
        job_key=job_key,
        raw_outbox=payload,
        run_artifacts_dir=tmp_path,
    )


def test_batch4_manifest_matches_openclaw_schedule() -> None:
    manifest = load_data_task_manifest()
    by_code = manifest.by_task_code()
    assert set(BATCH4_EXPECTATIONS).issubset(by_code)
    assert len(manifest.tasks) == 37
    for code, expected in BATCH4_EXPECTATIONS.items():
        entry = by_code[code]
        assert entry.job_key == expected["job_key"]
        assert entry.cron == expected["cron"]
        assert entry.timezone == expected["timezone"]
        assert entry.timeout_seconds == expected["timeout_seconds"]
        assert entry.no_output_timeout_seconds == expected["no_output_timeout_seconds"]
        assert entry.output_max_bytes == expected["output_max_bytes"]
        assert entry.runner == expected["runner"]
        assert entry.host_mode == expected["host_mode"]
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
        assert entry.cos_mode == "skip"
        assert resolve_task_payload_source(str(expected["job_key"])) == expected["payload_source"]
        assert resolve_task_payload_source(code) == expected["payload_source"]

    a_share = by_code["T38"].command_argv
    assert a_share[0:2] == ["sh", "-c"]
    assert a_share[2].startswith("mkdir -p state/a-share-volume-watch && ")
    assert "2>>state/a-share-volume-watch/stderr.log" in a_share[2]
    assert by_code["T2"].command.startswith("sh -lc ")
    assert by_code["T38"].command.startswith("sh -lc ")


def test_plan_uses_the_same_delivery_specs_as_the_preview() -> None:
    mod = _overlay()
    source = inspect.getsource(mod.Runner.plan)
    assert "e_watch_daily_delivery_spec" in source
    assert "pt_watchlist_delivery_spec" in source
    assert "china_2c_delivery_spec" in source
    main_source = inspect.getsource(mod.main)
    assert "run_dalton_e_watch_daily_preview" in main_source
    assert "run_dalton_pt_watchlist_preview" in main_source
    assert "run_dalton_china_2c_preview" in main_source
    for fn in (mod.run_dalton_pt_watchlist_preview, mod.run_dalton_china_2c_preview):
        body = inspect.getsource(fn)
        assert "执行失败" in body
        assert "_write_e_watch_failure_outbox" not in body
        assert "_write_staged_monitor_outbox" not in body
    assert mod.V_DM == V_DM
    assert mod.ASIA == ASIA
    assert mod.E_WATCH_ATTACHMENT_TEXT == ATTACHMENT_TEXT


def test_watchlist_messages_match_openclaw_message_helper() -> None:
    mod = _overlay()
    messages = ["早盘：无新增\n", "第二条没有换行"]
    report = "/Users/everflow/.openclaw/workspace/temp/e_watch_latest.txt"
    spec = mod.e_watch_daily_delivery_spec(
        {"ok": True, "messages": messages, "report_file": report}
    )
    assert spec == [
        {"channel": "discord", "target": V_DM, "text": messages[0], "media": None},
        {"channel": "discord", "target": V_DM, "text": messages[1], "media": None},
        {"channel": "discord", "target": V_DM, "text": ATTACHMENT_TEXT, "media": report},
    ]
    assert [mod.message(item["channel"], item["target"], item["text"]) for item in spec[:-1]] == [
        mod.message("discord", V_DM, messages[0]),
        mod.message("discord", V_DM, messages[1]),
    ]
    assert mod.e_watch_daily_delivery_spec({"messages": [], "report_file": report}) == [
        {"channel": "discord", "target": V_DM, "text": ATTACHMENT_TEXT, "media": report},
    ]

    main = "重点标的技术观察｜2026-11-12 10:08｜无新信号（覆盖12只）\n"
    jepq = "JEPQ—QQQ 信号灯由 黄 转为 绿\n第二行"
    obj = {"ok": True, "main_message": main, "jepq_message": jepq, "feishu_target": ASIA}
    items = mod._items_from_spec(mod.pt_watchlist_delivery_spec(obj))
    assert items == [
        mod.message("feishu", ASIA, main),
        mod.message("feishu", ASIA, jepq),
    ]
    assert items[0]["text"] == main
    assert items[1]["text"] == jepq
    assert len(mod.pt_watchlist_delivery_spec({**obj, "jepq_message": ""})) == 1
    assert len(mod.pt_watchlist_delivery_spec({**obj, "jepq_message": None})) == 1
    with pytest.raises(ValueError, match="投递目标与原任务不一致，请检查脚本配置"):
        mod.pt_watchlist_delivery_spec({**obj, "feishu_target": "chat:oc_other"})

    raw = "今天没有需要推送的新事件\n"
    china = mod._items_from_spec(mod.china_2c_delivery_spec(raw))
    assert china == [mod.message("feishu", ASIA, raw)]
    assert china[0]["text"] == raw
    assert china[0]["text"].endswith("\n")
    with pytest.raises(ValueError, match="消费监控输出为空"):
        mod.china_2c_delivery_spec("\n  \n")


def test_e_watch_txt_attachment_is_staged_as_plain_text(tmp_path: Path) -> None:
    mod = _overlay()
    workspace = tmp_path / "ws"
    report = workspace / "temp" / "e_watch_latest.txt"
    report.parent.mkdir(parents=True)
    body = "全文第一行\n未触发：ABC\n"
    report.write_bytes(body.encode("utf-8"))
    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="附件不存在或不在 workspace"):
        mod._validate_workspace_chart(str(outside), workspace, workspace)

    first = "早盘一条，末尾换行要留着\n"
    spec = mod.e_watch_daily_delivery_spec(
        {"messages": [first], "report_file": str(report)}
    )
    run_dir = workspace / ".dalton-runs" / RUN_ID
    run_dir.mkdir(parents=True)
    items = mod.stage_delivery_items(
        spec, workspace, workspace, run_dir, allowed_suffixes=mod.E_WATCH_REPORT_SUFFIXES,
    )
    assert [item["text"] for item in items] == [first, ATTACHMENT_TEXT]
    assert items[0].get("media") is None
    assert items[1]["filename"] == "e_watch_latest.txt"
    assert items[1]["media"] == "attachments/1/e_watch_latest.txt"
    assert (run_dir / "attachments" / "1" / "e_watch_latest.txt").read_bytes() == body.encode("utf-8")

    outbox = {
        "job_key": "e-watch-daily",
        "text": first + "\n\n" + ATTACHMENT_TEXT,
        "items": items,
        "preserve_exact_text": True,
        "channel": "discord",
        "target": V_DM,
        "route_target": DISCORD_DM,
    }
    assert "delivery_writeback" not in outbox
    prepared = prepare_run_outbox_image_metadata(
        outbox,
        run_artifacts_dir=run_dir,
        job_key="e-watch-daily",
    )
    assert prepared is not None
    plan = build_data_task_delivery_plan(
        run_id=RUN_ID,
        job_key="e-watch-daily",
        raw_outbox=prepared,
        run_artifacts_dir=run_dir,
    )
    assert [item.kind for item in plan.items] == ["text", "text_and_file"]
    assert plan.items[0].text == first
    assert plan.items[1].text == ATTACHMENT_TEXT
    assert plan.items[1].artifact is not None
    assert plan.items[1].artifact.filename == "e_watch_latest.txt"
    assert plan.items[1].artifact.mime_type == PLAIN_TEXT_ARTIFACT_MIME
    loaded = read_validated_run_artifact(
        run_id=RUN_ID,
        artifact_id=plan.items[1].artifact.artifact_id,
        plan=plan,
        run_artifacts_dir=run_dir,
    )
    assert loaded.filename == "e_watch_latest.txt"
    assert loaded.mime_type == "text/plain"
    assert loaded.content == body.encode("utf-8")
    assert _detect_validated_artifact_mime(
        b"# card\n", "card.md", text_suffixes={".md"},
    ) == MARKDOWN_ARTIFACT_MIME
    with pytest.raises(Exception, match="Unsupported artifact file extension"):
        _detect_validated_artifact_mime(b"not an image", "chart.txt")


def test_china_stdout_keeps_the_trailing_newline(tmp_path: Path) -> None:
    mod = _overlay()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_dir = workspace / ".dalton-runs" / RUN_ID
    run_dir.mkdir(parents=True)
    raw = "港股消费：今天没有需要推送的新事件\n"
    items = mod.stage_delivery_items(mod.china_2c_delivery_spec(raw), workspace, workspace, run_dir)
    assert items == [{
        "index": 0,
        "channel": "feishu",
        "target": ASIA,
        "text": raw,
        "preserve_exact_text": True,
    }]
    plan = _text_plan(
        run_dir,
        "china-2c-hk-monitor",
        {"text": raw, "items": items, "preserve_exact_text": True},
    )
    assert len(plan.items) == 1
    assert plan.items[0].kind == "text"
    assert plan.items[0].text == raw


def test_announce_stdout_matches_openclaw_and_drops_silence(tmp_path: Path) -> None:
    intraday = "⚡ **e-watch 盘中｜11/12 HKT**　用的是没收盘的日线\n第二行\n"
    saved = _persist(tmp_path / "intra", "e-watch-intraday", intraday)
    assert saved is not None
    assert saved["text"] == intraday
    assert saved["feishu_message"] == intraday
    assert saved["channel"] == "discord"
    assert saved["target"] == E_WATCH_DISCORD_DM == V_DM
    assert saved["route_target"] == DISCORD_DM
    assert saved["format"] == "text"
    assert saved["preserve_exact_text"] is True
    assert saved["notify_on_failure"] is True
    assert saved["job_key"] == "e-watch-intraday"
    assert "delivery_writeback" not in saved
    plan = _text_plan(tmp_path / "intra", "e-watch-intraday", saved)
    assert plan.items[0].kind == "text"
    assert plan.items[0].text == intraday

    alias = adjust_batch3_payload(job_key="T2", payload=None, stdout_text="hello\n")
    assert isinstance(alias, dict)
    assert alias["job_key"] == "e-watch-intraday"
    assert alias["text"] == "hello\n"
    assert alias["route_target"] == canonical_route_target("discord", V_DM)

    share = _persist(tmp_path / "share", "a-share-bottom-volume-watch", A_SHARE_FAIL)
    assert share is not None
    assert share["text"] == A_SHARE_FAIL
    assert share["channel"] == "feishu"
    assert share["target"] == A_SHARE_FEISHU_DM
    assert share["route_target"] == FEISHU_DM
    assert share["notify_on_failure"] is True
    assert "delivery_writeback" not in share
    assert _text_plan(tmp_path / "share", "a-share-bottom-volume-watch", share).items[0].text == A_SHARE_FAIL
    assert resolve_notify_status(
        terminal_status="failed",
        notify_enabled=True,
        has_outbox_payload=True,
        notify_on_failure=True,
    ) == "pending_dispatch"
    assert resolve_notify_status(
        terminal_status="failed",
        notify_enabled=True,
        has_outbox_payload=True,
        notify_on_failure=False,
    ) == "skipped"

    for job_key, silent in (
        ("e-watch-intraday", ""),
        ("e-watch-intraday", "\n  \n"),
        ("e-watch-intraday", "NO_REPLY\n"),
        ("e-watch-intraday", "  NO_REPLY\n后面不再发"),
        ("a-share-bottom-volume-watch", "NO_REPLY"),
        ("T38", " \n"),
    ):
        assert _persist(tmp_path / job_key, job_key, silent) is None
        assert not (tmp_path / job_key / "outbox.json").exists()

    noisy = _persist(tmp_path / "case", "e-watch-intraday", "no_reply\n")
    assert noisy is not None
    assert noisy["text"] == "no_reply\n"


def test_preview_outbox_is_kept_when_stdout_is_no_reply(tmp_path: Path) -> None:
    staged = {
        "schema_version": 1,
        "task_code": "T41",
        "job_key": "pt-watchlist-asia",
        "channel": "feishu",
        "target": ASIA,
        "route_target": FEISHU_CHAT,
        "text": "主消息\n",
        "items": [{
            "index": 0,
            "channel": "feishu",
            "target": ASIA,
            "text": "主消息\n",
            "preserve_exact_text": True,
        }],
        "preserve_exact_text": True,
    }
    staging = tmp_path / "staging.json"
    staging.write_text(json.dumps(staged, ensure_ascii=False), encoding="utf-8")
    saved = _persist(tmp_path, "pt-watchlist-asia", "NO_REPLY\n", staging=staging)
    assert saved is not None
    assert saved["text"] == "主消息\n"
    assert saved["items"][0]["text"] == "主消息\n"
    assert "delivery_writeback" not in saved
    assert not staging.exists()
    assert adjust_batch3_payload(
        job_key="e-watch-daily",
        payload=staged,
        stdout_text="NO_REPLY\n",
    ) is None
    assert adjust_batch3_payload(
        job_key="china-2c-us-monitor",
        payload=staged,
        stdout_text="NO_REPLY\n",
    ) is None


def test_batch3_announce_rules_stay_unchanged() -> None:
    assert adjust_batch3_payload(
        job_key="semi-bottom-checklist-1030et",
        payload=None,
        stdout_text="NO_REPLY\n",
    ) is SUPPRESS_OUTBOX
    kept = adjust_batch3_payload(
        job_key="semi-bottom-checklist-1030et",
        payload=None,
        stdout_text="清单正文\n",
    )
    assert isinstance(kept, dict)
    assert kept["channel"] == "discord"
    assert kept["target"] == SEMI_BOTTOM_DISCORD_TARGET
    assert "notify_on_failure" not in kept
    assert "route_target" not in kept

    failure = fde_weekly_failure_text("2026-11-10", "boom\n")
    failed = adjust_batch3_payload(
        job_key="fde-hiring-weekly-report",
        payload=None,
        stdout_text=failure,
    )
    assert isinstance(failed, dict)
    assert failed["notify_on_failure"] is True
    assert failed["target"] == FDE_WEEKLY_DISCORD_TARGET
    plain = adjust_batch3_payload(
        job_key="fde-hiring-weekly-report",
        payload=None,
        stdout_text="周报正文\n",
    )
    assert isinstance(plain, dict)
    assert "notify_on_failure" not in plain


def test_batch4_has_no_post_confirm_writeback(tmp_path: Path) -> None:
    state = tmp_path / "state" / "e_watch_signal_state.json"
    state.parent.mkdir()
    state.write_text('{"seen":{}}', encoding="utf-8")
    outbox = {
        "job_key": "e-watch-daily",
        "text": "早盘",
        "items": [{
            "index": 0,
            "channel": "discord",
            "target": V_DM,
            "text": "早盘",
            "preserve_exact_text": True,
        }],
    }
    assert "delivery_writeback" not in outbox
    assert apply_confirmed_delivery_writeback(outbox, tmp_path, "dispatched") == []
    assert apply_confirmed_delivery_writeback(outbox, tmp_path, "failed") == []
    assert state.read_text(encoding="utf-8") == '{"seen":{}}'
    with pytest.raises(ValueError, match="写回缺少 delivery_writeback"):
        capture_delivery_writeback_snapshot(
            outbox,
            workspace=tmp_path,
            run_dir=tmp_path / "run",
        )


def test_e_watch_failure_outbox_is_one_discord_dm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    staging = tmp_path / "staging" / "outbox.json"
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(workspace))
    monkeypatch.setenv("DALTON_RUN_ID", RUN_ID)
    monkeypatch.setenv("DALTON_RUN_DIR", str(workspace / ".dalton-runs" / RUN_ID))
    monkeypatch.setenv("DALTON_OUTBOX_FILE", str(staging))
    mod = _overlay()
    mod._write_e_watch_failure_outbox(RuntimeError("采集失败"))
    payload = json.loads(staging.read_text(encoding="utf-8"))
    assert payload["text"] == "e-watch-daily 执行失败：采集失败"
    assert not payload["text"].endswith("\n")
    assert payload["notify_on_failure"] is True
    assert payload["channel"] == "discord"
    assert payload["target"] == V_DM
    assert payload["route_target"] == DISCORD_DM
    assert payload["task_code"] == "T35"
    assert "delivery_writeback" not in payload
    assert payload["items"] == [{
        "index": 0,
        "channel": "discord",
        "target": V_DM,
        "text": "e-watch-daily 执行失败：采集失败",
        "preserve_exact_text": True,
    }]


async def _authorize(cursor, target: str):
    return await dtn_db._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=RUN_ID,
        target=target,
        require_plan=False,
    )


@pytest.mark.asyncio
async def test_personal_targets_use_the_binding_check(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_valid_dalton_personal_target(DISCORD_DM) is True
    assert is_valid_dalton_personal_target(FEISHU_DM) is True
    assert is_valid_dalton_personal_target(FEISHU_CHAT) is False
    assert is_valid_dalton_notify_target(FEISHU_CHAT) is True
    assert canonical_route_target("discord", V_DM) == DISCORD_DM
    assert canonical_route_target("feishu", A_SHARE_FEISHU_DM) == FEISHU_DM
    assert canonical_route_target("feishu", ASIA) == FEISHU_CHAT
    assert canonical_route_target("feishu", "oc_a6b59323f034cb246c80e1b3684e71d1") == FEISHU_CHAT

    class _Settings:
        enabled = True

    monkeypatch.setattr(dtn_db, "get_dalton_settings", lambda: _Settings())
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-batch4")
    monkeypatch.delenv("DALTON_DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        dtn_db,
        "_authorize_saved_report_in_tx",
        create_autospec(dtn_db._authorize_saved_report_in_tx, return_value=None),
    )
    verify = create_autospec(
        dtn_db._verify_group_target_deliverable,
        return_value=(False, "not bound", None),
    )
    monkeypatch.setattr(dtn_db, "_verify_group_target_deliverable", verify)
    eligible = create_autospec(dtn_db.evaluate_user_notify_eligibility, return_value=(True, None, []))
    monkeypatch.setattr(dtn_db, "evaluate_user_notify_eligibility", eligible)
    personal = create_autospec(dtn_db.resolve_dalton_notify_targets, return_value=[])
    monkeypatch.setattr(dtn_db, "resolve_dalton_notify_targets", personal)

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    rows: dict[str, dict | None] = {"run": None, "binding": None, "other": None}
    seen_sql: list[str] = []

    async def _execute(sql, _params=None):
        norm = " ".join(str(sql).split())
        seen_sql.append(norm)
        if "FROM dalton_data_task_runs" in norm:
            cursor.fetchone.return_value = rows["run"]
        elif "platform_user_id = %s" in norm:
            cursor.fetchone.return_value = rows["other"]
        elif "FROM dalton_channel_identities" in norm:
            cursor.fetchone.return_value = rows["binding"]
        else:
            cursor.fetchone.return_value = None

    cursor.execute.side_effect = _execute

    def _succeeded(job_key: str) -> dict:
        return {
            "run_id": RUN_ID,
            "job_key": job_key,
            "status": "succeeded",
            "notify_status": "pending_dispatch",
            "outbox_payload": {"text": "ok"},
            "log_tail": None,
            "def_job_key": job_key,
            "task_enabled": True,
            "task_notify_enabled": True,
        }

    rows["run"] = _succeeded("e-watch-intraday")
    rows["binding"] = {"platform_user_id": "1134818575211839498"}
    eligible.return_value = (True, None, [DISCORD_DM])
    allowed = await _authorize(cursor, DISCORD_DM)
    assert allowed.allowed is True
    assert allowed.target_kind == "dm"
    assert allowed.platform == "discord"
    assert allowed.canonical_target_address == "discord:@me"
    assert allowed.recipient_id == "1134818575211839498"
    assert any("FROM dalton_channel_identities" in sql and "user_id = %s" in sql for sql in seen_sql)
    assert verify.call_args_list == []

    seen_sql.clear()
    eligible.return_value = (True, None, [DISCORD_DM])
    rows["binding"] = {"platform_user_id": "999999999999999999"}
    denied = await _authorize(cursor, DISCORD_DM)
    assert denied.allowed is False
    assert denied.deny_code == "cross_user_target"
    assert any("FROM dalton_channel_identities" in sql for sql in seen_sql)

    seen_sql.clear()
    eligible.return_value = (True, None, ["discord:user:1"])
    rows["binding"] = {"platform_user_id": "1134818575211839498"}
    unrouted = await _authorize(cursor, DISCORD_DM)
    assert unrouted.allowed is False
    assert unrouted.deny_code == "target_not_routed"
    assert not any("FROM dalton_channel_identities" in sql for sql in seen_sql)

    rows["binding"] = None
    eligible.return_value = (True, None, [DISCORD_DM])
    unlinked = await _authorize(cursor, DISCORD_DM)
    assert unlinked.allowed is False
    assert unlinked.deny_code == "not_linked"

    rows["run"] = _succeeded("a-share-bottom-volume-watch")
    rows["binding"] = {"platform_user_id": "ou_75b483fb76c31370c715e3a3df1e0a61"}
    rows["other"] = None
    eligible.return_value = (True, None, [FEISHU_DM])
    personal.reset_mock()
    feishu = await _authorize(cursor, FEISHU_DM)
    assert feishu.allowed is True
    assert feishu.target_kind == "dm"
    assert feishu.platform == "feishu"
    assert feishu.canonical_target_address == "feishu:@me"
    assert feishu.recipient_id == "ou_75b483fb76c31370c715e3a3df1e0a61"
    assert personal.call_count == 0

    rows["binding"] = {"platform_user_id": "ou_dalton_app_bound"}
    personal.return_value = []
    foreign = await _authorize(cursor, FEISHU_DM)
    assert foreign.allowed is False
    assert foreign.deny_code == "cross_user_target"
    assert personal.call_count == 1

    personal.return_value = ["feishu:user:ou_someone_else"]
    mismatched = await _authorize(cursor, FEISHU_DM)
    assert mismatched.allowed is False
    assert mismatched.deny_code == "cross_user_target"

    personal.return_value = [FEISHU_DM]
    exception = await _authorize(cursor, FEISHU_DM)
    assert exception.allowed is True
    assert exception.recipient_id == "ou_dalton_app_bound"
    assert exception.canonical_target_address == "feishu:@me"

    eligible.return_value = (True, None, [FEISHU_CHAT])
    group = await _authorize(cursor, FEISHU_CHAT)
    assert group.allowed is False
    assert group.deny_code == "not_allowed"
    assert verify.call_count == 1
    assert verify.call_args.kwargs["platform"] == "feishu"
    assert verify.call_args.kwargs["chat_address"] == "feishu:oc_a6b59323f034cb246c80e1b3684e71d1"


def _child_env(workspace: Path, staging: Path, job_key: str) -> dict[str, str]:
    """Same child env the wrapper builds when PYTHONPATH is only ``/app`` and ``/app/src``."""
    env = build_child_env(
        base_env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(("/app", "/app/src")),
        },
        workspace_dir=str(workspace),
        run_id=RUN_ID,
        job_key=job_key,
        outbox_file=staging,
    )
    assert env["PYTHONPATH"] == str(CHILD_SITE_DIR)
    assert "src" not in env["PYTHONPATH"].split(os.pathsep)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_overlay_entry(workspace: Path, staging: Path, task: str) -> subprocess.CompletedProcess[str]:
    staging.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(OVERLAY_SCRIPT), task],
        cwd=workspace,
        env=_child_env(workspace, staging, task),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_wrapper_entries_write_outbox_with_only_child_site_on_pythonpath(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    report = workspace / "temp" / "e_watch_latest.txt"
    report.parent.mkdir(parents=True)
    report.write_text("全文第一行\n", encoding="utf-8")
    (scripts / "e_watch_monitor.py").write_text(
        "import json\n"
        "print(json.dumps({"
        "'ok': True, "
        "'messages': ['早盘一条\\n', '第二条没有换行'], "
        f"'report_file': {str(report)!r}"
        "}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    (scripts / "pt_watchlist_monitor.py").write_text(
        "import json\n"
        "print(json.dumps({"
        "'ok': True, "
        "'main_message': '重点标的\\n', "
        "'jepq_message': '', "
        f"'feishu_target': {ASIA!r}"
        "}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    (scripts / "china_2c_monitor.py").write_text(
        "import sys\n"
        "print('今天没有需要推送的新事件\\n', end='')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    cases = (
        ("e-watch-daily", "T35", "discord", V_DM, DISCORD_DM, 3),
        ("pt-watchlist-asia", "T41", "feishu", ASIA, FEISHU_CHAT, 1),
        ("pt-watchlist-us", "T23", "feishu", ASIA, FEISHU_CHAT, 1),
        ("china-2c-hk-monitor", "T43", "feishu", ASIA, FEISHU_CHAT, 1),
        ("china-2c-us-monitor", "T24", "feishu", ASIA, FEISHU_CHAT, 1),
    )
    for task, code, channel, target, route, item_count in cases:
        staging = tmp_path / task / "outbox.json"
        proc = _run_overlay_entry(workspace, staging, task)
        assert proc.returncode == 0, proc.stderr
        assert "ModuleNotFoundError" not in proc.stderr
        assert proc.stdout.strip() == "NO_REPLY"
        payload = json.loads(staging.read_text(encoding="utf-8"))
        assert payload["task_code"] == code
        assert payload["job_key"] == task
        assert payload["channel"] == channel
        assert payload["target"] == target
        assert payload["route_target"] == route
        assert len(payload["items"]) == item_count
        assert "src" not in json.dumps(payload)

    assert json.loads((tmp_path / "e-watch-daily" / "outbox.json").read_text(encoding="utf-8"))["items"][-1]["filename"] == "e_watch_latest.txt"


def test_e_watch_failure_print_survives_outbox_write_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(workspace))
    monkeypatch.setenv("DALTON_RUN_ID", RUN_ID)
    monkeypatch.setenv("DALTON_OUTBOX_FILE", str(tmp_path / "fail-outbox.json"))
    mod = _overlay()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("采集失败")

    def _write_boom(*_args, **_kwargs):
        raise RuntimeError("outbox write failed")

    monkeypatch.setattr(mod, "_run_dalton_e_watch_daily_preview_body", _boom)
    monkeypatch.setattr(mod, "_write_staged_monitor_outbox", _write_boom)
    assert mod.run_dalton_e_watch_daily_preview() == 1
    captured = capsys.readouterr()
    assert captured.out == "e-watch-daily 执行失败：采集失败\n"
    assert not (tmp_path / "fail-outbox.json").exists()


def test_e_watch_inner_timeout_keeps_subprocess_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _overlay()
    sleeper = tmp_path / "sleep.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    argv = [sys.executable, str(sleeper)]
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        mod._run_workspace_argv(argv, tmp_path, timeout=1)
    assert caught.value.cmd == argv
    assert caught.value.timeout == 1
    assert "超时（timeout" not in str(caught.value)
    assert "timed out after 1" in str(caught.value)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(workspace))
    monkeypatch.setenv("DALTON_RUN_ID", RUN_ID)
    monkeypatch.setenv("DALTON_OUTBOX_FILE", str(tmp_path / "timeout-outbox.json"))
    monkeypatch.delenv("DALTON_PYTHON_BIN", raising=False)
    monitor = [sys.executable, "scripts/e_watch_monitor.py"]

    def _timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 540))

    monkeypatch.setattr(mod.subprocess, "run", _timeout)
    assert mod.run_dalton_e_watch_daily_preview() == 1
    captured = capsys.readouterr()
    expired = subprocess.TimeoutExpired(monitor, 540)
    assert captured.out == f"e-watch-daily 执行失败：{expired}\n"
    assert "超时（timeout" not in captured.out
    payload = json.loads((tmp_path / "timeout-outbox.json").read_text(encoding="utf-8"))
    assert payload["text"] == f"e-watch-daily 执行失败：{expired}"
    assert payload["notify_on_failure"] is True
    assert payload["route_target"] == DISCORD_DM
