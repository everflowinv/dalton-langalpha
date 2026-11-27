"""OC-1 batch 5: T34 kweb relative dashboard on the host data-task runner.

Expected schedule is hardcoded from the OpenClaw cron export. This file does
not read ``.agy/``. Podcast jobs stay on OpenClaw and have no manifest here.
T34 uses the ``kweb_dashboard_cli.py`` overlay so ``deploy --no-upload --json``
updates the virtual portfolio without uploading to Tencent COS.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from src.dalton.data_jobs.batch3_messages import adjust_batch3_payload
from src.dalton.data_jobs.container_runner import build_path_map
from src.dalton.data_jobs.host_runner import DataTaskHostBridgeClient
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.data_jobs.runner_wrapper import (
    TASK_NOTIFY_PAYLOAD_SOURCES,
    SecretRedactor,
    _TASK_ID_PAYLOAD_SOURCE_ALIASES,
    persist_redacted_outbox,
    resolve_task_payload_source,
)
from src.dalton.data_jobs.scheduler import (
    ClaimedTaskRun,
    _resolve_run_notify_payload,
    execute_claimed_host_run,
    resolve_notify_status,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

JOB_KEY = "richard-relative-dashboard-refresh"
DATA_COPY_CWD = "var/dalton/openclaw-data"
OPENCLAW_CWD = "/Users/everflow/.openclaw/workspace"
KWEB_ARGV = [
    "bash",
    "skills/kweb-dashboard/scripts/run.sh",
    "deploy",
    "--no-upload",
    "--json",
]
KWEB_COMMAND = "bash skills/kweb-dashboard/scripts/run.sh deploy --no-upload --json"
FREEZE_PATHS = [
    "research-outputs/kweb-dashboard/index.html",
]
DISCORD_USER = "user:1333343845827739700"


def _deploy_stdout() -> str:
    """Realistic ``deploy --json`` object. It has no chat-message keys."""
    payload = {
        "ok": True,
        "output": "research-outputs/kweb-dashboard/index.html",
        "bytes": 120034,
        "title": "Richard 相对看板",
        "window_start": "2023-01-01",
        "sections": [{"id": "kweb"}],
        "pairs": 28,
        "charts": 28,
        "generated_at": "2026-11-12 18:20 EST",
        "warnings": [f"page note mentions discord {DISCORD_USER}"],
        "overlong_notes": [],
        "readings": [{"name": "腾讯", "pair": "0700.HK/KWEB"}],
        "portfolio": {"ok": True, "signal_date": "2026-11-12"},
        "etf_board": {"ok": True, "rows": 40, "errors": []},
        "upload": {
            "ok": True,
            "bucket": "everflow-1320643462",
            "region": "ap-hongkong",
            "key": "kweb.html",
            "public_url": "https://eve.lumos.space/kweb.html",
        },
        "uploaded": True,
    }
    return json.dumps(payload, ensure_ascii=False)


def _persist(tmp_path: Path, stdout_text: str) -> dict | None:
    outbox_path = tmp_path / "outbox.json"
    return persist_redacted_outbox(
        outbox_path=outbox_path,
        redactor=SecretRedactor(()),
        job_key=JOB_KEY,
        stdout_text=stdout_text,
    )


def test_t34_manifest_matches_openclaw_schedule() -> None:
    manifest = load_data_task_manifest()
    by_code = manifest.by_task_code()
    by_key = manifest.by_job_key()
    assert len(manifest.tasks) == 42
    assert "T34" in by_code
    entry = by_code["T34"]
    assert entry is by_key[JOB_KEY]
    assert entry.job_key == JOB_KEY
    assert entry.title == JOB_KEY
    assert entry.batch_no == 1
    assert entry.runner == "host"
    assert entry.host_mode == "run"
    assert entry.cron == "20 18 * * 1-5"
    assert entry.timezone == "America/New_York"
    assert entry.timeout_seconds == 900
    assert entry.no_output_timeout_seconds == 600
    assert entry.output_max_bytes == 50000
    assert entry.cwd == DATA_COPY_CWD == rb.HOST_DATA_COPY_CWD
    assert entry.command == KWEB_COMMAND
    assert list(entry.command_argv) == KWEB_ARGV
    assert "--allow-warnings" not in entry.command_argv
    assert "-lc" not in entry.command_argv
    assert "cron_command_tasks.py" not in entry.command
    assert list(entry.freeze_paths) == FREEZE_PATHS
    assert list(entry.secret_names) == []
    assert entry.cos_mode == "skip"
    assert entry.notify_enabled is False
    assert entry.enabled is False
    job_keys = {task.job_key for task in manifest.tasks}
    assert "podcast-weekly-pipeline" in job_keys
    assert "T60" in by_code
    assert "podcast-weekly-watch" not in job_keys
    assert "podcast-weekly-publish" not in job_keys
    for code in ("T63", "T65"):
        assert code not in by_code


def test_t34_stdout_does_not_become_a_message(tmp_path: Path) -> None:
    assert resolve_task_payload_source(JOB_KEY) == "none"
    assert resolve_task_payload_source("T34") == "none"
    stdout = _deploy_stdout()
    parsed = json.loads(stdout)
    assert DISCORD_USER in stdout
    for trigger in ("feishu_message", "feishu_target", "report_text", "report", "card"):
        assert trigger not in parsed

    assert _persist(tmp_path / "json", stdout) is None
    assert _persist(tmp_path / "empty", "") is None
    assert _persist(tmp_path / "blank", " \n\t") is None
    assert not (tmp_path / "json" / "outbox.json").exists()
    assert not (tmp_path / "empty" / "outbox.json").exists()
    assert not (tmp_path / "blank" / "outbox.json").exists()

    payload = {
        "ok": True,
        "uploaded": True,
        "warnings": [f"discord {DISCORD_USER}"],
    }
    original = dict(payload)
    assert (
        adjust_batch3_payload(
            job_key=JOB_KEY,
            payload=payload,
            stdout_text=stdout,
        )
        is None
    )
    assert (
        adjust_batch3_payload(
            job_key="T34",
            payload=payload,
            stdout_text=stdout,
        )
        is None
    )
    assert payload == original
    assert "channel" not in payload
    assert "target" not in payload

    assert (
        resolve_notify_status(
            terminal_status="succeeded",
            notify_enabled=False,
            has_outbox_payload=False,
        )
        == "skipped"
    )
    assert (
        resolve_notify_status(
            terminal_status="succeeded",
            notify_enabled=True,
            has_outbox_payload=False,
        )
        == "skipped"
    )

    text, card, resolved = _resolve_run_notify_payload(
        "run-t34-silent",
        JOB_KEY,
        log_tail=stdout,
        path_map=build_path_map(
            host_var_dir=tmp_path / "host-var",
            backend_var_dir=tmp_path / "backend-var",
        ),
    )
    assert text == ""
    assert card is None
    assert resolved.get("channel") is None
    assert resolved.get("target") is None
    assert DISCORD_USER not in text


def test_host_whitelist_accepts_t34_manifest_command(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ocw = tmp_path / "ocw"
    data.mkdir()
    ocw.mkdir()
    accepted = rb.resolve_host_run_command_spec(
        job_key=JOB_KEY,
        command_argv=list(KWEB_ARGV),
        cwd=DATA_COPY_CWD,
        data_root=data,
        ocw_root=ocw,
    )
    assert accepted["command_argv"] == KWEB_ARGV
    assert accepted["cwd"] == str(data)
    assert accepted["workspace_env"] == str(data)

    rejected = (
        (JOB_KEY, None, DATA_COPY_CWD),
        (JOB_KEY, [], DATA_COPY_CWD),
        (JOB_KEY, ["bash", ""], DATA_COPY_CWD),
        (JOB_KEY, ["bad\x00bin"], DATA_COPY_CWD),
        (JOB_KEY, [*KWEB_ARGV, "--allow-warnings"], DATA_COPY_CWD),
        (JOB_KEY, KWEB_ARGV, OPENCLAW_CWD),
        (JOB_KEY, KWEB_ARGV, str(ocw)),
        (JOB_KEY, KWEB_ARGV, DATA_COPY_CWD + "/"),
        ("not-a-real-job", KWEB_ARGV, DATA_COPY_CWD),
        ("podcast-weekly-pipeline", KWEB_ARGV, DATA_COPY_CWD),
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


def _host_body(root_name: str, **extra: object) -> dict:
    body = {
        "generation": 1,
        "run_id": root_name,
        "job_key": JOB_KEY,
        "host_mode": "run",
        "timeout": 30,
        "no_output_timeout": 15,
        "command_argv": list(KWEB_ARGV),
        "cwd": DATA_COPY_CWD,
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_host_run_rejects_t34_drift_and_mirror_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[object] = []

    async def _boom(
        program: str,
        *args: str,
        stdin: object = None,
        stdout: object = None,
        stderr: object = None,
        limit: int | None = None,
        **kwds: object,
    ) -> None:
        launched.append((program, args, stdin, stdout, stderr, limit, kwds))
        raise AssertionError("host command was launched")

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_boom),
    )

    async def _call(root: Path, body: dict) -> tuple[int, dict]:
        cfg = rb.BridgeConfig()
        cfg.data_tasks_data_root = root
        root.mkdir()
        bridge = rb.ResearchDataBridge(cfg)
        return await bridge.handle_data_task_host_run(body)

    cases = (
        {"command_argv": ["python", "-c", "print(1)"]},
        {"command_argv": [*KWEB_ARGV, "--allow-warnings"]},
        {"cwd": OPENCLAW_CWD},
        {"cwd": DATA_COPY_CWD + "/"},
        {"host_mode": "mirror"},
        {"host_mode": "mirror", "command_argv": list(KWEB_ARGV), "cwd": DATA_COPY_CWD},
    )
    for index, extra in enumerate(cases):
        launched.clear()
        root = tmp_path / f"reject-{index}"
        status, body = await _call(root, _host_body(f"reject-{index}", **extra))
        assert status == 400
        assert body["ok"] is False
        assert body["rejected"] is True
        assert body["error"] == "host_command_rejected"
        assert launched == []
        assert not (root / ".dalton-runs").exists()


@pytest.mark.asyncio
async def test_host_run_accepts_t34_and_points_workspace_at_data_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[tuple] = []

    async def _boom(
        program: str,
        *args: str,
        stdin: object = None,
        stdout: object = None,
        stderr: object = None,
        limit: int | None = None,
        **kwds: object,
    ) -> None:
        del stdin, stdout, stderr, limit
        launched.append(((program, *args), kwds))
        raise RuntimeError("stop-after-launch")

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_boom),
    )
    root = (tmp_path / "openclaw-data").resolve()
    root.mkdir()
    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = root
    bridge = rb.ResearchDataBridge(cfg)
    with pytest.raises(RuntimeError, match="stop-after-launch"):
        await bridge.handle_data_task_host_run(_host_body("run-t34-accept"))
    assert len(launched) == 1
    args, kwargs = launched[0]
    blob = next(arg for arg in args if isinstance(arg, str) and '"mode"' in arg)
    parsed = json.loads(blob)
    assert parsed["mode"] == "command"
    assert parsed["command_argv"] == KWEB_ARGV
    assert parsed["cwd"] == str(root)
    assert kwargs["cwd"] == str(root)
    assert kwargs["env"]["OPENCLAW_WORKSPACE"] == str(root)
    assert kwargs["env"]["DALTON_NO_DIRECT_SEND"] == "1"
    assert "bbg_remote_refresh.sh" not in blob


@pytest.mark.asyncio
async def test_scheduler_forwards_t34_manifest_argv_and_data_copy_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.data_jobs.scheduler as scheduler_mod

    seen: list[dict] = []

    async def _capture(**kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop-after-args")

    async def _started(conn: object, *, lock_conn: object, claimed: object) -> bool:
        del conn, lock_conn, claimed
        return True

    async def _lock(lock_conn: object) -> None:
        del lock_conn
        return None

    monkeypatch.setattr(
        scheduler_mod,
        "start_claimed_host_run",
        create_autospec(scheduler_mod.start_claimed_host_run, side_effect=_started),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "check_scheduler_lock",
        create_autospec(scheduler_mod.check_scheduler_lock, side_effect=_lock),
    )
    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.run_host_task.side_effect = _capture
    now = datetime(2026, 11, 12, 23, 20, tzinfo=timezone.utc)
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    entry = load_data_task_manifest().by_job_key()[JOB_KEY]
    drifted = [*KWEB_ARGV, "--allow-warnings"]
    claimed = ClaimedTaskRun(
        run_id=str(uuid4()),
        job_key=JOB_KEY,
        generation=3,
        runner="host",
        command_argv=drifted,
        workdir=OPENCLAW_CWD,
        timeout_seconds=900,
        no_output_timeout_seconds=600,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="run",
    )
    with pytest.raises(RuntimeError, match="stop-after-args"):
        await execute_claimed_host_run(
            object(),
            lock_conn=object(),
            host_client=host_client,
            claimed=claimed,
            path_map=path_map,
        )
    assert seen[-1]["command_argv"] == list(entry.command_argv) == KWEB_ARGV
    assert seen[-1]["cwd"] == DATA_COPY_CWD
    assert seen[-1]["cwd"] != OPENCLAW_CWD
    assert "--allow-warnings" not in seen[-1]["command_argv"]
    assert seen[-1]["host_mode"] == "run"
    assert seen[-1]["timeout"] == 900
    assert seen[-1]["no_output_timeout"] == 600


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_seed_deploys_kweb_data_assets_without_legacy_source_sync(tmp_path: Path) -> None:
    script_path = REPO_ROOT / "scripts" / "dalton" / "seed_openclaw_data.sh"
    script_text = script_path.read_text(encoding="utf-8")
    assert "data_assets" in script_text
    assert "--source" in script_text

    dst = tmp_path / "openclaw-data"
    dry = subprocess.run(
        ["bash", str(script_path), "--target", str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "Mode: DRY-RUN" in dry.stdout
    assert "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py" in dry.stdout
    assert not dst.exists()

    preflight_cfg = dst / "config" / "data_tasks.env"
    preflight_cfg.parent.mkdir(parents=True, exist_ok=True)
    preflight_cfg.write_text(
        "\n".join(
            [
                "DALTON_DATA_TASK_BBG_HOST=bbg@test.example",
                "DALTON_DATA_TASK_BBG_CSV_REMOTE=C:/test/bbg.csv",
                "DALTON_DATA_TASK_BBG_XLSX_REMOTE=C:/test/bbg.xlsx",
                "DALTON_DATA_TASK_BBG_REFRESH_CMD=cmd /c test.bat",
                "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID=oc_placeholder_tracking",
                "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID=oc_placeholder_semi",
                "DALTON_DATA_TASK_FEISHU_ASIA_TARGET=chat:oc_placeholder_asia",
                "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET=oc_placeholder_megacap",
                "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID=oc_placeholder_sentiment",
                "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET=user:ou_placeholder_ashare",
                "DALTON_DATA_TASK_DISCORD_DM_TARGET=user:100000000000000001",
                "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL=channel:100000000000000002",
                "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL=channel:100000000000000003",
                "DALTON_SEC_USER_AGENT=TestBot test@example.com",
                "DALTON_DATA_TASK_GS_PB_PYTHON=/opt/test/gs-pb/bin/python",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    apply = subprocess.run(
        ["bash", str(script_path), "--apply", "--target", str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply.returncode == 0, apply.stderr
    assert (dst / "skills/kweb-dashboard/scripts/kweb_dashboard_cli.py").is_file()
    assert (dst / "skills/kweb-dashboard/SKILL.md").is_file()


def test_none_payload_source_ignores_stdout_trigger_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``none`` does not turn stdout into an outbox. Other sources still do."""
    monkeypatch.setenv("DALTON_DATA_TASK_DISCORD_DEV_CHANNEL", "channel:100000000000000002")
    monkeypatch.setenv("DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL", "channel:100000000000000003")
    monkeypatch.setenv("DALTON_DATA_TASK_DISCORD_DM_TARGET", "user:100000000000000001")
    monkeypatch.setenv("DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET", "user:ou_placeholder_ashare")
    none_jobs = {
        "bbg-data-pull-asia",  # T1
        "cn-leverage-daily",  # T5
        "jp-leverage-daily",  # T7
        "kr-leverage-daily",  # T8
        "index-crowding-html-daily",  # T10
        "sentiment-dashboard-refresh",  # T25
        "tw-leverage-daily",  # T28
        "bbg-data-pull-us",  # T29
        "vol-surface-daily-snapshot",  # T42
        "gs-pb-weekly-rundown",  # T58
        "fde-hiring-snapshot-daily",  # T9
        "richard-relative-dashboard-refresh",  # T34
        "podcast-weekly-pipeline",  # T60
        "pm-market-state",  # T84
        "cn-intraday",  # T85
        "hk-momentum",  # T86
        "model-availability-watch",  # T87
    }
    assert {key for key, source in TASK_NOTIFY_PAYLOAD_SOURCES.items() if source == "none"} == none_jobs
    assert {
        key for key, source in _TASK_ID_PAYLOAD_SOURCE_ALIASES.items() if source == "none"
    } == {"T42", "T58", "T9", "T34", "T60", "T84", "T85", "T86", "T87"}
    batch4_jobs = {
        "e-watch-daily",
        "e-watch-intraday",
        "pt-watchlist-asia",
        "pt-watchlist-us",
        "china-2c-hk-monitor",
        "china-2c-us-monitor",
        "a-share-bottom-volume-watch",
    }
    assert batch4_jobs.isdisjoint(none_jobs)
    trigger = json.dumps(
        {
            "ok": True,
            "feishu_message": "do not send",
            "feishu_target": "oc_should_not_send",
            "report_text": "report",
            "report": {"body": "x"},
            "card": {"schema": "2.0"},
        },
        ensure_ascii=False,
    )
    for job_key in (*none_jobs, "T42", "T58", "T9", "T34"):
        outbox_path = tmp_path / "none" / job_key / "outbox.json"
        assert (
            persist_redacted_outbox(
                outbox_path=outbox_path,
                redactor=SecretRedactor(()),
                job_key=job_key,
                stdout_text=trigger,
            )
            is None
        )
        assert not outbox_path.exists()

    staging = tmp_path / "t5-staging.json"
    staging.write_text('{"msg":"from-staging"}\n', encoding="utf-8")
    staged_out = tmp_path / "t5-staged" / "outbox.json"
    staged = persist_redacted_outbox(
        outbox_path=staged_out,
        staging_outbox_path=staging,
        redactor=SecretRedactor(()),
        job_key="cn-leverage-daily",
        stdout_text=trigger,
    )
    assert staged == {"msg": "from-staging"}
    assert staged_out.is_file()
    assert not staging.exists()

    for job_key, source in TASK_NOTIFY_PAYLOAD_SOURCES.items():
        if source == "none" or job_key in {"semi-rescue-alert-am", "semi-rescue-alert-pm"}:
            continue
        assert resolve_task_payload_source(job_key) == source
        outbox_path = tmp_path / "keep" / job_key / "outbox.json"
        saved = persist_redacted_outbox(
            outbox_path=outbox_path,
            redactor=SecretRedactor(()),
            job_key=job_key,
            stdout_text=trigger,
        )
        assert saved is not None, job_key
        assert outbox_path.is_file()


