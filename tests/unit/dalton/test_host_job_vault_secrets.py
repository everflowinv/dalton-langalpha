"""Tests for controlled Vault secret passthrough to `runner: host` data tasks."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import json
import logging
import os
import time
from pathlib import Path
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import psycopg
import pytest

from src.dalton.data_jobs.container_runner import build_path_map
from src.dalton.data_jobs.host_runner import (
    DataTaskHostBridgeClient,
    HostRunOutcome,
    post_bridge_host_run,
    strip_secrets_from_host_run_body,
)
from src.dalton.data_jobs import scheduler as sched_mod
from src.dalton.data_jobs.scheduler import ClaimedTaskRun, execute_claimed_host_run

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import bridge_subprocess_env as bse  # noqa: E402
import researchdata_bridge as rb  # noqa: E402

PIPELINE_SECRETS = [
    "GEMINI_API_KEY",
    "INTERNAL_SERVICE_TOKEN",
    "SEED_ASR_APP_ID",
    "SEED_ASR_ACCESS_KEY",
    "VOLC_TOS_AK",
    "VOLC_TOS_SK",
    "VOLC_TOS_BUCKET",
]


def _make_autospec_db(cursor_steps: list[dict[str, Any]] | None = None):
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.closed = False
    steps = list(cursor_steps or [])
    executed: list[tuple[str, Any]] = []

    @asynccontextmanager
    async def _tx():
        yield

    @asynccontextmanager
    async def _cursor(*, binary: bool = False, row_factory: Any = None):
        del binary, row_factory
        cur = create_autospec(psycopg.AsyncCursor, instance=True)
        current: dict[str, Any] = {}

        async def _execute(query: Any, params: Any = None, **kwargs: Any):
            nonlocal current
            del kwargs
            executed.append((str(query), params))
            current = steps.pop(0) if steps else {}
            cur.rowcount = current.get("rowcount", 1)

        async def _fetchone():
            return current.get("fetchone")

        async def _fetchall():
            return current.get("fetchall", [])

        cur.execute.side_effect = _execute
        cur.fetchone.side_effect = _fetchone
        cur.fetchall.side_effect = _fetchall
        yield cur

    conn.transaction.side_effect = _tx
    conn.cursor.side_effect = _cursor
    return conn, executed


@pytest.mark.asyncio
async def test_scheduler_host_run_resolves_vault_sends_secrets_and_scrubs_db_log_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-admin-01")
    vault_vals = {
        "GEMINI_API_KEY": "vault-gem-secret-1111",
        "INTERNAL_SERVICE_TOKEN": "vault-svc-secret-2222",
    }
    mock_get_secrets = create_autospec(
        sched_mod.get_user_secrets_decrypted,
        return_value=dict(vault_vals),
    )
    monkeypatch.setattr(sched_mod, "get_user_secrets_decrypted", mock_get_secrets)

    host_client = create_autospec(DataTaskHostBridgeClient, instance=True)
    host_client.run_host_task.return_value = HostRunOutcome(
        ok=True,
        status_code=200,
        exit_code=0,
        exit_confirmed=True,
        log_tail="gemini=vault-gem-secret-1111 token=vault-svc-secret-2222 ok\n",
    )

    lock_conn, _ = _make_autospec_db(
        [
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (1,)},
            {"fetchone": (1,)},
        ]
    )
    conn, executed = _make_autospec_db(
        [
            {"rowcount": 1},  # starting -> running
            {"rowcount": 1},  # finalize UPDATE dalton_data_task_runs
            {"rowcount": 1, "fetchone": {"consecutive_failures": 0}},  # defs update
            {"rowcount": 1},  # slot release
        ]
    )

    now = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
    claimed = ClaimedTaskRun(
        run_id=str(uuid4()),
        job_key="podcast-weekly-pipeline",
        generation=1,
        runner="host",
        command_argv=[
            "bash",
            "skills/podcast-weekly-digest/scripts/run.sh",
            "--json",
            "pipeline",
            "--since-days",
            "7",
            "--video-model",
            "gemini-4-argon",
            "--text-backend",
            "dalton",
            "--text-model",
            "gemini-4-hydrogen-high-oauth",
        ],
        workdir="var/dalton/openclaw-data",
        timeout_seconds=10800,
        no_output_timeout_seconds=3600,
        required_secrets=list(PIPELINE_SECRETS),
        cos_mode="skip",
        notify_enabled=False,
        scheduled_slot_utc=now,
        deadline_utc=now + timedelta(hours=3),
        timezone="Asia/Hong_Kong",
        host_mode="run",
    )
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")

    with caplog.at_level(logging.WARNING, logger="src.dalton.data_jobs.scheduler"):
        committed, _ = await execute_claimed_host_run(
            conn,
            lock_conn=lock_conn,
            host_client=host_client,
            claimed=claimed,
            path_map=path_map,
        )

    assert committed is True
    mock_get_secrets.assert_awaited_once_with("pm-admin-01", names=PIPELINE_SECRETS)

    run_kwargs = host_client.run_host_task.await_args.kwargs
    assert run_kwargs["secrets"] == vault_vals
    assert run_kwargs["env"] == {"DALTON_BACKEND_URL": "http://127.0.0.1:8000"}

    # Missing Vault secret warning logs ONLY key names, never values, and still launches task
    warn_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "SEED_ASR_APP_ID" in warn_text
    assert "VOLC_TOS_BUCKET" in warn_text
    assert "vault-gem-secret-1111" not in warn_text
    assert "vault-svc-secret-2222" not in warn_text

    # log_tail persisted to DB has secret values replaced with ***
    finalize_params = executed[1][1]
    assert finalize_params["log_tail"] == "gemini=*** token=*** ok\n"
    assert "vault-gem-secret-1111" not in finalize_params["log_tail"]
    assert "vault-svc-secret-2222" not in finalize_params["log_tail"]


@pytest.mark.asyncio
async def test_post_bridge_host_run_forwards_secrets_and_strip_helper_removes_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "admin-tok-test")
    captured: list[dict[str, Any]] = []

    mock_resp = create_autospec(httpx.Response, instance=True)
    mock_resp.status_code = 200
    mock_resp.is_success = True
    mock_resp.content = b'{"ok": true, "exit_code": 0}'
    mock_resp.json.return_value = {"ok": True, "exit_code": 0, "exit_confirmed": True}

    mock_client = create_autospec(httpx.AsyncClient, instance=True)

    async def _post(url: str, *, json: dict[str, Any], headers: dict[str, str]):
        del url, headers
        captured.append(json)
        return mock_resp

    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.side_effect = _post

    client_factory = create_autospec(httpx.AsyncClient, return_value=mock_client)
    monkeypatch.setattr("src.dalton.data_jobs.host_runner.httpx.AsyncClient", client_factory)

    outcome = await post_bridge_host_run(
        generation=2,
        run_id="run-post-1",
        job_key="podcast-weekly-pipeline",
        host_mode="run",
        scheduled_slot_utc="2026-11-13T10:00:00Z",
        timeout=60,
        secrets={"GEMINI_API_KEY": "vault-gem-abc"},
        env={"DALTON_BACKEND_URL": "http://127.0.0.1:8000"},
    )
    assert outcome.ok is True
    assert len(captured) == 1
    assert captured[0]["secrets"] == {"GEMINI_API_KEY": "vault-gem-abc"}
    assert captured[0]["env"] == {"DALTON_BACKEND_URL": "http://127.0.0.1:8000"}

    sanitized = strip_secrets_from_host_run_body(captured[0])
    assert "secrets" not in sanitized
    assert sanitized["env"] == {"DALTON_BACKEND_URL": "http://127.0.0.1:8000"}
    assert sanitized["job_key"] == "podcast-weekly-pipeline"


@pytest.mark.asyncio
async def test_bridge_host_run_enforces_manifest_vault_whitelist_and_masks_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    probe_script = tmp_path / "probe_env.py"
    probe_script.write_text(
        (
            "import json, os, pathlib, sys\n"
            "outbox = pathlib.Path(os.environ['DALTON_OUTBOX_FILE'])\n"
            "snap = {\n"
            "  'GEMINI_API_KEY': os.environ.get('GEMINI_API_KEY'),\n"
            "  'GEMINI_MATCHED_VAULT': os.environ.get('GEMINI_API_KEY') == 'vault-gemini-real-12345',\n"
            "  'SVC_MATCHED_VAULT': os.environ.get('INTERNAL_SERVICE_TOKEN') == 'vault-internal-tok-67890',\n"
            "  'INTERNAL_SERVICE_TOKEN': os.environ.get('INTERNAL_SERVICE_TOKEN'),\n"
            "  'DALTON_BACKEND_URL': os.environ.get('DALTON_BACKEND_URL'),\n"
            "  'SEED_ASR_APP_ID': os.environ.get('SEED_ASR_APP_ID'),\n"
            "  'OPENAI_API_KEY': os.environ.get('OPENAI_API_KEY'),\n"
            "  'SPOOFED_UNDECLARED_SECRET': os.environ.get('SPOOFED_UNDECLARED_SECRET'),\n"
            "}\n"
            "outbox.write_text(json.dumps({'snap': snap, 'leak': os.environ.get('GEMINI_API_KEY', '')}), encoding='utf-8')\n"
            "print(json.dumps(snap))\n"
            "print('LEAK_STDOUT=' + os.environ.get('GEMINI_API_KEY', ''))\n"
            "print('LEAK_STDERR=' + os.environ.get('INTERNAL_SERVICE_TOKEN', ''), file=sys.stderr)\n"
        ),
        encoding="utf-8",
    )

    # Host bridge process env contains unrelated & unpassed credentials that MUST NOT leak.
    monkeypatch.setenv("OPENAI_API_KEY", "bridge-openai-leaked-9999")
    monkeypatch.setenv("SEED_ASR_APP_ID", "bridge-asr-should-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "bridge-gemini-stale-should-be-wiped")

    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = data_root
    tracked_procs: dict[int, tuple[str, str]] = {}

    def _ps_for_bridge() -> list[tuple[int, int, str, str]]:
        rows: list[tuple[int, int, str, str]] = []
        for p_id, (lstart, cmd) in list(tracked_procs.items()):
            rb._reap_pgid_zombies(p_id)
            try:
                os.killpg(p_id, 0)
            except ProcessLookupError:
                tracked_procs.pop(p_id, None)
                continue
            except PermissionError:
                pass
            rows.append((p_id, p_id, lstart, cmd))
        return rows

    orig_create_subproc = asyncio.create_subprocess_exec

    async def _wrapped_create_subproc(*args: Any, **kwargs: Any) -> Any:
        proc = await orig_create_subproc(*args, **kwargs)
        pgid = int(proc.pid)
        tracked_procs[pgid] = (
            "Sat Nov  7 10:00:00 2026",
            " ".join(str(a) for a in args),
        )
        return proc

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_wrapped_create_subproc),
    )

    bridge = rb.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(
        rb._list_host_processes_ps, side_effect=_ps_for_bridge
    )
    bridge._host_task_command_override = [sys.executable, str(probe_script)]

    vault_gem = "vault-gemini-real-12345"
    vault_svc = "vault-internal-tok-67890"
    spoof_val = "spoof-undeclared-val-999"

    with caplog.at_level(logging.WARNING, logger="researchdata_bridge"):
        code, payload = await bridge.handle_data_task_host_run(
            {
                "generation": 1,
                "run_id": "run-vault-e2e-1",
                "job_key": "podcast-weekly-pipeline",
                "host_mode": "run",
                "timeout": 30,
                "secrets": {
                    "GEMINI_API_KEY": vault_gem,
                    "INTERNAL_SERVICE_TOKEN": vault_svc,
                    "SPOOFED_UNDECLARED_SECRET": spoof_val,
                    "OPENAI_API_KEY": "spoof-openai-8888",
                },
            }
        )

    assert code == 200
    assert payload["ok"] is True

    # Verify child env snapshot printed as line 1 of stdout (safe values preserved, secrets absent/masked)
    first_line = payload["stdout"].splitlines()[0]
    snap = json.loads(first_line)
    assert snap["GEMINI_MATCHED_VAULT"] is True
    assert snap["SVC_MATCHED_VAULT"] is True
    assert snap["GEMINI_API_KEY"] == "***"
    assert snap["INTERNAL_SERVICE_TOKEN"] == "***"
    assert snap["DALTON_BACKEND_URL"] == "http://127.0.0.1:8000"
    assert snap["SEED_ASR_APP_ID"] is None
    assert snap["OPENAI_API_KEY"] is None
    assert snap["SPOOFED_UNDECLARED_SECRET"] is None

    # Stdout, stderr, log_tail, and on-disk outbox.json NEVER contain raw Vault secret values
    for field_name in ("stdout", "stderr", "log_tail"):
        assert vault_gem not in payload[field_name]
        assert vault_svc not in payload[field_name]
    assert "LEAK_STDOUT=***" in payload["stdout"]
    assert "LEAK_STDERR=***" in payload["stderr"]
    assert "LEAK_STDOUT=***" in payload["log_tail"]
    assert "LEAK_STDERR=***" in payload["log_tail"]

    outbox_text = (data_root / ".dalton-runs" / "run-vault-e2e-1" / "outbox.json").read_text(
        encoding="utf-8"
    )
    assert vault_gem not in outbox_text
    assert vault_svc not in outbox_text
    assert '"leak": "***"' in outbox_text

    # Undeclared & missing warnings log key names only, zero values
    warn_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "SPOOFED_UNDECLARED_SECRET" in warn_text
    assert "OPENAI_API_KEY" in warn_text
    assert "SEED_ASR_APP_ID" in warn_text
    assert spoof_val not in warn_text
    assert vault_gem not in warn_text
    assert vault_svc not in warn_text


def test_build_host_task_subprocess_env_blocks_bridge_secrets_and_smuggled_plain_env() -> None:
    built = bse.build_host_task_subprocess_env(
        {
            "PATH": "/usr/bin:/bin",
            "OPENAI_API_KEY": "bridge-openai",
            "FEISHU_APP_SECRET": "bridge-feishu",
            "INTERNAL_SERVICE_TOKEN": "bridge-internal-svc-tok",
            "DALTON_BRIDGE_ADMIN_TOKEN": "bridge-admin",
            "BASH_ENV": "/tmp/evil.sh",
            "GEMINI_API_KEY": "bridge-stale-gemini",
        },
        allowed_secrets={
            "GEMINI_API_KEY": "vault-gemini",
            "DALTON_BRIDGE_ADMIN_TOKEN": "cannot-reinject",
            "BASH_ENV": "cannot-reinject",
        },
        plain_env={
            "DALTON_BACKEND_URL": "http://127.0.0.1:8000",
            "OPENAI_API_KEY": "smuggled-via-env",
            "SEED_ASR_ACCESS_KEY": "smuggled-via-env",
            "INTERNAL_SERVICE_TOKEN": "smuggled-via-env",
            "PYTHONSTARTUP": "/tmp/evil.py",
        },
    )
    assert built["GEMINI_API_KEY"] == "vault-gemini"
    assert built["DALTON_BACKEND_URL"] == "http://127.0.0.1:8000"
    for blocked in (
        "OPENAI_API_KEY",
        "FEISHU_APP_SECRET",
        "INTERNAL_SERVICE_TOKEN",
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "BASH_ENV",
        "SEED_ASR_ACCESS_KEY",
        "PYTHONSTARTUP",
    ):
        assert blocked not in built


@pytest.mark.asyncio
async def test_undeclared_host_task_never_inherits_internal_service_token_or_missing_vault_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Vault secrets never fall back to bridge env, and undeclared tasks strip INTERNAL_SERVICE_TOKEN."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    probe = tmp_path / "probe_isolation.py"
    probe.write_text(
        (
            "import json, os\n"
            "print(json.dumps({\n"
            "  'INTERNAL_SERVICE_TOKEN': os.environ.get('INTERNAL_SERVICE_TOKEN'),\n"
            "  'SEED_ASR_ACCESS_KEY': os.environ.get('SEED_ASR_ACCESS_KEY'),\n"
            "  'GEMINI_API_KEY': os.environ.get('GEMINI_API_KEY'),\n"
            "  'DALTON_OUTBOX_FILE': os.environ.get('DALTON_OUTBOX_FILE'),\n"
            "}))\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "bridge-host-internal-token-secret")
    monkeypatch.setenv("SEED_ASR_ACCESS_KEY", "bridge-host-asr-key-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "bridge-host-gemini-secret")

    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = data_root
    tracked: dict[int, tuple[str, str]] = {}

    def _ps_rows() -> list[tuple[int, int, str, str]]:
        rows: list[tuple[int, int, str, str]] = []
        for pid, (lstart, cmd) in list(tracked.items()):
            rb._reap_pgid_zombies(pid)
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                tracked.pop(pid, None)
                continue
            rows.append((pid, pid, lstart, cmd))
        return rows

    orig_exec = asyncio.create_subprocess_exec

    async def _wrapped_exec(*args: Any, **kwargs: Any) -> Any:
        proc = await orig_exec(*args, **kwargs)
        tracked[int(proc.pid)] = ("Sat Nov  7 10:00:00 2026", " ".join(str(a) for a in args))
        return proc

    bridge = rb.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(
        rb._list_host_processes_ps, side_effect=_ps_rows
    )
    bridge._host_task_command_override = [sys.executable, str(probe)]

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_wrapped_exec),
    )

    # Case 1: undeclared task ("gs-pb-weekly-rundown" has no INTERNAL_SERVICE_TOKEN)
    code1, out1 = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-undecl-1",
            "job_key": "gs-pb-weekly-rundown",
            "host_mode": "run",
            "timeout": 30,
            "secrets": {"INTERNAL_SERVICE_TOKEN": "spoof-should-be-dropped"},
        }
    )
    assert code1 == 200 and out1["ok"] is True
    snap1 = json.loads(out1["stdout"].splitlines()[0])
    assert snap1["INTERNAL_SERVICE_TOKEN"] is None
    assert snap1["SEED_ASR_ACCESS_KEY"] is None
    assert snap1["GEMINI_API_KEY"] is None
    assert ".dalton-runs" not in (snap1["DALTON_OUTBOX_FILE"] or "")

    # Case 2: declared task ("podcast-weekly-pipeline") with empty Vault secrets must NOT fall back to bridge env
    code2, out2 = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-missing-vault-2",
            "job_key": "podcast-weekly-pipeline",
            "host_mode": "run",
            "timeout": 30,
            "secrets": {},
        }
    )
    assert code2 == 200 and out2["ok"] is True
    snap2 = json.loads(out2["stdout"].splitlines()[0])
    assert snap2["INTERNAL_SERVICE_TOKEN"] is None
    assert snap2["SEED_ASR_ACCESS_KEY"] is None
    assert snap2["GEMINI_API_KEY"] is None


@pytest.mark.asyncio
async def test_host_run_outbox_redaction_failure_blocks_delivery_and_hides_final_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outbox staging stays off persistent runs dir; redaction failure fails task and writes no final outbox."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    probe = tmp_path / "write_corrupt_outbox.py"
    probe.write_text(
        (
            "import os, pathlib\n"
            "p = pathlib.Path(os.environ['DALTON_OUTBOX_FILE'])\n"
            "assert '.dalton-runs' not in str(p), f'must stage off runs dir: {p}'\n"
            "p.write_text('{corrupt non-json with secret=vault-gem-leak-999', encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )

    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = data_root
    tracked: dict[int, tuple[str, str]] = {}

    def _ps_rows() -> list[tuple[int, int, str, str]]:
        rows: list[tuple[int, int, str, str]] = []
        for pid, (lstart, cmd) in list(tracked.items()):
            rb._reap_pgid_zombies(pid)
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                tracked.pop(pid, None)
                continue
            rows.append((pid, pid, lstart, cmd))
        return rows

    orig_exec = asyncio.create_subprocess_exec

    async def _wrapped_exec(*args: Any, **kwargs: Any) -> Any:
        proc = await orig_exec(*args, **kwargs)
        tracked[int(proc.pid)] = ("Sat Nov  7 10:00:00 2026", " ".join(str(a) for a in args))
        return proc

    bridge = rb.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(
        rb._list_host_processes_ps, side_effect=_ps_rows
    )
    bridge._host_task_command_override = [sys.executable, str(probe)]

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_wrapped_exec),
    )
    code, payload = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-outbox-fail-1",
            "job_key": "podcast-weekly-pipeline",
            "host_mode": "run",
            "timeout": 30,
            "secrets": {"GEMINI_API_KEY": "vault-gem-leak-999"},
        }
    )

    assert code == 500
    assert payload["ok"] is False
    assert payload["error"] == "outbox_redaction_failed"
    assert payload["outbox_redaction_failed"] is True
    assert "vault-gem-leak-999" not in json.dumps(payload, ensure_ascii=False)
    final_outbox = data_root / ".dalton-runs" / "run-outbox-fail-1" / "outbox.json"
    assert not final_outbox.exists()


@pytest.mark.asyncio
async def test_host_run_outbox_staging_allows_read_modify_write_append_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host outbox staging dir allows feishu_send_card read-modify-write append and removes temp dir after redaction."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    vault_secret = "vault-stage-append-secret-5566"

    probe = tmp_path / "append_cards_probe.py"
    probe.write_text(
        (
            "import json, os, pathlib, stat\n"
            "p = pathlib.Path(os.environ['DALTON_OUTBOX_FILE'])\n"
            "assert p.parent.name.startswith('.dalton-outbox-')\n"
            "assert stat.S_IMODE(p.parent.lstat().st_mode) == 0o700\n"
            f"data_root = pathlib.Path({str(data_root)!r}).resolve()\n"
            "assert not p.resolve().is_relative_to(data_root)\n"
            "# Card 1 write\n"
            "p.write_text(json.dumps({'feishu_cards': [{'title': 'Card 1'}]}), encoding='utf-8')\n"
            "# Card 2 feishu_send_card-style read-back + append (contains secret to verify redaction)\n"
            "cur = json.loads(p.read_text(encoding='utf-8'))\n"
            "cur['feishu_cards'].append({'title': 'Card 2', 'leak': os.environ['GEMINI_API_KEY']})\n"
            "p.write_text(json.dumps(cur), encoding='utf-8')\n"
            "print(str(p.parent))\n"
        ),
        encoding="utf-8",
    )

    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = data_root
    tracked: dict[int, tuple[str, str]] = {}

    def _ps_rows() -> list[tuple[int, int, str, str]]:
        rows: list[tuple[int, int, str, str]] = []
        for pid, (lstart, cmd) in list(tracked.items()):
            rb._reap_pgid_zombies(pid)
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                tracked.pop(pid, None)
                continue
            rows.append((pid, pid, lstart, cmd))
        return rows

    orig_exec = asyncio.create_subprocess_exec

    async def _wrapped_exec(*args: Any, **kwargs: Any) -> Any:
        proc = await orig_exec(*args, **kwargs)
        tracked[int(proc.pid)] = ("Sat Nov  7 10:00:00 2026", " ".join(str(a) for a in args))
        return proc

    bridge = rb.ResearchDataBridge(cfg)
    bridge._host_ps_list_fn = create_autospec(rb._list_host_processes_ps, side_effect=_ps_rows)
    bridge._host_task_command_override = [sys.executable, str(probe)]

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_wrapped_exec),
    )
    code, payload = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-stage-append-1",
            "job_key": "podcast-weekly-pipeline",
            "host_mode": "run",
            "timeout": 30,
            "secrets": {"GEMINI_API_KEY": vault_secret},
        }
    )
    assert code == 200 and payload["ok"] is True

    staging_dir_reported = Path(payload["stdout"].strip().splitlines()[0])
    assert not staging_dir_reported.exists()
    final_outbox = data_root / ".dalton-runs" / "run-stage-append-1" / "outbox.json"
    assert final_outbox.is_file()
    final_text = final_outbox.read_text(encoding="utf-8")
    assert vault_secret not in final_text
    parsed = json.loads(final_text)
    assert len(parsed["feishu_cards"]) == 2
    assert parsed["feishu_cards"][0]["title"] == "Card 1"
    assert parsed["feishu_cards"][1] == {"title": "Card 2", "leak": "***"}


def test_cleanup_stale_host_outbox_staging_dirs_only_removes_owned_prefix_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup cleanup removes only owned `.dalton-outbox-*` directories without following symlinks."""
    import tempfile

    fake_tmp = tmp_path / "tmp_root"
    fake_tmp.mkdir()
    stale_dir = fake_tmp / ".dalton-outbox-crashed123"
    stale_dir.mkdir()
    (stale_dir / "outbox.json").write_text('{"secret": "leftover"}', encoding="utf-8")

    # Different prefix / plain file MUST stay untouched
    keep_dir = fake_tmp / "other-workdir"
    keep_dir.mkdir()
    keep_file = fake_tmp / ".dalton-outbox-plainfile"
    keep_file.write_text("keep me", encoding="utf-8")

    # Symlink with matching prefix MUST NOT delete target dir
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    (victim_dir / "precious.txt").write_text("keep target", encoding="utf-8")
    sym_dir = fake_tmp / ".dalton-outbox-symlink"
    sym_dir.symlink_to(victim_dir)

    # Dir owned by another uid MUST stay untouched
    other_uid_dir = fake_tmp / ".dalton-outbox-otheruid"
    other_uid_dir.mkdir()

    real_lstat = Path.lstat

    def _fake_path_lstat(self: Path) -> Any:
        st = real_lstat(self)
        if self == other_uid_dir:
            fake_st = list(st)
            fake_st[4] = (os.getuid() + 1000) if hasattr(os, "getuid") else 99999
            return os.stat_result(fake_st)
        return st

    fake_shm = tmp_path / "shm"
    fake_shm.mkdir()
    monkeypatch.setattr(Path, "lstat", create_autospec(Path.lstat, side_effect=_fake_path_lstat))
    monkeypatch.setattr(tempfile, "gettempdir", create_autospec(tempfile.gettempdir, return_value=str(fake_tmp)))
    monkeypatch.setattr(rb, "_HOST_OUTBOX_CLEANUP_EXTRA_DIRS", (str(fake_shm),))

    # A fresh directory may belong to a live run (or another process sharing $TMPDIR): kept.
    assert rb._cleanup_stale_host_outbox_staging_dirs() == 0
    assert stale_dir.is_dir()

    old = time.time() - rb._HOST_OUTBOX_STALE_AGE_S - 60
    os.utime(stale_dir, (old, old))
    os.utime(other_uid_dir, (old, old))
    removed = rb._cleanup_stale_host_outbox_staging_dirs()
    assert removed == 1
    assert not stale_dir.exists()
    assert keep_dir.is_dir()
    assert keep_file.is_file()
    assert sym_dir.is_symlink()
    assert (victim_dir / "precious.txt").is_file()
    assert other_uid_dir.is_dir()



