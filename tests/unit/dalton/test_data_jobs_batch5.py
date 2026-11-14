"""OC-1 batch 5: T34 kweb relative dashboard on the host data-task runner.

Expected schedule is hardcoded from the OpenClaw cron export. This file does
not read ``.agy/``. Podcast jobs stay on OpenClaw and have no manifest here.
T34 has no overlay entry; the host command is the skill script itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
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
    "--json",
]
KWEB_COMMAND = "bash skills/kweb-dashboard/scripts/run.sh deploy --json"
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
    assert len(manifest.tasks) == 37
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
        assert body == {"ok": False, "rejected": True, "error": "host_command_rejected"}
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


def test_seed_copies_kweb_code_and_fills_missing_state_only(tmp_path: Path) -> None:
    script_path = REPO_ROOT / "scripts" / "dalton" / "seed_openclaw_data.sh"
    script_text = script_path.read_text(encoding="utf-8")
    assert "skills/kweb-dashboard" in script_text
    assert "state/kweb-dashboard/portfolio_state.json" in script_text
    assert "state/kweb-dashboard/portfolio_ledger.jsonl" in script_text
    assert "state/kweb-dashboard/etf_aum.json" in script_text
    assert "research-outputs/kweb-dashboard/portfolio/*.md" in script_text
    assert "podcast-weekly" not in script_text

    src = tmp_path / "workspace"
    dst = tmp_path / "openclaw-data"
    # rsync treats a file as unchanged when the size and the whole-second
    # mtime both match. "echo old" and "echo new" are the same length, so a
    # second --apply in the same second would leave the first copy in place.
    _write(src / "skills/kweb-dashboard/scripts/run.sh", "echo old-kweb\n")
    _write(
        src / "skills/kweb-dashboard/references/kweb_config.json",
        '{"site_title":"Richard 相对看板"}\n',
    )
    _write(src / "skills/kweb-dashboard/.venv/pyvenv.cfg", "home = /hidden\n")
    _write(src / "state/kweb-dashboard/portfolio_state.json", '{"cash": 1}\n')
    _write(src / "state/kweb-dashboard/portfolio_ledger.jsonl", '{"signal_date":"2026-11-11"}\n')
    _write(src / "state/kweb-dashboard/etf_aum.json", '{"as_of":"2026-11-01"}\n')
    _write(src / "state/kweb-dashboard/cos_secret.json", "secret-id\n")
    _write(src / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md", "old report\n")
    _write(src / "research-outputs/kweb-dashboard/index.html", "<html>generated</html>\n")
    _write(src / "config/kweb_cos_secret.json", '{"secret":"no"}\n')
    _write(src / "skills/podcast-weekly-digest/scripts/run.sh", "echo podcast\n")

    dry = subprocess.run(
        ["bash", str(script_path), "--source", str(src), "--target", str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "Mode: DRY-RUN" in dry.stdout
    assert "DRY-RUN total size" in dry.stdout
    assert not dst.exists()

    apply = subprocess.run(
        ["bash", str(script_path), "--apply", "--source", str(src), "--target", str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply.returncode == 0, apply.stderr
    assert (dst / "skills/kweb-dashboard/scripts/run.sh").read_text(encoding="utf-8") == "echo old-kweb\n"
    assert "Richard 相对看板" in (
        dst / "skills/kweb-dashboard/references/kweb_config.json"
    ).read_text(encoding="utf-8")
    assert (dst / "state/kweb-dashboard/portfolio_state.json").read_text(encoding="utf-8") == '{"cash": 1}\n'
    assert (dst / "state/kweb-dashboard/portfolio_ledger.jsonl").is_file()
    assert (dst / "state/kweb-dashboard/etf_aum.json").is_file()
    assert (dst / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md").read_text(
        encoding="utf-8"
    ) == "old report\n"
    assert not (dst / "skills/kweb-dashboard/.venv").exists()
    assert not (dst / "state/kweb-dashboard/cos_secret.json").exists()
    assert not (dst / "research-outputs/kweb-dashboard/index.html").exists()
    assert not (dst / "config/kweb_cos_secret.json").exists()
    assert "echo podcast" not in (dst / "skills/podcast-weekly-digest/scripts/run.sh").read_text(encoding="utf-8")

    (dst / "state/kweb-dashboard/portfolio_state.json").write_text("KEEP-STATE\n", encoding="utf-8")
    (dst / "state/kweb-dashboard/etf_aum.json").write_text("KEEP-AUM\n", encoding="utf-8")
    (dst / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md").write_text(
        "KEEP-MD\n", encoding="utf-8"
    )
    (dst / "state/kweb-dashboard/portfolio_ledger.jsonl").unlink()
    _write(src / "skills/kweb-dashboard/scripts/run.sh", "echo new-kweb-v2\n")
    _write(src / "skills/kweb-dashboard/references/kweb_config.json", '{"site_title":"updated"}\n')
    _write(src / "state/kweb-dashboard/portfolio_state.json", "NEW-STATE\n")
    _write(src / "state/kweb-dashboard/etf_aum.json", "NEW-AUM\n")
    _write(src / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md", "NEW-MD\n")
    _write(src / "state/kweb-dashboard/portfolio_ledger.jsonl", '{"signal_date":"2026-11-12"}\n')

    again = subprocess.run(
        ["bash", str(script_path), "--apply", "--source", str(src), "--target", str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    assert (dst / "skills/kweb-dashboard/scripts/run.sh").read_text(encoding="utf-8") == "echo new-kweb-v2\n"
    assert (
        dst / "skills/kweb-dashboard/references/kweb_config.json"
    ).read_text(encoding="utf-8") == '{"site_title":"updated"}\n'
    assert (dst / "state/kweb-dashboard/portfolio_state.json").read_text(encoding="utf-8") == "KEEP-STATE\n"
    assert (dst / "state/kweb-dashboard/etf_aum.json").read_text(encoding="utf-8") == "KEEP-AUM\n"
    assert (dst / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md").read_text(
        encoding="utf-8"
    ) == "KEEP-MD\n"
    assert (dst / "state/kweb-dashboard/portfolio_ledger.jsonl").read_text(
        encoding="utf-8"
    ) == '{"signal_date":"2026-11-12"}\n'
    assert not (dst / "skills/kweb-dashboard/.venv").exists()
    assert not (dst / "config/kweb_cos_secret.json").exists()
    assert not (dst / "research-outputs/kweb-dashboard/index.html").exists()


_CREDENTIAL_NAMES = (
    ".env",
    "prod.env",
    ".env.local",
    "kweb_cos_secret.json",
    "cos_secret.json",
    "api_credential.json",
    "server.pem",
    "private.key",
    "client.p12",
    "id_rsa",
    "id_rsa.pub",
    "session_token.json",
    "token.json",
)


def test_seed_apply_skips_credential_files_and_keeps_code(tmp_path: Path) -> None:
    """Credential names stay out of every seed channel. Code and config still copy."""
    script_path = REPO_ROOT / "scripts" / "dalton" / "seed_openclaw_data.sh"
    src = tmp_path / "workspace"
    dst = tmp_path / "openclaw-data"
    _write(src / "skills/kweb-dashboard/scripts/run.sh", "echo keep-code\n")
    _write(src / "skills/kweb-dashboard/references/kweb_config.json", '{"site_title":"ok"}\n')
    _write(src / "skills/ai-chain/scripts/daily_run.py", "print('chain')\n")
    _write(src / "config/etf_characteristics.json", '{"ok": true}\n')
    _write(src / "scripts/megacap_relative_signal_monitor.py", "print('ok')\n")
    _write(src / "state/cn-leverage/margin_core.csv", "date,val\n")
    _write(src / "state/cn-leverage/my_secret.csv", "blocked\n")
    _write(src / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md", "report\n")
    _write(src / "research-outputs/kweb-dashboard/portfolio/secret-note.md", "blocked\n")
    _write(src / "state/a-share-volume-watch/stderr.log", "log\n")
    _write(src / "skills/ai-chain/state/ingest_health.json", '{"ok": 1}\n')
    for name in _CREDENTIAL_NAMES:
        _write(src / "skills/kweb-dashboard" / name, "blocked\n")
        _write(src / "skills/kweb-dashboard/scripts" / name, "blocked\n")
        _write(src / "state/a-share-volume-watch" / name, "blocked\n")
        _write(src / "skills/ai-chain/state" / name, "blocked\n")

    apply = subprocess.run(
        ["bash", str(script_path), "--apply", "--source", str(src), "--target", str(dst)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply.returncode == 0, apply.stderr
    assert (dst / "skills/kweb-dashboard/scripts/run.sh").read_text(encoding="utf-8") == "echo keep-code\n"
    assert (dst / "skills/kweb-dashboard/references/kweb_config.json").is_file()
    assert (dst / "skills/ai-chain/scripts/daily_run.py").read_text(encoding="utf-8") == "print('chain')\n"
    assert (dst / "config/etf_characteristics.json").read_text(encoding="utf-8") == '{"ok": true}\n'
    assert (dst / "scripts/megacap_relative_signal_monitor.py").is_file()
    assert (dst / "state/cn-leverage/margin_core.csv").read_text(encoding="utf-8") == "date,val\n"
    assert (dst / "research-outputs/kweb-dashboard/portfolio/2026-11-11.md").read_text(
        encoding="utf-8"
    ) == "report\n"
    assert (dst / "state/a-share-volume-watch/stderr.log").read_text(encoding="utf-8") == "log\n"
    assert (dst / "skills/ai-chain/state/ingest_health.json").read_text(encoding="utf-8") == '{"ok": 1}\n'
    assert not (dst / "state/cn-leverage/my_secret.csv").exists()
    assert not (dst / "research-outputs/kweb-dashboard/portfolio/secret-note.md").exists()
    for name in _CREDENTIAL_NAMES:
        assert not (dst / "skills/kweb-dashboard" / name).exists()
        assert not (dst / "skills/kweb-dashboard/scripts" / name).exists()
        assert not (dst / "state/a-share-volume-watch" / name).exists()
        assert not (dst / "skills/ai-chain/state" / name).exists()


def test_none_payload_source_ignores_stdout_trigger_fields(tmp_path: Path) -> None:
    """``none`` does not turn stdout into an outbox. Other sources still do."""
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
    }
    assert {key for key, source in TASK_NOTIFY_PAYLOAD_SOURCES.items() if source == "none"} == none_jobs
    assert {
        key for key, source in _TASK_ID_PAYLOAD_SOURCE_ALIASES.items() if source == "none"
    } == {"T42", "T58", "T9", "T34", "T60"}
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
