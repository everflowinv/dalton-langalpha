"""Offline deterministic preview tests for OC-1 T26 (`us-topbottom-dm-v23`)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.data_jobs.container_runner import build_path_map, read_run_artifacts
from src.dalton.data_jobs.evaluators import TaskEvaluationContext, evaluate_task_run
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.data_jobs.runner_wrapper import (
    TASK_NOTIFY_PAYLOAD_SOURCES,
    run_wrapped_command,
)
from src.dalton.data_jobs.scheduler import (
    _resolve_run_notify_payload,
    resolve_notify_status,
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
SEED_SCRIPT = REPO_ROOT / "scripts" / "dalton" / "seed_openclaw_data.sh"
T26_JOB_KEY = "us-topbottom-dm-v23"
EXPECTED_DISCORD_TARGET = "user:1134818575211839498"
SKILL_CLI_REL_PATHS: dict[str, str] = {
    "us-bottom-monitor": "skills/us-bottom-monitor/scripts/us_bottom_monitor_cli.py",
    "us-top-monitor": "skills/us-top-monitor/scripts/us_top_monitor_cli.py",
}

FORBIDDEN_DELIVERY_KEYS = frozenset(
    {
        "messageId",
        "message_id",
        "platformMessageIds",
        "primaryPlatformMessageId",
        "sentAt",
        "sent_at",
        "receipt",
        "receipts",
        "inflight",
    }
)


def _assert_no_forbidden_delivery_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        overlap = FORBIDDEN_DELIVERY_KEYS.intersection(obj.keys())
        assert not overlap, f"Unexpected delivery/receipt keys in preview payload: {overlap}"
        for value in obj.values():
            _assert_no_forbidden_delivery_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_forbidden_delivery_keys(item)


def _assert_no_temp_files(*roots: Path) -> None:
    leftovers: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for entry in root.rglob("*"):
            if entry.name.startswith(".tmp.") or entry.name.endswith(".tmp"):
                leftovers.append(str(entry))
    assert leftovers == [], f"Leftover temporary files found: {leftovers}"


def _install_trap_openclaw(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "openclaw_invoked.marker"
    trap_bin = fake_bin / "openclaw"
    trap_bin.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {str(marker)!r}\nexit 99\n",
        encoding="utf-8",
    )
    trap_bin.chmod(0o755)

    pip_marker = tmp_path / "pip_invoked.marker"
    for pip_name in ("pip", "pip3"):
        trap_pip = fake_bin / pip_name
        trap_pip.write_text(
            f"#!/usr/bin/env bash\nprintf '%s:%s\\n' {pip_name!r} \"$*\" >> {str(pip_marker)!r}\nexit 98\n",
            encoding="utf-8",
        )
        trap_pip.chmod(0o755)
    return fake_bin, trap_bin, marker


def _assert_no_bootstrap_or_openclaw(
    tmp_path: Path,
    ws: Path,
    *,
    expect_no_skill_venv: bool = True,
) -> None:
    openclaw_marker = tmp_path / "openclaw_invoked.marker"
    pip_marker = tmp_path / "pip_invoked.marker"
    bootstrap_marker = tmp_path / "bootstrap_invoked.marker"
    assert not openclaw_marker.exists(), f"openclaw invoked: {openclaw_marker.read_text()}"
    assert not pip_marker.exists(), f"pip bootstrap invoked: {pip_marker.read_text()}"
    assert not bootstrap_marker.exists(), f"run.sh bootstrap invoked: {bootstrap_marker.read_text()}"
    if expect_no_skill_venv:
        for skill in SKILL_CLI_REL_PATHS:
            assert not (ws / "skills" / skill / "venv").exists()
            assert not (ws / "skills" / skill / ".venv").exists()


def _build_clean_base_env(fake_bin: Path) -> dict[str, str]:
    stripped_keys = {
        "PYTEST_CURRENT_TEST",
        "DALTON_NO_DIRECT_SEND",
        "DALTON_NOTIFY_DRY_RUN",
        "DALTON_OUTBOX_FILE",
        "DALTON_RUN_ID",
        "DALTON_RUN_DIR",
        "DALTON_JOB_KEY",
        "DALTON_PYTHON_BIN",
        "OPENCLAW_WORKSPACE",
        "WORKDIR",
    }
    env = {k: v for k, v in os.environ.items() if k not in stripped_keys}
    orig_path = env.get("PATH", "/usr/bin:/bin")
    env["PATH"] = f"{fake_bin}{os.pathsep}{orig_path}"
    return env


def _setup_workspace(ws: Path) -> Path:
    scripts_dir = ws / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    installed = scripts_dir / "cron_command_tasks.py"
    shutil.copy2(OVERLAY_SCRIPT, installed)
    installed.chmod(0o755)
    return installed


def _write_collector_fixture(
    ws: Path,
    skill: str,
    *,
    spec_file: Path,
    invocation_log: Path,
    bootstrap_marker: Path | None = None,
) -> Path:
    cli_rel = SKILL_CLI_REL_PATHS[skill]
    cli_py = ws / cli_rel
    cli_py.parent.mkdir(parents=True, exist_ok=True)

    # Install a trap run.sh that records and fails if any caller triggers legacy venv/pip bootstrap.
    trap_marker = bootstrap_marker or (spec_file.parent.parent / "bootstrap_invoked.marker")
    run_sh = ws / "skills" / skill / "run.sh"
    run_sh.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s:run.sh:%s\\n' {skill!r} \"$*\" >> {str(trap_marker)!r}\n"
        "echo 'ERROR: legacy run.sh bootstrap must not be called in Dalton mode' >&2\n"
        "exit 97\n",
        encoding="utf-8",
    )
    run_sh.chmod(0o755)

    cli_py.write_text(
        "import base64, json, os, pathlib, sys\n"
        f"spec = json.loads(pathlib.Path({str(spec_file)!r}).read_text(encoding='utf-8'))\n"
        f"log_path = pathlib.Path({str(invocation_log)!r})\n"
        f"skill_name = {skill!r}\n"
        f"cli_rel = {cli_rel!r}\n"
        f"expected_ws = pathlib.Path({str(ws)!r}).resolve()\n"
        "args = sys.argv[1:]\n"
        "if pathlib.Path.cwd().resolve() != expected_ws:\n"
        "    print(f'unexpected cwd: {pathlib.Path.cwd()} != {expected_ws}', file=sys.stderr)\n"
        "    raise SystemExit(65)\n"
        "with log_path.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(f'{skill_name}:{cli_rel}:{\" \".join(args)}\\n')\n"
        "if args != ['--json', 'run']:\n"
        "    print(f'unexpected args: {args}', file=sys.stderr)\n"
        "    raise SystemExit(64)\n"
        "secret_env_name = spec.get('echo_env_var')\n"
        "secret_suffix = ''\n"
        "if secret_env_name:\n"
        "    secret_suffix = os.environ.get(secret_env_name, '')\n"
        "if spec.get('progress_line'):\n"
        "    print(spec['progress_line'] + secret_suffix, flush=True)\n"
        "if spec.get('stderr_text'):\n"
        "    print(spec['stderr_text'], file=sys.stderr, flush=True)\n"
        "chart_write_rel = spec.get('write_chart_rel')\n"
        "if chart_write_rel:\n"
        "    cp = pathlib.Path.cwd() / chart_write_rel\n"
        "    cp.parent.mkdir(parents=True, exist_ok=True)\n"
        "    cp.write_bytes(base64.b64decode(spec['chart_bytes_b64']))\n"
        "if spec.get('raw_stdout') is not None:\n"
        "    sys.stdout.write(spec['raw_stdout'])\n"
        "else:\n"
        "    payload = dict(spec['payload'])\n"
        "    if secret_suffix and isinstance(payload.get('feishu_message'), str):\n"
        "        payload['feishu_message'] = payload['feishu_message'] + secret_suffix\n"
        "    print(json.dumps(payload, ensure_ascii=False, indent=2))\n"
        "raise SystemExit(int(spec.get('exit_code', 0)))\n",
        encoding="utf-8",
    )
    cli_py.chmod(0o755)
    return cli_py


def _write_spec(
    spec_file: Path,
    *,
    feishu_message: str = "",
    chart: str | None = None,
    write_chart_rel: str | None = None,
    chart_bytes: bytes = b"\x89PNG\r\n\x1a\nt26-chart",
    ok: bool = True,
    error: str | None = None,
    exit_code: int = 0,
    raw_stdout: str | None = None,
    stderr_text: str = "",
    progress_line: str = "",
    echo_env_var: str | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"ok": ok}
    if feishu_message != "":
        payload["feishu_message"] = feishu_message
    if chart is not None:
        payload["chart"] = chart
    if error is not None:
        payload["error"] = error
    if extra_payload:
        payload.update(extra_payload)

    spec_data = {
        "payload": payload,
        "write_chart_rel": write_chart_rel,
        "chart_bytes_b64": base64.b64encode(chart_bytes).decode("ascii"),
        "exit_code": exit_code,
        "raw_stdout": raw_stdout,
        "stderr_text": stderr_text,
        "progress_line": progress_line,
        "echo_env_var": echo_env_var,
    }
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(json.dumps(spec_data, ensure_ascii=False), encoding="utf-8")


def _load_overlay_module(module_name: str = "t26_cron_command_tasks_overlay") -> Any:
    spec = importlib.util.spec_from_file_location(module_name, OVERLAY_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_t26_manifest_and_seed_overlay_contract(tmp_path: Path) -> None:
    manifest = load_data_task_manifest()
    entry = manifest.by_job_key()[T26_JOB_KEY]
    assert entry.task_code == "T26"
    assert entry.runner == "container"
    assert entry.enabled is False
    assert entry.notify_enabled is False
    assert entry.secret_names == []
    assert entry.command == "python3 scripts/cron_command_tasks.py us-topbottom-dm-v23"
    assert entry.command_argv == [
        "python3",
        "scripts/cron_command_tasks.py",
        "us-topbottom-dm-v23",
    ]
    assert TASK_NOTIFY_PAYLOAD_SOURCES[T26_JOB_KEY] == "outbox_file"
    assert OVERLAY_SCRIPT.is_file()

    # Verify seed_openclaw_data.sh installs the cron_command_tasks.py overlay without mutating host source.
    src_ws = tmp_path / "host_openclaw_ws"
    dst_data = tmp_path / "dalton_openclaw_data"
    (src_ws / "scripts").mkdir(parents=True)
    legacy_bytes = b"#!/usr/bin/env python3\n# host script placeholder\n"
    (src_ws / "scripts" / "cron_command_tasks.py").write_bytes(legacy_bytes)

    apply_res = subprocess.run(
        [
            "bash",
            str(SEED_SCRIPT),
            "--apply",
            "--source",
            str(src_ws),
            "--target",
            str(dst_data),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply_res.returncode == 0, apply_res.stderr
    assert (
        dst_data / "scripts" / "cron_command_tasks.py"
    ).read_bytes() == OVERLAY_SCRIPT.read_bytes()
    assert (src_ws / "scripts" / "cron_command_tasks.py").read_bytes() == legacy_bytes


@pytest.mark.asyncio
async def test_t26_preview_runs_both_collectors_preserves_order_target_charts_and_never_invokes_openclaw(
    tmp_path: Path,
) -> None:
    var_dir = tmp_path / "var" / "dalton"
    path_map = build_path_map(host_var_dir=var_dir, backend_var_dir=var_dir)
    ws = path_map.backend_data_root
    _setup_workspace(ws)

    fake_bin, trap_openclaw, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"

    _write_collector_fixture(
        ws, "us-bottom-monitor", spec_file=bottom_spec, invocation_log=invocation_log
    )
    _write_collector_fixture(
        ws, "us-top-monitor", spec_file=top_spec, invocation_log=invocation_log
    )

    bottom_text = "【美股底部监控 v2.3】\n综合得分：18/100（正常区间）\nSPX 200D 偏离：+6.4%"
    top_text = "【美股顶部监控 v2.3】\n综合得分：62/100（观察区间）\nPut/Call 5D：0.81"
    bottom_png = b"\x89PNG\r\n\x1a\nBOTTOM-CHART-BYTES-V1"
    top_png = b"\x89PNG\r\n\x1a\nTOP-CHART-BYTES-V1"

    # Bottom emits an absolute workspace chart path; top emits a workspace-relative chart path.
    _write_spec(
        bottom_spec,
        feishu_message=bottom_text,
        chart=str(ws / "temp" / "us-bottom-monitor" / "bottom_latest.png"),
        write_chart_rel="temp/us-bottom-monitor/bottom_latest.png",
        chart_bytes=bottom_png,
        progress_line="[us-bottom-monitor] computing indicators...",
    )
    _write_spec(
        top_spec,
        feishu_message=top_text,
        chart="temp/us-top-monitor/top_latest.png",
        write_chart_rel="temp/us-top-monitor/top_latest.png",
        chart_bytes=top_png,
        progress_line="[us-top-monitor] computing indicators...",
    )

    empty_secrets = tmp_path / "empty_secrets"
    empty_secrets.mkdir()

    # Exercise both notify_dry_run=True (DALTON_NOTIFY_DRY_RUN=1) and notify_dry_run=False (DALTON_NOTIFY_DRY_RUN=0).
    for idx, dry_run_flag in enumerate((True, False)):
        invocation_log.unlink(missing_ok=True)
        run_id = f"run-t26-ok-{idx}"
        outbox_tmp = tmp_path / f"tmpfs_{idx}"
        outbox_tmp.mkdir(parents=True, exist_ok=True)

        res = await run_wrapped_command(
            [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
            run_id=run_id,
            job_key=T26_JOB_KEY,
            workdir=str(ws),
            workspace_dir=str(ws),
            secrets_dir=empty_secrets,
            outbox_tmp_dir=outbox_tmp,
            notify_dry_run=dry_run_flag,
            timeout_seconds=30,
            no_output_timeout_seconds=30,
            base_env=base_env,
            stdout_sink=io.StringIO(),
            stderr_sink=io.StringIO(),
        )

        # Trap openclaw and pip binaries were first resolvable on child PATH, yet never called;
        # trap run.sh exists in both skills, neither skill has venv/.venv, and bootstrap never ran.
        assert shutil.which("openclaw", path=res.env_exported["PATH"]) == str(trap_openclaw)
        assert shutil.which("pip", path=res.env_exported["PATH"]) == str(fake_bin / "pip")
        assert res.env_exported["DALTON_NO_DIRECT_SEND"] == "1"
        assert res.env_exported["DALTON_NOTIFY_DRY_RUN"] == ("1" if dry_run_flag else "0")
        _assert_no_bootstrap_or_openclaw(tmp_path, ws)

        assert res.exit_code == 0, res.redacted_stderr
        assert res.redacted_stdout.strip() == "NO_REPLY"
        assert not (outbox_tmp / "outbox.json").exists()

        # Verify exact invocation order and direct Python CLI paths: bottom first, top second.
        calls = invocation_log.read_text(encoding="utf-8").splitlines()
        assert calls == [
            f"us-bottom-monitor:{SKILL_CLI_REL_PATHS['us-bottom-monitor']}:--json run",
            f"us-top-monitor:{SKILL_CLI_REL_PATHS['us-top-monitor']}:--json run",
        ]

        # Verify scheduler artifact reading, evaluator, notify_status, and _resolve_run_notify_payload.
        disk_outbox, _ = read_run_artifacts(run_id, path_map=path_map)
        assert disk_outbox is not None
        assert disk_outbox == res.outbox_payload
        _assert_no_forbidden_delivery_keys(disk_outbox)

        evaluation = evaluate_task_run(
            TaskEvaluationContext(
                job_key=T26_JOB_KEY,
                exit_code=res.exit_code,
                stdout=res.redacted_stdout,
                data_root=ws,
            )
        )
        assert evaluation.ok is True
        assert evaluation.status == "succeeded"
        assert (
            resolve_notify_status(
                terminal_status=evaluation.status,
                notify_enabled=False,
                has_outbox_payload=True,
                is_trial=True,
            )
            == "preview_only"
        )
        assert (
            resolve_notify_status(
                terminal_status=evaluation.status,
                notify_enabled=False,
                has_outbox_payload=True,
                is_trial=False,
            )
            == "preview_only"
        )

        resolved_text, resolved_card, resolved_payload = _resolve_run_notify_payload(
            run_id,
            T26_JOB_KEY,
            path_map=path_map,
        )
        assert resolved_card is None
        assert "_outbox_missing" not in resolved_payload
        assert resolved_payload["route_key"] == f"data:{T26_JOB_KEY}"
        assert resolved_text == f"{bottom_text}\n\n{top_text}"

        # Assert structured outbox fields, order, targets, preview flags, and per-run chart copies.
        assert disk_outbox["task_code"] == "T26"
        assert disk_outbox["job_key"] == T26_JOB_KEY
        assert disk_outbox["run_id"] == run_id
        assert disk_outbox["mode"] == "preview"
        assert disk_outbox["ok"] is True
        assert disk_outbox["delivered"] is False
        assert disk_outbox["delivery_status"] == "preview_only"
        assert disk_outbox["channel"] == "discord"
        assert disk_outbox["target"] == EXPECTED_DISCORD_TARGET
        assert disk_outbox["error"] == ""

        items = disk_outbox["items"]
        assert len(items) == 2
        assert [it["skill"] for it in items] == ["us-bottom-monitor", "us-top-monitor"]
        assert [it["index"] for it in items] == [0, 1]
        assert [it["channel"] for it in items] == ["discord", "discord"]
        assert [it["target"] for it in items] == [
            EXPECTED_DISCORD_TARGET,
            EXPECTED_DISCORD_TARGET,
        ]
        assert [it["text"] for it in items] == [bottom_text, top_text]
        assert [it["delivered"] for it in items] == [False, False]

        run_dir = path_map.backend_run_artifacts_dir(run_id)
        retained_0 = run_dir / items[0]["media"]
        retained_1 = run_dir / items[1]["media"]
        assert items[0]["media"] == "attachments/0.png"
        assert items[1]["media"] == "attachments/1.png"
        assert retained_0.is_file() and retained_0.read_bytes() == bottom_png
        assert retained_1.is_file() and retained_1.read_bytes() == top_png
        assert items[0]["chart_bytes"] == len(bottom_png)
        assert items[0]["chart_sha256"] == hashlib.sha256(bottom_png).hexdigest()
        assert items[1]["chart_bytes"] == len(top_png)
        assert items[1]["chart_sha256"] == hashlib.sha256(top_png).hexdigest()

        # Verify preserved last-run.json with plan.error and plan.items[*].text/media.
        last_run_path = ws / "state" / "cron-command" / T26_JOB_KEY / "last-run.json"
        assert last_run_path.is_file()
        last_run = json.loads(last_run_path.read_text(encoding="utf-8"))
        _assert_no_forbidden_delivery_keys(last_run)
        assert last_run["mode"] == "preview"
        assert last_run["ok"] is True
        assert last_run["delivered"] is False
        assert last_run["delivery_status"] == "preview_only"
        assert last_run["plan"]["error"] == ""
        assert len(last_run["plan"]["items"]) == 2
        assert last_run["plan"]["items"][0] == {
            "channel": "discord",
            "target": EXPECTED_DISCORD_TARGET,
            "text": bottom_text,
            "media": str(retained_0.resolve()),
        }
        assert last_run["plan"]["items"][1] == {
            "channel": "discord",
            "target": EXPECTED_DISCORD_TARGET,
            "text": top_text,
            "media": str(retained_1.resolve()),
        }
        _assert_no_temp_files(ws, outbox_tmp)


@pytest.mark.asyncio
async def test_t26_preserves_legacy_pending_inflight_receipts_and_repeat_preview_collects_fresh_outputs(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    _setup_workspace(ws)
    fake_bin, _, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)
    empty_secrets = tmp_path / "empty_secrets"
    empty_secrets.mkdir()

    # Seed pre-existing legacy state (including cached commands, plan, inflight=0, receipts, and attachments).
    state_dir = ws / "state" / "cron-command" / T26_JOB_KEY
    receipts_dir = state_dir / "receipts"
    legacy_att_dir = state_dir / "attachments"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    legacy_att_dir.mkdir(parents=True, exist_ok=True)

    legacy_att_0 = legacy_att_dir / "0.png"
    legacy_att_bytes = b"\x89PNG\r\n\x1a\nLEGACY-STALE-ATTACHMENT"
    legacy_att_0.write_bytes(legacy_att_bytes)

    pending_path = state_dir / "pending.json"
    legacy_pending_obj = {
        "commands": {
            json.dumps(["bash", "skills/us-bottom-monitor/run.sh", "--json", "run"]): json.dumps(
                {
                    "ok": True,
                    "feishu_message": "STALE-CACHED-BOTTOM-DO-NOT-USE",
                    "chart": str(legacy_att_0),
                }
            ),
            json.dumps(["bash", "skills/us-top-monitor/run.sh", "--json", "run"]): json.dumps(
                {
                    "ok": True,
                    "feishu_message": "STALE-CACHED-TOP-DO-NOT-USE",
                    "chart": str(legacy_att_0),
                }
            ),
        },
        "next": 0,
        "inflight": 0,
        "plan": {
            "items": [
                {
                    "channel": "discord",
                    "target": EXPECTED_DISCORD_TARGET,
                    "text": "STALE-PLAN-ITEM-DO-NOT-USE",
                    "media": str(legacy_att_0),
                }
            ],
            "text": "NO_REPLY",
            "error": "",
            "after": None,
        },
    }
    pending_raw_bytes = (json.dumps(legacy_pending_obj, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    pending_path.write_bytes(pending_raw_bytes)

    receipt_json = receipts_dir / "legacy-attempt-001.json"
    receipt_stdout = receipts_dir / "legacy-attempt-001.stdout"
    receipt_stderr = receipts_dir / "legacy-attempt-001.stderr"
    receipt_json.write_bytes(b'{"returncode": 127}\n')
    receipt_stdout.write_bytes(b"")
    receipt_stderr.write_bytes(b"openclaw: command not found\n")

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"
    _write_collector_fixture(
        ws, "us-bottom-monitor", spec_file=bottom_spec, invocation_log=invocation_log
    )
    _write_collector_fixture(
        ws, "us-top-monitor", spec_file=top_spec, invocation_log=invocation_log
    )

    # Preview Run 1 (no skill venv/.venv directories exist anywhere).
    _write_spec(
        bottom_spec,
        feishu_message="FRESH-BOTTOM-RUN-1",
        chart="temp/bottom.png",
        write_chart_rel="temp/bottom.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nFRESH-BOTTOM-PNG-1",
    )
    _write_spec(
        top_spec,
        feishu_message="FRESH-TOP-RUN-1",
        chart="temp/top.png",
        write_chart_rel="temp/top.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nFRESH-TOP-PNG-1",
    )

    res_1 = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id="preview-run-1",
        job_key=T26_JOB_KEY,
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=tmp_path / "tmpfs_1",
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_1.exit_code == 0, res_1.redacted_stderr
    assert res_1.outbox_payload is not None
    assert [it["text"] for it in res_1.outbox_payload["items"]] == [
        "FRESH-BOTTOM-RUN-1",
        "FRESH-TOP-RUN-1",
    ]
    assert "STALE-" not in json.dumps(res_1.outbox_payload, ensure_ascii=False)

    # Legacy pending.json, inflight, receipts, and legacy attachment remain untouched byte-for-byte.
    assert pending_path.is_file()
    assert pending_path.read_bytes() == pending_raw_bytes
    assert receipt_json.read_bytes() == b'{"returncode": 127}\n'
    assert receipt_stdout.read_bytes() == b""
    assert receipt_stderr.read_bytes() == b"openclaw: command not found\n"
    assert legacy_att_0.read_bytes() == legacy_att_bytes

    # Preview Run 2 with updated collector outputs in the same workspace
    _write_spec(
        bottom_spec,
        feishu_message="FRESH-BOTTOM-RUN-2",
        chart="temp/bottom.png",
        write_chart_rel="temp/bottom.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nFRESH-BOTTOM-PNG-2",
    )
    _write_spec(
        top_spec,
        feishu_message="FRESH-TOP-RUN-2",
        chart="temp/top.png",
        write_chart_rel="temp/top.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nFRESH-TOP-PNG-2",
    )

    res_2 = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id="preview-run-2",
        job_key=T26_JOB_KEY,
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=tmp_path / "tmpfs_2",
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_2.exit_code == 0, res_2.redacted_stderr
    assert res_2.outbox_payload is not None
    assert [it["text"] for it in res_2.outbox_payload["items"]] == [
        "FRESH-BOTTOM-RUN-2",
        "FRESH-TOP-RUN-2",
    ]
    _assert_no_bootstrap_or_openclaw(tmp_path, ws)

    # Both runs ran both Python CLIs freshly without skill venvs (4 total invocations in strict order).
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        f"us-bottom-monitor:{SKILL_CLI_REL_PATHS['us-bottom-monitor']}:--json run",
        f"us-top-monitor:{SKILL_CLI_REL_PATHS['us-top-monitor']}:--json run",
        f"us-bottom-monitor:{SKILL_CLI_REL_PATHS['us-bottom-monitor']}:--json run",
        f"us-top-monitor:{SKILL_CLI_REL_PATHS['us-top-monitor']}:--json run",
    ]

    # Run 1 retained chart copies were NOT overwritten by Run 2; Run 2 has its own copies; last-run.json reflects Run 2.
    assert (
        ws / ".dalton-runs" / "preview-run-1" / "attachments" / "0.png"
    ).read_bytes() == b"\x89PNG\r\n\x1a\nFRESH-BOTTOM-PNG-1"
    assert (
        ws / ".dalton-runs" / "preview-run-1" / "attachments" / "1.png"
    ).read_bytes() == b"\x89PNG\r\n\x1a\nFRESH-TOP-PNG-1"
    assert (
        ws / ".dalton-runs" / "preview-run-2" / "attachments" / "0.png"
    ).read_bytes() == b"\x89PNG\r\n\x1a\nFRESH-BOTTOM-PNG-2"
    assert (
        ws / ".dalton-runs" / "preview-run-2" / "attachments" / "1.png"
    ).read_bytes() == b"\x89PNG\r\n\x1a\nFRESH-TOP-PNG-2"

    last_run_2 = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert [it["text"] for it in last_run_2["plan"]["items"]] == [
        "FRESH-BOTTOM-RUN-2",
        "FRESH-TOP-RUN-2",
    ]
    assert pending_path.read_bytes() == pending_raw_bytes
    _assert_no_temp_files(ws, tmp_path / "tmpfs_1", tmp_path / "tmpfs_2")


@pytest.mark.asyncio
async def test_t26_one_or_both_collectors_fail_returns_nonzero_and_preserves_error_wording(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    _setup_workspace(ws)
    fake_bin, _, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)
    empty_secrets = tmp_path / "empty_secrets"
    empty_secrets.mkdir()

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"
    _write_collector_fixture(
        ws, "us-bottom-monitor", spec_file=bottom_spec, invocation_log=invocation_log
    )
    _write_collector_fixture(
        ws, "us-top-monitor", spec_file=top_spec, invocation_log=invocation_log
    )

    last_run_path = ws / "state" / "cron-command" / T26_JOB_KEY / "last-run.json"

    # 1. Bottom collector fails (exit 2), top collector succeeds -> exit 1, partial error wording appended.
    _write_spec(
        bottom_spec,
        raw_stdout="yfinance timeout",
        stderr_text="Traceback: network unreachable",
        exit_code=2,
    )
    _write_spec(
        top_spec,
        feishu_message="TOP-OK-MESSAGE",
        chart="temp/top_ok.png",
        write_chart_rel="temp/top_ok.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nTOP-OK-PNG",
    )
    res_bottom_fail = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id="run-partial-bottom-fail",
        job_key=T26_JOB_KEY,
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=tmp_path / "tmpfs_p1",
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_bottom_fail.exit_code == 1
    eval_1 = evaluate_task_run(
        TaskEvaluationContext(job_key=T26_JOB_KEY, exit_code=res_bottom_fail.exit_code)
    )
    assert eval_1.ok is False and eval_1.status == "failed"
    assert (
        resolve_notify_status(
            terminal_status=eval_1.status,
            notify_enabled=False,
            has_outbox_payload=True,
            is_trial=True,
        )
        == "skipped"
    )
    assert res_bottom_fail.outbox_payload is not None
    assert res_bottom_fail.outbox_payload["ok"] is False
    assert res_bottom_fail.outbox_payload["delivered"] is False
    assert res_bottom_fail.outbox_payload["delivery_status"] == "failed"
    assert res_bottom_fail.outbox_payload["error"].startswith(
        f"us-bottom-monitor：{SKILL_CLI_REL_PATHS['us-bottom-monitor']} 失败（exit 2）："
    )
    assert len(res_bottom_fail.outbox_payload["items"]) == 1
    item_p1 = res_bottom_fail.outbox_payload["items"][0]
    assert item_p1["skill"] == "us-top-monitor"
    assert item_p1["original_text"] == "TOP-OK-MESSAGE"
    assert item_p1["text"].startswith("TOP-OK-MESSAGE\n⚠️ 另一份未取得：us-bottom-monitor：")
    assert (
        ws / ".dalton-runs" / "run-partial-bottom-fail" / "attachments" / "0.png"
    ).read_bytes() == b"\x89PNG\r\n\x1a\nTOP-OK-PNG"

    lr_1 = json.loads(last_run_path.read_text(encoding="utf-8"))
    assert lr_1["ok"] is False and lr_1["delivered"] is False
    assert lr_1["plan"]["error"] == res_bottom_fail.outbox_payload["error"]
    assert lr_1["plan"]["items"][0]["text"] == item_p1["text"]

    # 2. Bottom collector succeeds, top collector returns {"ok": false, "error": ...} -> exit 1.
    _write_spec(
        bottom_spec,
        feishu_message="BOTTOM-OK-MESSAGE",
        chart="temp/bottom_ok.png",
        write_chart_rel="temp/bottom_ok.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nBOTTOM-OK-PNG",
    )
    _write_spec(
        top_spec,
        ok=False,
        error="CBOE PCR CSV unavailable",
    )
    res_top_fail = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id="run-partial-top-fail",
        job_key=T26_JOB_KEY,
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=tmp_path / "tmpfs_p2",
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_top_fail.exit_code == 1
    assert res_top_fail.outbox_payload is not None
    assert res_top_fail.outbox_payload["ok"] is False
    assert res_top_fail.outbox_payload["error"] == "us-top-monitor：CBOE PCR CSV unavailable"
    item_p2 = res_top_fail.outbox_payload["items"][0]
    assert item_p2["skill"] == "us-bottom-monitor"
    assert (
        item_p2["text"]
        == "BOTTOM-OK-MESSAGE\n⚠️ 另一份未取得：us-top-monitor：CBOE PCR CSV unavailable"
    )

    # 3. Both collectors fail -> exit 1, combined error preserves bottom-then-top order and original prefix.
    _write_spec(bottom_spec, raw_stdout="not valid json at all\n", exit_code=0)
    _write_spec(top_spec, ok=False, error="top source down")
    res_both_fail = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id="run-both-fail",
        job_key=T26_JOB_KEY,
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=tmp_path / "tmpfs_p3",
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_both_fail.exit_code == 1
    eval_3 = evaluate_task_run(
        TaskEvaluationContext(job_key=T26_JOB_KEY, exit_code=res_both_fail.exit_code)
    )
    assert eval_3.ok is False and eval_3.status == "failed"
    assert res_both_fail.outbox_payload is not None
    expected_err = "us-bottom-monitor：采集输出没有返回完整 JSON\nus-top-monitor：top source down"
    assert res_both_fail.outbox_payload["error"] == expected_err
    assert res_both_fail.outbox_payload["items"] == [
        {
            "index": 0,
            "skill": None,
            "channel": "discord",
            "target": EXPECTED_DISCORD_TARGET,
            "text": "顶部和底部监控均失败：\n" + expected_err,
            "media": None,
            "delivered": False,
        }
    ]
    lr_3 = json.loads(last_run_path.read_text(encoding="utf-8"))
    assert lr_3["plan"]["error"] == expected_err
    assert lr_3["plan"]["items"] == [
        {
            "channel": "discord",
            "target": EXPECTED_DISCORD_TARGET,
            "text": "顶部和底部监控均失败：\n" + expected_err,
        }
    ]
    _assert_no_bootstrap_or_openclaw(tmp_path, ws)
    _assert_no_temp_files(ws, tmp_path / "tmpfs_p1", tmp_path / "tmpfs_p2", tmp_path / "tmpfs_p3")


@pytest.mark.asyncio
async def test_t26_rejects_missing_empty_directory_outside_and_symlink_charts_without_false_success(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    _setup_workspace(ws)
    fake_bin, _, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)
    empty_secrets = tmp_path / "empty_secrets"
    empty_secrets.mkdir()

    outside_dir = tmp_path / "outside_zone"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_png = outside_dir / "outside_secret.png"
    outside_bytes = b"\x89PNG\r\n\x1a\nOUTSIDE-WORKSPACE-SECRET-PNG"
    outside_png.write_bytes(outside_bytes)

    (ws / "temp").mkdir(parents=True, exist_ok=True)
    empty_png = ws / "temp" / "empty.png"
    empty_png.write_bytes(b"")
    chart_dir = ws / "temp" / "chart_as_dir.png"
    chart_dir.mkdir(parents=True, exist_ok=True)
    symlink_outside = ws / "temp" / "symlink_outside.png"
    symlink_outside.symlink_to(outside_png)
    bad_ext = ws / "temp" / "chart.txt"
    bad_ext.write_text("not an image", encoding="utf-8")

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"
    _write_collector_fixture(
        ws, "us-bottom-monitor", spec_file=bottom_spec, invocation_log=invocation_log
    )
    _write_collector_fixture(
        ws, "us-top-monitor", spec_file=top_spec, invocation_log=invocation_log
    )
    _write_spec(
        top_spec,
        feishu_message="TOP-VALID",
        chart="temp/valid_top.png",
        write_chart_rel="temp/valid_top.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nVALID-TOP-PNG",
    )

    invalid_chart_cases = [
        ("missing", "temp/nonexistent_chart.png"),
        ("empty", "temp/empty.png"),
        ("directory", "temp/chart_as_dir.png"),
        ("rel_outside", "../outside_zone/outside_secret.png"),
        ("abs_outside", str(outside_png)),
        ("symlink_outside", "temp/symlink_outside.png"),
        ("bad_ext", "temp/chart.txt"),
    ]

    for label, bad_chart in invalid_chart_cases:
        _write_spec(
            bottom_spec,
            feishu_message=f"BOTTOM-{label}",
            chart=bad_chart,
        )
        run_id = f"run-bad-chart-{label}"
        outbox_tmp = tmp_path / f"tmpfs_{label}"
        res = await run_wrapped_command(
            [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
            run_id=run_id,
            job_key=T26_JOB_KEY,
            workdir=str(ws),
            workspace_dir=str(ws),
            secrets_dir=empty_secrets,
            outbox_tmp_dir=outbox_tmp,
            base_env=base_env,
            stdout_sink=io.StringIO(),
            stderr_sink=io.StringIO(),
        )
        assert res.exit_code == 1, f"Case {label} must fail with exit_code=1"
        assert res.outbox_payload is not None
        assert res.outbox_payload["ok"] is False
        assert res.outbox_payload["error"].startswith("us-bottom-monitor：")

        # Outside file bytes must never be copied into .dalton-runs.
        run_dir = ws / ".dalton-runs" / run_id
        for p in run_dir.rglob("*"):
            if p.is_file():
                assert outside_bytes not in p.read_bytes()
        _assert_no_temp_files(ws, outbox_tmp)

    _assert_no_bootstrap_or_openclaw(tmp_path, ws)


def test_t26_atomic_write_cleanup_on_error_and_live_send_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_overlay_module()
    fake_bin, _, openclaw_marker = _install_trap_openclaw(tmp_path)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}")

    # 1. Atomic write cleans up temporary file when os.replace raises OSError.
    target_json = tmp_path / "atomic_dir" / "last-run.json"
    monkeypatch.setattr(
        mod.os,
        "replace",
        create_autospec(os.replace, side_effect=OSError("simulated replace failure")),
    )
    with pytest.raises(OSError, match="simulated replace failure"):
        mod._atomic_write_json(target_json, {"ok": True})
    assert not target_json.exists()
    _assert_no_temp_files(tmp_path)

    # 2. Dalton mode blocks send() unconditionally even when DALTON_NOTIFY_DRY_RUN=0.
    for dry_val in ("1", "0"):
        monkeypatch.setenv("DALTON_NO_DIRECT_SEND", "1")
        monkeypatch.setenv("DALTON_NOTIFY_DRY_RUN", dry_val)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        with pytest.raises(RuntimeError, match="Dalton 模式禁止调用 openclaw"):
            mod.send({"channel": "discord", "target": EXPECTED_DISCORD_TARGET, "text": "hello"})
        assert not openclaw_marker.exists()

    # 3. Non-Dalton mode also refuses live sends when running under pytest (PYTEST_CURRENT_TEST set).
    for var in mod.DALTON_RUNNER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DALTON_NO_DIRECT_SEND", "0")
    assert mod.is_dalton_mode() is False
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/unit/dalton/test_t26_task_preview.py::guard")
    with pytest.raises(RuntimeError, match="测试环境禁止调用 openclaw"):
        mod.send({"channel": "discord", "target": EXPECTED_DISCORD_TARGET, "text": "hello"})
    assert not openclaw_marker.exists()


@pytest.mark.asyncio
async def test_t26_wrapper_actual_outbox_redaction_path(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _setup_workspace(ws)
    fake_bin, _, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)

    secrets_dir = tmp_path / "run_secrets"
    secrets_dir.mkdir(parents=True)
    secret_val = "sk-t26-vault-secret-token-99887766"
    (secrets_dir / "DISCORD_PREVIEW_TEST_SECRET").write_text(secret_val + "\n", encoding="utf-8")

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"
    _write_collector_fixture(
        ws, "us-bottom-monitor", spec_file=bottom_spec, invocation_log=invocation_log
    )
    _write_collector_fixture(
        ws, "us-top-monitor", spec_file=top_spec, invocation_log=invocation_log
    )

    _write_spec(
        bottom_spec,
        feishu_message="底部监控摘要 credential=",
        chart="temp/bottom_sec.png",
        write_chart_rel="temp/bottom_sec.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nBOTTOM-SEC-PNG",
        progress_line="collector progress leak=",
        echo_env_var="DISCORD_PREVIEW_TEST_SECRET",
    )
    _write_spec(
        top_spec,
        feishu_message="顶部监控摘要正常",
        chart="temp/top_sec.png",
        write_chart_rel="temp/top_sec.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nTOP-SEC-PNG",
    )

    outbox_tmp = tmp_path / "tmpfs_redact"
    outbox_tmp.mkdir(parents=True)
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()

    res = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id="run-t26-redact",
        job_key=T26_JOB_KEY,
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=secrets_dir,
        outbox_tmp_dir=outbox_tmp,
        notify_dry_run=False,
        timeout_seconds=30,
        no_output_timeout_seconds=30,
        base_env=base_env,
        stdout_sink=stdout_buf,
        stderr_sink=stderr_buf,
    )
    assert res.exit_code == 0, res.redacted_stderr
    _assert_no_bootstrap_or_openclaw(tmp_path, ws)
    assert not (outbox_tmp / "outbox.json").exists()
    assert res.outbox_path is not None and res.outbox_path.is_file()

    persisted_raw = res.outbox_path.read_text(encoding="utf-8")
    assert secret_val not in persisted_raw
    assert secret_val not in res.redacted_stdout
    assert secret_val not in res.redacted_stderr
    assert secret_val not in stdout_buf.getvalue()
    assert secret_val not in stderr_buf.getvalue()

    persisted_json = json.loads(persisted_raw)
    assert "[REDACTED]" in persisted_json["text"]
    assert persisted_json["items"][0]["text"] == "底部监控摘要 credential=[REDACTED]"
    assert persisted_json["items"][1]["text"] == "顶部监控摘要正常"
    _assert_no_temp_files(ws, outbox_tmp)


def test_t26_dalton_python_bin_priority_stale_venv_ignored_and_external_run_dir_creates_nothing(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    installed_script = _setup_workspace(ws)
    fake_bin, _, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bootstrap_marker = tmp_path / "bootstrap_invoked.marker"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"
    _write_collector_fixture(
        ws,
        "us-bottom-monitor",
        spec_file=bottom_spec,
        invocation_log=invocation_log,
        bootstrap_marker=bootstrap_marker,
    )
    _write_collector_fixture(
        ws,
        "us-top-monitor",
        spec_file=top_spec,
        invocation_log=invocation_log,
        bootstrap_marker=bootstrap_marker,
    )
    _write_spec(
        bottom_spec,
        feishu_message="BOTTOM-DIRECT-CLI",
        chart="temp/b_direct.png",
        write_chart_rel="temp/b_direct.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nB-DIRECT",
    )
    _write_spec(
        top_spec,
        feishu_message="TOP-DIRECT-CLI",
        chart="temp/t_direct.png",
        write_chart_rel="temp/t_direct.png",
        chart_bytes=b"\x89PNG\r\n\x1a\nT-DIRECT",
    )

    # 1. External absolute DALTON_RUN_DIR is rejected BEFORE creating any external directory.
    never_created_outside = tmp_path / "outside_never_created"
    bad_external_run_dir = never_created_outside / "nested_parent" / "run-external"
    ext_env = {
        **base_env,
        "DALTON_NO_DIRECT_SEND": "1",
        "OPENCLAW_WORKSPACE": str(ws),
        "DALTON_RUN_ID": "run-external",
        "DALTON_RUN_DIR": str(bad_external_run_dir),
        "DALTON_OUTBOX_FILE": str(tmp_path / "tmpfs_ext" / "outbox.json"),
    }
    ext_res = subprocess.run(
        [sys.executable, str(installed_script), T26_JOB_KEY],
        cwd=ws,
        env=ext_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ext_res.returncode == 1
    assert "DALTON_RUN_DIR 必须位于 workspace 内" in ext_res.stderr
    assert not never_created_outside.exists()
    assert not (tmp_path / "tmpfs_ext").exists()
    assert not invocation_log.exists()

    # 2. Ancestor symlink (inside workspace escaping out, or outside workspace pointing in) is
    #    rejected before creating any directories.
    existing_outside = tmp_path / "outside_existing_target"
    existing_outside.mkdir(parents=True)
    (ws / "escape_link").symlink_to(existing_outside)
    sym_env = {
        **ext_env,
        "DALTON_RUN_ID": "run-symlink-dir",
        "DALTON_RUN_DIR": str(ws / "escape_link" / "must_not_be_created" / "run-symlink-dir"),
    }
    sym_res = subprocess.run(
        [sys.executable, str(installed_script), T26_JOB_KEY],
        cwd=ws,
        env=sym_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sym_res.returncode == 1
    assert not (existing_outside / "must_not_be_created").exists()
    assert not invocation_log.exists()

    ext_link_into_ws = tmp_path / "external_link_into_ws"
    ext_link_into_ws.symlink_to(ws)
    ext_sym_env = {
        **ext_env,
        "DALTON_RUN_ID": "run-ext-symlink-in",
        "DALTON_RUN_DIR": str(ext_link_into_ws / "must_not_create_in_ws" / "run-ext-symlink-in"),
    }
    ext_sym_res = subprocess.run(
        [sys.executable, str(installed_script), T26_JOB_KEY],
        cwd=ws,
        env=ext_sym_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ext_sym_res.returncode == 1
    assert "路径包含软链接" in ext_sym_res.stderr
    assert not (ws / "must_not_create_in_ws").exists()
    assert not invocation_log.exists()

    # 3. Plant broken leftover host venv files inside skill dirs to prove Dalton ignores skill venvs,
    #    and verify DALTON_PYTHON_BIN takes priority over sys.executable alongside system temp fallback.
    for skill in SKILL_CLI_REL_PATHS:
        stale_venv_bin = ws / "skills" / skill / "venv" / "bin"
        stale_venv_bin.mkdir(parents=True)
        bad_py = stale_venv_bin / "python"
        bad_py.write_bytes(b"\xcf\xfa\xed\xfeBROKEN-MACOS-MACHO-BINARY")
        bad_py.chmod(0o755)

    py_wrapper_log = tmp_path / "dalton_python_bin.log"
    custom_python = tmp_path / "custom_dalton_python"
    custom_python.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {str(py_wrapper_log)!r}\n"
        f"exec {str(Path(sys.executable))!r} \"$@\"\n",
        encoding="utf-8",
    )
    custom_python.chmod(0o755)

    std_run_id = "run-custom-py-and-std-tmp"
    std_temp_outbox_dir = Path(tempfile.gettempdir()) / f"dalton-outbox-{std_run_id}"
    shutil.rmtree(std_temp_outbox_dir, ignore_errors=True)
    try:
        ok_env = {
            **base_env,
            "DALTON_NO_DIRECT_SEND": "1",
            "OPENCLAW_WORKSPACE": str(ws),
            "DALTON_RUN_ID": std_run_id,
            "DALTON_RUN_DIR": str(ws / ".dalton-runs" / std_run_id),
            "DALTON_PYTHON_BIN": str(custom_python),
        }
        ok_res = subprocess.run(
            [sys.executable, str(installed_script), T26_JOB_KEY],
            cwd=ws,
            env=ok_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ok_res.returncode == 0, ok_res.stderr
        assert ok_res.stdout.strip() == "NO_REPLY"
        assert py_wrapper_log.read_text(encoding="utf-8").splitlines() == [
            f"{SKILL_CLI_REL_PATHS['us-bottom-monitor']} --json run",
            f"{SKILL_CLI_REL_PATHS['us-top-monitor']} --json run",
        ]
        assert (std_temp_outbox_dir / "outbox.json").is_file()
        std_outbox = json.loads((std_temp_outbox_dir / "outbox.json").read_text(encoding="utf-8"))
        assert std_outbox["ok"] is True
        assert std_outbox["delivered"] is False
        assert (ws / ".dalton-runs" / std_run_id / "attachments" / "0.png").is_file()
        assert (ws / ".dalton-runs" / std_run_id / "attachments" / "1.png").is_file()
    finally:
        shutil.rmtree(std_temp_outbox_dir, ignore_errors=True)

    # Also verify unset DALTON_PYTHON_BIN cleanly defaults to sys.executable.
    fallback_run_id = "run-default-sys-executable"
    fallback_outbox = tmp_path / "tmpfs_fallback" / "outbox.json"
    fallback_env = {
        **base_env,
        "DALTON_NO_DIRECT_SEND": "1",
        "OPENCLAW_WORKSPACE": str(ws),
        "DALTON_RUN_ID": fallback_run_id,
        "DALTON_RUN_DIR": str(ws / ".dalton-runs" / fallback_run_id),
        "DALTON_OUTBOX_FILE": str(fallback_outbox),
    }
    fallback_res = subprocess.run(
        [sys.executable, str(installed_script), T26_JOB_KEY],
        cwd=ws,
        env=fallback_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fallback_res.returncode == 0, fallback_res.stderr
    assert fallback_outbox.is_file()
    _assert_no_bootstrap_or_openclaw(tmp_path, ws, expect_no_skill_venv=False)


@pytest.mark.asyncio
async def test_t26_external_dir_symlink_to_workspace_png_fails_without_copy_and_preserves_other_collector(
    tmp_path: Path,
) -> None:
    # Simulate macOS-style system root symlink (/var -> /private/var) above the workspace directory
    # so both raw_ws and resolved ws_root normalization are exercised alongside external symlink rejection.
    real_var = tmp_path / "real_var"
    real_ws = real_var / "workspace"
    real_ws.mkdir(parents=True)
    os_var_alias = tmp_path / "os_var_alias"
    os_var_alias.symlink_to(real_var)
    raw_ws_via_parent = os_var_alias / "workspace"
    ws_root = real_ws.resolve(strict=True)

    _setup_workspace(ws_root)
    fake_bin, _, _ = _install_trap_openclaw(tmp_path)
    base_env = _build_clean_base_env(fake_bin)
    empty_secrets = tmp_path / "empty_secrets"
    empty_secrets.mkdir()

    specs_dir = tmp_path / "specs"
    invocation_log = tmp_path / "invocations.log"
    bottom_spec = specs_dir / "bottom.json"
    top_spec = specs_dir / "top.json"
    _write_collector_fixture(
        ws_root, "us-bottom-monitor", spec_file=bottom_spec, invocation_log=invocation_log
    )
    _write_collector_fixture(
        ws_root, "us-top-monitor", spec_file=top_spec, invocation_log=invocation_log
    )

    bottom_chart_dir = ws_root / "temp" / "us-bottom-monitor"
    bottom_chart_dir.mkdir(parents=True, exist_ok=True)
    bottom_aliased_bytes = b"\x89PNG\r\n\x1a\nVALID-IN-WS-VIA-EXTERNAL-SYMLINK"
    top_valid_bytes = b"\x89PNG\r\n\x1a\nVALID-TOP-KEEP-ON-PARTIAL-FAIL"

    # External directory symlink outside workspace pointing to valid chart directory inside workspace.
    external_chart_alias = tmp_path / "chart-alias"
    external_chart_alias.symlink_to(bottom_chart_dir)

    _write_spec(
        bottom_spec,
        feishu_message="BOTTOM-THROUGH-EXTERNAL-ALIAS",
        chart=str(external_chart_alias / "chart.png"),
        write_chart_rel="temp/us-bottom-monitor/chart.png",
        chart_bytes=bottom_aliased_bytes,
    )
    _write_spec(
        top_spec,
        feishu_message="TOP-VALID-PRESERVED",
        chart="temp/us-top-monitor/valid_top.png",
        write_chart_rel="temp/us-top-monitor/valid_top.png",
        chart_bytes=top_valid_bytes,
    )

    run_id_ext = "run-ext-dir-symlink-chart"
    outbox_tmp_ext = tmp_path / "tmpfs_ext_alias"
    res_ext = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id=run_id_ext,
        job_key=T26_JOB_KEY,
        workdir=str(raw_ws_via_parent),
        workspace_dir=str(raw_ws_via_parent),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=outbox_tmp_ext,
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )

    # 1. External symlink alias to in-workspace PNG must fail, not copy bottom chart, and preserve top chart.
    assert (ws_root / "temp" / "us-bottom-monitor" / "chart.png").is_file()
    assert res_ext.exit_code == 1
    eval_ext = evaluate_task_run(
        TaskEvaluationContext(job_key=T26_JOB_KEY, exit_code=res_ext.exit_code)
    )
    assert eval_ext.ok is False and eval_ext.status == "failed"
    assert res_ext.outbox_payload is not None
    assert res_ext.outbox_payload["ok"] is False
    assert res_ext.outbox_payload["delivered"] is False
    assert res_ext.outbox_payload["delivery_status"] == "failed"
    assert res_ext.outbox_payload["error"].startswith("us-bottom-monitor：")
    assert "附件不存在或不在 workspace" in res_ext.outbox_payload["error"]

    run_dir_ext = ws_root / ".dalton-runs" / run_id_ext
    att_files_ext = sorted(p.name for p in (run_dir_ext / "attachments").iterdir())
    assert att_files_ext == ["0.png"]
    assert (run_dir_ext / "attachments" / "0.png").read_bytes() == top_valid_bytes
    for p in run_dir_ext.rglob("*"):
        if p.is_file():
            assert bottom_aliased_bytes not in p.read_bytes()

    assert len(res_ext.outbox_payload["items"]) == 1
    kept_item = res_ext.outbox_payload["items"][0]
    assert kept_item["skill"] == "us-top-monitor"
    assert kept_item["channel"] == "discord"
    assert kept_item["target"] == EXPECTED_DISCORD_TARGET
    assert kept_item["original_text"] == "TOP-VALID-PRESERVED"
    assert kept_item["text"] == (
        "TOP-VALID-PRESERVED\n⚠️ 另一份未取得：" + res_ext.outbox_payload["error"]
    )
    assert kept_item["media"] == "attachments/0.png"
    assert kept_item["delivered"] is False

    last_run_path = ws_root / "state" / "cron-command" / T26_JOB_KEY / "last-run.json"
    lr_ext = json.loads(last_run_path.read_text(encoding="utf-8"))
    assert lr_ext["ok"] is False and lr_ext["delivered"] is False
    assert lr_ext["plan"]["error"] == res_ext.outbox_payload["error"]
    assert lr_ext["plan"]["items"] == [
        {
            "channel": "discord",
            "target": EXPECTED_DISCORD_TARGET,
            "text": kept_item["text"],
            "media": str((run_dir_ext / "attachments" / "0.png").resolve()),
        }
    ]
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        f"us-bottom-monitor:{SKILL_CLI_REL_PATHS['us-bottom-monitor']}:--json run",
        f"us-top-monitor:{SKILL_CLI_REL_PATHS['us-top-monitor']}:--json run",
    ]

    # Also test a /tmp-anchored external directory symlink (/tmp/.../chart-alias/chart.png -> ws/temp).
    if Path("/tmp").is_dir() and os.access("/tmp", os.W_OK):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="t26-ext-alias-") as slash_tmp_dir:
            slash_tmp_alias = Path(slash_tmp_dir) / "chart-alias"
            slash_tmp_alias.symlink_to(bottom_chart_dir)
            _write_spec(
                bottom_spec,
                feishu_message="BOTTOM-THROUGH-SLASH-TMP-ALIAS",
                chart=str(slash_tmp_alias / "chart.png"),
                write_chart_rel="temp/us-bottom-monitor/chart.png",
                chart_bytes=bottom_aliased_bytes,
            )
            res_slash_tmp = await run_wrapped_command(
                [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
                run_id="run-slash-tmp-alias-chart",
                job_key=T26_JOB_KEY,
                workdir=str(raw_ws_via_parent),
                workspace_dir=str(raw_ws_via_parent),
                secrets_dir=empty_secrets,
                outbox_tmp_dir=tmp_path / "tmpfs_slash_tmp_alias",
                base_env=base_env,
                stdout_sink=io.StringIO(),
                stderr_sink=io.StringIO(),
            )
            assert res_slash_tmp.exit_code == 1
            assert res_slash_tmp.outbox_payload is not None
            assert res_slash_tmp.outbox_payload["ok"] is False
            assert "附件不存在或不在 workspace" in res_slash_tmp.outbox_payload["error"]
            run_dir_slash = ws_root / ".dalton-runs" / "run-slash-tmp-alias-chart"
            assert sorted(p.name for p in (run_dir_slash / "attachments").iterdir()) == ["0.png"]
            for p in run_dir_slash.rglob("*"):
                if p.is_file():
                    assert bottom_aliased_bytes not in p.read_bytes()

    # 2. In-workspace directory symlink pointing to in-workspace PNG is also rejected without copying.
    internal_chart_alias = ws_root / "temp" / "internal-chart-alias"
    internal_chart_alias.symlink_to(bottom_chart_dir)
    _write_spec(
        bottom_spec,
        feishu_message="BOTTOM-THROUGH-INTERNAL-ALIAS",
        chart=str(raw_ws_via_parent / "temp" / "internal-chart-alias" / "chart.png"),
        write_chart_rel="temp/us-bottom-monitor/chart.png",
        chart_bytes=bottom_aliased_bytes,
    )
    run_id_int = "run-int-dir-symlink-chart"
    outbox_tmp_int = tmp_path / "tmpfs_int_alias"
    res_int = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id=run_id_int,
        job_key=T26_JOB_KEY,
        workdir=str(raw_ws_via_parent),
        workspace_dir=str(raw_ws_via_parent),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=outbox_tmp_int,
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_int.exit_code == 1
    assert res_int.outbox_payload is not None
    assert res_int.outbox_payload["ok"] is False
    assert "附件路径包含软链接" in res_int.outbox_payload["error"]
    run_dir_int = ws_root / ".dalton-runs" / run_id_int
    assert sorted(p.name for p in (run_dir_int / "attachments").iterdir()) == ["0.png"]
    for p in run_dir_int.rglob("*"):
        if p.is_file():
            assert bottom_aliased_bytes not in p.read_bytes()

    # 3. Legitimate raw_ws and normalized ws_root absolute chart paths still succeed cleanly.
    _write_spec(
        bottom_spec,
        feishu_message="BOTTOM-RAW-WS-VALID",
        chart=str(raw_ws_via_parent / "temp" / "us-bottom-monitor" / "chart.png"),
        write_chart_rel="temp/us-bottom-monitor/chart.png",
        chart_bytes=bottom_aliased_bytes,
    )
    _write_spec(
        top_spec,
        feishu_message="TOP-RESOLVED-WS-VALID",
        chart=str(ws_root / "temp" / "us-top-monitor" / "valid_top.png"),
        write_chart_rel="temp/us-top-monitor/valid_top.png",
        chart_bytes=top_valid_bytes,
    )
    run_id_norm = "run-valid-ws-root-normalization"
    outbox_tmp_norm = tmp_path / "tmpfs_norm"
    res_norm = await run_wrapped_command(
        [sys.executable, "scripts/cron_command_tasks.py", T26_JOB_KEY],
        run_id=run_id_norm,
        job_key=T26_JOB_KEY,
        workdir=str(raw_ws_via_parent),
        workspace_dir=str(raw_ws_via_parent),
        secrets_dir=empty_secrets,
        outbox_tmp_dir=outbox_tmp_norm,
        base_env=base_env,
        stdout_sink=io.StringIO(),
        stderr_sink=io.StringIO(),
    )
    assert res_norm.exit_code == 0, res_norm.redacted_stderr
    assert res_norm.outbox_payload is not None
    assert res_norm.outbox_payload["ok"] is True
    assert res_norm.outbox_payload["delivered"] is False
    run_dir_norm = ws_root / ".dalton-runs" / run_id_norm
    assert (run_dir_norm / "attachments" / "0.png").read_bytes() == bottom_aliased_bytes
    assert (run_dir_norm / "attachments" / "1.png").read_bytes() == top_valid_bytes
    _assert_no_bootstrap_or_openclaw(tmp_path, ws_root)
    _assert_no_temp_files(ws_root, outbox_tmp_ext, outbox_tmp_int, outbox_tmp_norm)