def test_kweb_deploy_no_upload_updates_portfolio_without_uploading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import importlib.util
    import types

    cli_path = (
        REPO_ROOT
        / "scripts"
        / "dalton"
        / "data_assets"
        / "skills"
        / "kweb-dashboard"
        / "scripts"
        / "kweb_dashboard_cli.py"
    )
    assert cli_path.is_file()

    def _fake_workspace_root(_file: str | Path) -> Path:
        return tmp_path

    ws_mod = types.ModuleType("openclaw_workspace")
    ws_mod.workspace_root = create_autospec(_fake_workspace_root, return_value=tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openclaw_workspace", ws_mod)

    spec = importlib.util.spec_from_file_location("kweb_dashboard_cli_overlay_test", cli_path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    try:
        spec.loader.exec_module(cli)
    finally:
        sys.modules.pop(spec.name, None)

    out_html = tmp_path / "research-outputs" / "kweb-dashboard" / "index.html"
    monkeypatch.setattr(
        cli,
        "load_config",
        create_autospec(cli.load_config, return_value={"site_title": "Richard 相对看板"}),
    )
    build_spy = create_autospec(
        cli.build,
        return_value={
            "ok": True,
            "output": str(out_html),
            "bytes": 1234,
            "title": "Richard 相对看板",
            "warnings": [],
            "overlong_notes": [],
            "portfolio": {"ok": True},
            "etf_board": {"ok": True, "rows": 10, "errors": []},
        },
    )
    monkeypatch.setattr(cli, "build", build_spy)
    upload_spy = create_autospec(
        cli.upload_html,
        side_effect=AssertionError("upload_html must not be called with --no-upload"),
    )
    monkeypatch.setattr(cli, "upload_html", upload_spy)

    rc = cli.main(["deploy", "--no-upload", "--output", str(out_html), "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["uploaded"] is False
    assert payload["upload_skipped"] is True
    assert payload["upload"] is None
    assert payload["title"] == "Richard 相对看板"
    build_spy.assert_called_once_with(
        out_html,
        {"site_title": "Richard 相对看板"},
        update_portfolio=True,
    )
    upload_spy.assert_not_called()

    # Warnings still fail deploy without calling upload_html.
    build_spy.return_value = {
        "ok": True,
        "output": str(out_html),
        "bytes": 1234,
        "title": "Richard 相对看板",
        "warnings": ["missing series"],
        "overlong_notes": [],
    }
    warn_result = cli.deploy(out_html, no_upload=True)
    assert warn_result["ok"] is False
    assert warn_result["uploaded"] is False
    assert warn_result["upload_skipped"] is True
    upload_spy.assert_not_called()


def _load_kweb_cli_for_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import importlib.util
    import types

    cli_path = (
        REPO_ROOT
        / "scripts"
        / "dalton"
        / "data_assets"
        / "skills"
        / "kweb-dashboard"
        / "scripts"
        / "kweb_dashboard_cli.py"
    )
    ws_mod = types.ModuleType("openclaw_workspace")
    ws_mod.workspace_root = create_autospec(lambda _f: tmp_path, return_value=tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openclaw_workspace", ws_mod)

    spec = importlib.util.spec_from_file_location("kweb_dashboard_cli_overlay_test", cli_path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    try:
        spec.loader.exec_module(cli)
    finally:
        sys.modules.pop(spec.name, None)
    return cli


def test_kweb_etf_failure_renders_stale_banner_on_page_without_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import types

    cli = _load_kweb_cli_for_test(tmp_path, monkeypatch)
    out_html = tmp_path / "research-outputs" / "kweb-dashboard" / "index.html"
    outbox_file = tmp_path / "staging" / "outbox.json"
    monkeypatch.setenv("DALTON_OUTBOX_FILE", str(outbox_file))

    def _fake_analyse(section: dict[str, object]) -> dict[str, object]:
        return {
            "id": str(section["id"]),
            "label": str(section["label"]),
            "heading": str(section["heading"]),
            "results": [],
            "warnings": [],
            "horizon": 63,
            "horizon_label": "3个月",
            "rsi_period": 30,
            "window_start": "2023-01-01",
            "snapshot": None,
            "basket": None,
            "excluded_groups": [],
        }

    monkeypatch.setattr(cli, "analyse", create_autospec(cli.analyse, side_effect=_fake_analyse))
    monkeypatch.setattr(
        cli,
        "pair_section",
        create_autospec(cli.pair_section, return_value="<p>ok</p>"),
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        create_autospec(
            cli.load_config,
            return_value={
                "site_title": "Richard 相对看板",
                "site_subtitle": "",
                "sections": [
                    {
                        "id": "kweb",
                        "label": "KWEB",
                        "heading": "KWEB",
                        "horizon_days": 63,
                        "rsi_period": 30,
                    }
                ],
                "etf_board": {
                    "enabled": True,
                    "id": "etf",
                    "label": "ETF",
                    "heading": "ETF 看板",
                },
            },
        ),
    )

    fake_etf_board = types.ModuleType("kweb_etf_board")
    fake_etf_board.load_etf_config = create_autospec(  # type: ignore[attr-defined]
        lambda: {"etf": True},
        return_value={"etf": True},
    )
    fake_etf_board.analyse_board = create_autospec(  # type: ignore[attr-defined]
        lambda _cfg, _ws, _rsi: {},
        return_value={"board_last": "2026-11-16", "errors": []},
    )
    fake_etf_board.render_board = create_autospec(  # type: ignore[attr-defined]
        lambda _cfg, _data: "",
        return_value='<div class="stats"><div><b>40</b></div></div>',
    )
    fake_etf_board.summary = create_autospec(  # type: ignore[attr-defined]
        lambda data: data,
        side_effect=lambda data: {
            "ok": not data["errors"],
            "rows": 40,
            "board_last": data["board_last"],
            "errors": data["errors"],
        },
    )
    fake_etf_board.error_body = create_autospec(  # type: ignore[attr-defined]
        lambda msg: msg,
        side_effect=lambda msg: (
            f'<article class="panel"><h3>ETF 看板这次没生成</h3><p>{msg}</p></article>'
        ),
    )
    monkeypatch.setitem(sys.modules, "kweb_etf_board", fake_etf_board)

    # 1) First run succeeds: records last successful ETF date, no stale banner in page.
    rc_ok = cli.main(["deploy", "--no-upload", "--output", str(out_html), "--json"])
    assert rc_ok == 0
    ok_payload = json.loads(capsys.readouterr().out)
    assert ok_payload["ok"] is True
    assert not outbox_file.exists()
    html_ok = out_html.read_text(encoding="utf-8")
    assert ".stale-banner{" in html_ok
    assert '<p class="stale-banner">' not in html_ok
    last_ok_file = tmp_path / "state" / "kweb-dashboard" / "etf_last_success.json"
    assert json.loads(last_ok_file.read_text(encoding="utf-8"))["as_of"] == "2026-11-16"

    # 2) Partial ETF fetch failure: page builds & deploy succeeds, ETF section top shows stale banner.
    fake_etf_board.analyse_board = create_autospec(  # type: ignore[attr-defined]
        lambda _cfg, _ws, _rsi: {},
        return_value={
            "board_last": "2026-11-17",
            "errors": ["SOXX 没取到行情", "周期/防御缺失"],
        },
    )
    rc_partial = cli.main(["deploy", "--no-upload", "--output", str(out_html), "--json"])
    assert rc_partial == 0
    partial_payload = json.loads(capsys.readouterr().out)
    assert partial_payload["ok"] is True
    assert partial_payload["upload_skipped"] is True
    assert not outbox_file.exists()
    html_partial = out_html.read_text(encoding="utf-8")
    etf_block = html_partial.split('<main id="tab-etf"', 1)[1]
    assert '<p class="stale-banner">' in etf_block
    assert "ETF 数据未更新（上次成功：2026-11-16，原因：SOXX 没取到行情；周期/防御缺失）" in etf_block
    assert etf_block.index("stale-banner") < etf_block.index('class="stats"')

    # 3) Full ETF failure (exception) without prior state: falls back to "未知", still succeeds.
    last_ok_file.unlink()
    fake_etf_board.analyse_board = create_autospec(  # type: ignore[attr-defined]
        lambda _cfg, _ws, _rsi: {},
        side_effect=RuntimeError("yfinance timeout"),
    )
    rc_exc = cli.main(["deploy", "--no-upload", "--output", str(out_html), "--json"])
    assert rc_exc == 0
    exc_payload = json.loads(capsys.readouterr().out)
    assert exc_payload["ok"] is True
    assert not outbox_file.exists()
    html_exc = out_html.read_text(encoding="utf-8")
    etf_exc_block = html_exc.split('<main id="tab-etf"', 1)[1]
    assert "ETF 数据未更新（上次成功：未知，原因：RuntimeError: yfinance timeout）" in etf_exc_block
    assert etf_exc_block.index("stale-banner") < etf_exc_block.index("ETF 看板这次没生成")


def test_kweb_cos_upload_and_default_deploy_disabled_unless_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_kweb_cli_for_test(tmp_path, monkeypatch)
    out_html = tmp_path / "research-outputs" / "kweb-dashboard" / "index.html"
    monkeypatch.delenv("DALTON_ALLOW_COS_UPLOAD", raising=False)

    real_build = cli.build
    build_spy = create_autospec(
        real_build,
        side_effect=AssertionError("build must not run when default deploy is disabled"),
    )
    monkeypatch.setattr(cli, "build", build_spy)

    # 1) upload subcommand rejected immediately
    rc_upload = cli.main(["upload", "--file", str(out_html), "--json"])
    assert rc_upload == 1
    err_upload = json.loads(capsys.readouterr().out)
    assert err_upload["ok"] is False
    assert "已停用" in err_upload["error"]

    # 2) deploy without --no-upload rejected immediately before build
    rc_deploy = cli.main(["deploy", "--output", str(out_html), "--json"])
    assert rc_deploy == 1
    err_deploy = json.loads(capsys.readouterr().out)
    assert err_deploy["ok"] is False
    assert "已停用" in err_deploy["error"]
    build_spy.assert_not_called()

    # 3) Explicit DALTON_ALLOW_COS_UPLOAD=1 unlocks legacy upload path (tested with mock upload)
    monkeypatch.setenv("DALTON_ALLOW_COS_UPLOAD", "1")
    monkeypatch.setattr(
        cli,
        "load_config",
        create_autospec(cli.load_config, return_value={"site_title": "Richard 相对看板"}),
    )
    monkeypatch.setattr(
        cli,
        "build",
        create_autospec(
            real_build,
            return_value={
                "ok": True,
                "output": str(out_html),
                "bytes": 100,
                "warnings": [],
                "overlong_notes": [],
            },
        ),
    )
    monkeypatch.setattr(
        cli,
        "upload_html",
        create_autospec(cli.upload_html, return_value={"ok": True, "key": "kweb.html"}),
    )
    rc_opt_in = cli.main(["deploy", "--output", str(out_html), "--json"])
    assert rc_opt_in == 0
    assert json.loads(capsys.readouterr().out)["uploaded"] is True


@pytest.mark.asyncio
async def test_scheduler_host_run_does_not_inject_etf_outbox_when_notify_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.dalton.data_jobs.scheduler as scheduler_mod
    from src.dalton.data_jobs.host_runner import HostRunOutcome
    from tests.unit.dalton.test_data_jobs import _make_autospec_db

    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.run_host_task.return_value = HostRunOutcome(
        ok=True,
        status_code=200,
        exit_code=0,
        exit_confirmed=True,
        log_tail=json.dumps(
            {"ok": True, "etf_error": "ETF 看板有取数问题：SOXX 缺失"},
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "start_claimed_host_run",
        create_autospec(scheduler_mod.start_claimed_host_run, return_value=True),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "check_scheduler_lock",
        create_autospec(scheduler_mod.check_scheduler_lock, return_value=None),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "freeze_task_run_artifacts",
        create_autospec(scheduler_mod.freeze_task_run_artifacts, return_value=None),
    )

    import src.dalton.monitors.service as monitor_service_mod

    monkeypatch.setattr(
        monitor_service_mod,
        "maybe_publish_data_job_monitor",
        create_autospec(monitor_service_mod.maybe_publish_data_job_monitor, return_value=None),
    )

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(6)])
    conn, state = _make_autospec_db(
        [
            {"rowcount": 1},  # finalize UPDATE dalton_data_task_runs
            {"rowcount": 1, "fetchone": {"consecutive_failures": 0}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot idle
        ]
    )
    now = datetime(2026, 11, 17, 23, 20, tzinfo=timezone.utc)
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    claimed = ClaimedTaskRun(
        run_id=str(uuid4()),
        job_key=JOB_KEY,
        generation=1,
        runner="host",
        command_argv=list(KWEB_ARGV),
        workdir=DATA_COPY_CWD,
        timeout_seconds=900,
        no_output_timeout_seconds=600,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="America/New_York",
        host_mode="run",
    )

    committed, cf = await execute_claimed_host_run(
        conn,
        lock_conn=lock_conn,
        host_client=host_client,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed is True
    assert cf == 0
    finalize_params = state["executed"][0][1]
    assert finalize_params["terminal_status"] == "succeeded"
    assert finalize_params["outbox_payload"] is None


@pytest.mark.asyncio
async def test_scheduler_host_run_reads_bridge_outbox_and_queues_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host run's outbox reaches notify like a container run's (weekly-token-usage, 2026-11-21)."""
    import src.dalton.data_jobs.scheduler as scheduler_mod
    from src.dalton.data_jobs.host_runner import HostRunOutcome
    from tests.unit.dalton.test_data_jobs import _make_autospec_db

    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.run_host_task.return_value = HostRunOutcome(
        ok=True,
        status_code=200,
        exit_code=0,
        exit_confirmed=True,
        log_tail=json.dumps({"ok": True}),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "start_claimed_host_run",
        create_autospec(scheduler_mod.start_claimed_host_run, return_value=True),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "check_scheduler_lock",
        create_autospec(scheduler_mod.check_scheduler_lock, return_value=None),
    )
    monkeypatch.setattr(
        scheduler_mod,
        "freeze_task_run_artifacts",
        create_autospec(scheduler_mod.freeze_task_run_artifacts, return_value=None),
    )

    import src.dalton.monitors.service as monitor_service_mod

    monkeypatch.setattr(
        monitor_service_mod,
        "maybe_publish_data_job_monitor",
        create_autospec(monitor_service_mod.maybe_publish_data_job_monitor, return_value=None),
    )

    lock_conn, _ = _make_autospec_db([{"fetchone": (1,)} for _ in range(6)])
    conn, state = _make_autospec_db(
        [
            {"rowcount": 1},  # finalize UPDATE dalton_data_task_runs
            {"rowcount": 1, "fetchone": {"consecutive_failures": 0}},  # UPDATE defs
            {"rowcount": 1},  # UPDATE slot idle
        ]
    )
    now = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    run_id = str(uuid4())
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "Weekly token usage"}},
        "body": {"elements": [{"tag": "markdown", "content": "周度 token 用量"}]},
    }
    run_dir = path_map.backend_run_artifacts_dir(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "outbox.json").write_text(
        json.dumps({"feishu_cards": [{"card": card, "images": []}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    claimed = ClaimedTaskRun(
        run_id=run_id,
        job_key="weekly-token-usage",
        generation=1,
        runner="host",
        command_argv=["bash", "skills/model-usage-cost/scripts/run.sh"],
        workdir=DATA_COPY_CWD,
        timeout_seconds=900,
        no_output_timeout_seconds=600,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=True,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(minutes=15),
        timezone="Asia/Hong_Kong",
        host_mode="run",
    )

    committed, _ = await execute_claimed_host_run(
        conn,
        lock_conn=lock_conn,
        host_client=host_client,
        claimed=claimed,
        path_map=path_map,
    )
    assert committed is True
    finalize_params = state["executed"][0][1]
    assert finalize_params.get("failure_reason") is None, finalize_params.get("failure_reason")
    assert finalize_params["terminal_status"] == "succeeded"
    assert finalize_params["outbox_payload"] is not None
    assert "周度 token 用量" in str(finalize_params["outbox_payload"])
    assert finalize_params["notify_status"] == "pending_dispatch"


def test_kweb_overlay_skill_md_recommends_deploy_no_upload() -> None:
    skill_md = (
        REPO_ROOT
        / "scripts"
        / "dalton"
        / "data_assets"
        / "skills"
        / "kweb-dashboard"
        / "SKILL.md"
    )
    assert skill_md.is_file()
    text = skill_md.read_text(encoding="utf-8")
    assert "deploy --no-upload" in text
    assert "已停用" in text
    assert "relative-strength" in text
    assert "ETF 数据未更新" in text
