"""Unit tests for Dalton usage collector, pointer resolution, launchd switch script, and missing OpenClaw sources."""

from __future__ import annotations

import datetime as dt
import fcntl
import importlib.util
import io
import json
import os
import plistlib
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "usage_collector.py"
COLLECTOR_RUN_SH_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "usage_collector_run.sh"
SWITCH_SH_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "usage_collector_switch.sh"
PLIST_TEMPLATE_PATH = REPO_ROOT / "scripts" / "dalton" / "launchd" / "com.dalton.usage-collector.plist.template"
SKILL_DIR = REPO_ROOT / "scripts" / "dalton" / "data_assets" / "skills" / "model-usage-cost"
SKILL_SCRIPTS_DIR = SKILL_DIR / "scripts"
TRACKER_PATH = REPO_ROOT / "scripts" / "dalton" / "data_assets" / "scripts" / "token_weekly_usage_tracker.py"


def _load_module(name: str, path: Path):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_exec_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ---------------------------------------------------------------------------
# 1. Shared state-dir pointer & weekly tracker consistency
# ---------------------------------------------------------------------------


def test_pointer_resolution_priority_and_shared_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli
    import usage_state

    collector = _load_module("dalton_usage_collector_test", COLLECTOR_PATH)
    tracker = _load_module("dalton_token_weekly_usage_tracker_test", TRACKER_PATH)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DALTON_USAGE_STATE_DIR", raising=False)
    monkeypatch.delenv("DALTON_USAGE_STATE_POINTER_FILE", raising=False)

    legacy_dir = fake_home / ".openclaw" / "usage-accounting"
    # 1. Pointer absent -> legacy ~/.openclaw/usage-accounting across all three readers
    assert usage_state.resolve_usage_state_dir() == legacy_dir
    assert cli.default_state_dir() == legacy_dir
    assert tracker.resolve_state_dir() == legacy_dir
    assert collector.resolve_state_dir() == legacy_dir

    # 2. Pointer present -> pointed dir across all three readers (mode 0600)
    pointed_dir = tmp_path / "custom_dalton_state"
    ptr_file = usage_state.write_usage_state_pointer(pointed_dir)
    assert ptr_file == fake_home / ".config" / "dalton" / "usage-state-dir"
    assert stat.S_IMODE(ptr_file.stat().st_mode) == 0o600
    assert ptr_file.read_text(encoding="utf-8") == f"{pointed_dir.resolve()}\n"

    assert usage_state.resolve_usage_state_dir() == pointed_dir.resolve()
    assert cli.default_state_dir() == pointed_dir.resolve()
    assert tracker.resolve_state_dir() == pointed_dir.resolve()
    assert collector.resolve_state_dir() == pointed_dir.resolve()

    # 3. Env var DALTON_USAGE_STATE_DIR takes precedence over pointer
    env_dir = tmp_path / "env_override_state"
    monkeypatch.setenv("DALTON_USAGE_STATE_DIR", str(env_dir))
    assert usage_state.resolve_usage_state_dir() == env_dir
    assert cli.default_state_dir() == env_dir
    assert tracker.resolve_state_dir() == env_dir
    assert collector.resolve_state_dir() == env_dir


def test_weekly_tracker_status_check_and_summary_cmd_use_same_pointer_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import usage_state

    tracker = _load_module("dalton_token_weekly_usage_tracker_test", TRACKER_PATH)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DALTON_USAGE_STATE_DIR", raising=False)
    monkeypatch.delenv("DALTON_USAGE_STATE_POINTER_FILE", raising=False)

    pointed_dir = tmp_path / "active_accounting"
    pointed_dir.mkdir(parents=True)
    usage_state.write_usage_state_pointer(pointed_dir)

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    (pointed_dir / "collector-status.json").write_text(
        json.dumps({"ok": True, "collected_at": now_iso}),
        encoding="utf-8",
    )

    captured_env: dict[str, str] = {}

    def fake_run(cmd, *, capture_output, text, check, env=None):
        captured_env.update(env or {})
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "version": "3.1.0",
                    "price_catalog": {"generated": "2026-11-14", "effective_notes": []},
                    "dalton_prod": {"ok": True, "runs": 0, "calls": 0, "models": 0},
                    "windows": [
                        {
                            "days": 7.0,
                            "from": "2026-11-11T00:00:00+00:00",
                            "to": "2026-11-18T00:00:00+00:00",
                            "rows": [],
                            "totals": {
                                "tokens": 0,
                                "records": 0,
                                "calls": 0,
                                "priced_cost_usd": 0.0,
                                "unpriced_tokens": 0,
                            },
                        }
                    ],
                    "coverage": {"issues": {}},
                }
            ),
            stderr="",
        )

    mock_run = create_autospec(subprocess.run, side_effect=fake_run)
    with patch.object(tracker.subprocess, "run", mock_run):
        raw_data = tracker.fetch_usage_data(days=7)

    assert captured_env["DALTON_USAGE_STATE_DIR"] == str(pointed_dir.resolve())
    stats = tracker.process_usage_stats(raw_data)
    assert stats["collector_status"]["present"] is True
    assert stats["collector_status"]["ok"] is True
    assert stats["collector_status"]["stale"] is False


# ---------------------------------------------------------------------------
# 2. Collector unit tests (flock, timeout, consecutive_failures, atomic write, Python selection)
# ---------------------------------------------------------------------------


def test_collector_flock_skips_reentrant_run_without_touching_status(tmp_path: Path) -> None:
    collector = _load_module("dalton_usage_collector_test", COLLECTOR_PATH)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    status_path = state_dir / "collector-status.json"
    prior_status = {"ok": True, "collected_at": "2026-11-18T09:00:00+00:00", "consecutive_failures": 0}
    status_path.write_text(json.dumps(prior_status), encoding="utf-8")

    lock_path = state_dir / "collector.lock"
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        mock_run = create_autospec(subprocess.run)
        rc = collector.collect_once(
            skill_dir=SKILL_DIR,
            state_dir=state_dir,
            run_fn=mock_run,
        )
        assert rc == 0
        mock_run.assert_not_called()
        assert json.loads(status_path.read_text(encoding="utf-8")) == prior_status
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_collector_timeout_records_failure_increments_counter_and_resets_on_ok(tmp_path: Path) -> None:
    collector = _load_module("dalton_usage_collector_test", COLLECTOR_PATH)
    state_dir = tmp_path / "state"
    fake_skill = tmp_path / "skill"
    (fake_skill / "scripts").mkdir(parents=True)
    (fake_skill / "scripts" / "model_usage_cost_cli.py").write_text("# stub\n", encoding="utf-8")
    fake_hb_py = _make_exec_file(tmp_path / "homebrew" / "python3.12")

    t0 = dt.datetime(2026, 11, 18, 9, 0, 0, tzinfo=dt.timezone.utc)
    t1 = dt.datetime(2026, 11, 18, 9, 1, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 11, 18, 9, 2, 0, tzinfo=dt.timezone.utc)
    t3 = dt.datetime(2026, 11, 18, 9, 3, 0, tzinfo=dt.timezone.utc)

    ok_proc = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"ok": True, "observations": 4, "issues": {}, "inventory": {}}),
        stderr="",
    )
    run_ok = create_autospec(subprocess.run, return_value=ok_proc)
    rc0 = collector.collect_once(
        skill_dir=fake_skill,
        state_dir=state_dir,
        homebrew_python=fake_hb_py,
        run_fn=run_ok,
        now_fn=lambda: t0,
    )
    assert rc0 == 0
    assert run_ok.call_args.kwargs["timeout"] == 120
    assert run_ok.call_args.args[0] == [
        str(fake_hb_py),
        str(fake_skill / "scripts" / "model_usage_cost_cli.py"),
        "collect-broker",
        "--days",
        "2",
        "--json",
    ]
    assert run_ok.call_args.kwargs["env"]["DALTON_USAGE_STATE_DIR"] == str(state_dir)

    status_path = state_dir / "collector-status.json"
    st0 = json.loads(status_path.read_text(encoding="utf-8"))
    assert st0["ok"] is True
    assert st0["consecutive_failures"] == 0
    assert st0["last_ok_at"] == t0.isoformat()
    assert st0["collected_at"] == t0.isoformat()
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600

    run_timeout = create_autospec(
        subprocess.run,
        side_effect=subprocess.TimeoutExpired(cmd="collect-broker", timeout=120),
    )
    rc1 = collector.collect_once(
        skill_dir=fake_skill,
        state_dir=state_dir,
        homebrew_python=fake_hb_py,
        run_fn=run_timeout,
        now_fn=lambda: t1,
    )
    assert rc1 == 1
    st1 = json.loads(status_path.read_text(encoding="utf-8"))
    assert st1["ok"] is False
    assert st1["consecutive_failures"] == 1
    assert st1["last_ok_at"] == t0.isoformat()
    assert st1["collected_at"] == t1.isoformat()
    assert "timed out" in st1["error"]

    rc2 = collector.collect_once(
        skill_dir=fake_skill,
        state_dir=state_dir,
        homebrew_python=fake_hb_py,
        run_fn=run_timeout,
        now_fn=lambda: t2,
    )
    assert rc2 == 1
    st2 = json.loads(status_path.read_text(encoding="utf-8"))
    assert st2["ok"] is False
    assert st2["consecutive_failures"] == 2
    assert st2["last_ok_at"] == t0.isoformat()
    assert st2["collected_at"] == t2.isoformat()

    rc3 = collector.collect_once(
        skill_dir=fake_skill,
        state_dir=state_dir,
        homebrew_python=fake_hb_py,
        run_fn=run_ok,
        now_fn=lambda: t3,
    )
    assert rc3 == 0
    st3 = json.loads(status_path.read_text(encoding="utf-8"))
    assert st3["ok"] is True
    assert st3["consecutive_failures"] == 0
    assert st3["last_ok_at"] == t3.isoformat()


def test_collector_atomic_write_uses_temp_replace_and_0600(tmp_path: Path) -> None:
    collector = _load_module("dalton_usage_collector_test", COLLECTOR_PATH)
    state_dir = tmp_path / "atomic_state"
    status_path = state_dir / "collector-status.json"

    replace_spy = create_autospec(os.replace, wraps=os.replace)
    with patch.object(collector.os, "replace", replace_spy):
        collector.write_status_atomic(status_path, {"ok": True, "consecutive_failures": 0})

    assert replace_spy.call_count == 1
    src_tmp, dst_target = replace_spy.call_args.args
    assert Path(src_tmp).parent == state_dir
    assert Path(src_tmp).name.startswith(".collector-status-")
    assert Path(dst_target) == status_path
    assert status_path.is_file()
    assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
    assert list(state_dir.glob(".collector-status-*")) == []


def test_collector_python_selection_and_bash_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = _load_module("dalton_usage_collector_test", COLLECTOR_PATH)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    hb_py = _make_exec_file(tmp_path / "hb" / "python3.12")
    monkeypatch.delenv("DALTON_USAGE_PYTHON", raising=False)

    assert collector.resolve_skill_python(skill_dir, homebrew_python=hb_py) == str(hb_py)

    venv_py = _make_exec_file(skill_dir / "venv" / "bin" / "python")
    assert collector.resolve_skill_python(skill_dir, homebrew_python=hb_py) == str(venv_py)

    # DALTON_USAGE_PYTHON takes priority over both skill venv and Homebrew 3.12
    override_py = _make_exec_file(tmp_path / "override" / "python3.12")
    monkeypatch.setenv("DALTON_USAGE_PYTHON", str(override_py))
    assert collector.resolve_skill_python(skill_dir, homebrew_python=hb_py) == str(override_py)
    monkeypatch.delenv("DALTON_USAGE_PYTHON", raising=False)

    empty_skill = tmp_path / "empty_skill"
    empty_skill.mkdir()
    with pytest.raises(FileNotFoundError):
        collector.resolve_skill_python(empty_skill, homebrew_python=tmp_path / "missing_py312")
    with pytest.raises(FileNotFoundError):
        collector.resolve_skill_python(empty_skill, homebrew_python=Path("/usr/bin/python3"))

    # Bash wrapper falls back cleanly and fails when neither venv nor Homebrew 3.12 exists
    missing_env = {
        "PATH": "/usr/bin:/bin",
        "DALTON_REPO_ROOT": str(REPO_ROOT),
        "DALTON_USAGE_SKILL_DIR": str(empty_skill),
        "DALTON_HOMEBREW_PYTHON_312": str(tmp_path / "missing_py312"),
    }
    res_missing = subprocess.run(
        ["/bin/bash", str(COLLECTOR_RUN_SH_PATH)],
        env=missing_env,
        capture_output=True,
        text=True,
    )
    assert res_missing.returncode != 0
    assert "refusing /usr/bin/python3" in res_missing.stderr


# ---------------------------------------------------------------------------
# 3. Switch script & plist template tests
# ---------------------------------------------------------------------------


def _create_sqlite_db(path: Path, rows: list[tuple[str, float, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS usage_evidence (id TEXT PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL)")
    for row in rows:
        conn.execute("INSERT OR REPLACE INTO usage_evidence VALUES (?, ?, ?)", row)
    conn.commit()
    conn.close()


def _setup_switch_sandbox(
    tmp_path: Path,
    *,
    with_src_db: bool = True,
    corrupt_db: bool = False,
    custom_dst_name: str = "dalton_state",
):
    src_state = tmp_path / "openclaw_state"
    dst_state = tmp_path / custom_dst_name
    pointer_file = tmp_path / "config" / "dalton" / "usage-state-dir"
    launch_agents = tmp_path / "LaunchAgents"
    logs_dir = tmp_path / "Logs"
    stub_dir = tmp_path / "stubs"
    src_state.mkdir(parents=True)
    launch_agents.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    stub_dir.mkdir(parents=True)

    old_label = "ai.openclaw.model-usage-collector"
    new_label = "com.dalton.usage-collector"
    old_plist = launch_agents / f"{old_label}.plist"
    old_plist.write_text("<plist version='1.0'><dict/></plist>", encoding="utf-8")

    if with_src_db:
        src_db = src_state / "evidence.sqlite"
        if corrupt_db:
            src_db.write_bytes(b"not-a-valid-sqlite-database")
        else:
            _create_sqlite_db(src_db, [("r1", 1700000000.0, '{"v":[1,0,0,0]}')])
        (src_state / "collector-status.json").write_text(
            json.dumps({"ok": True, "collected_at": "2026-11-18T08:00:00+00:00"}),
            encoding="utf-8",
        )

    launchctl_log = tmp_path / "launchctl.log"
    loaded_file = tmp_path / "loaded_labels.txt"
    loaded_file.write_text(f"{old_label}\n", encoding="utf-8")

    launchctl_stub = _make_stub(
        stub_dir / "launchctl",
        f"""
echo "$*" >> "{launchctl_log}"
cmd="${{1:-}}"
target="${{2:-}}"
fail_mode="${{STUB_LAUNCHCTL_FAIL:-}}"
if [[ -n "${{fail_mode}}" && "$*" == *"${{fail_mode}}"* ]]; then
  exit 1
fi
case "${{cmd}}" in
  print)
    label="${{target##*/}}"
    if grep -Fxq "${{label}}" "{loaded_file}" 2>/dev/null; then
      echo "gui/501/${{label}} = {{"
      exit 0
    fi
    exit 113
    ;;
  bootout)
    label="${{target##*/}}"
    grep -Fxv "${{label}}" "{loaded_file}" > "{loaded_file}.tmp" || true
    mv "{loaded_file}.tmp" "{loaded_file}"
    exit 0
    ;;
  bootstrap)
    plist_arg="${{3:-}}"
    label="$(basename "${{plist_arg}}" .plist)"
    if ! grep -Fxq "${{label}}" "{loaded_file}" 2>/dev/null; then
      echo "${{label}}" >> "{loaded_file}"
    fi
    python_bin="${{DALTON_USAGE_PYTHON:-/opt/homebrew/bin/python3.12}}"
    if [[ "${{label}}" == "{new_label}" ]]; then
      if [[ -n "${{STUB_NEW_RECORD_ID:-}}" ]]; then
        "${{python_bin}}" - "${{DALTON_USAGE_STATE_DIR}}/evidence.sqlite" "${{STUB_NEW_RECORD_ID}}" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("INSERT OR REPLACE INTO usage_evidence VALUES (?, ?, ?)", (sys.argv[2], 1700000100.0, '{{"v":[5,0,0,0]}}'))
conn.commit()
conn.close()
PY
      fi
      if [[ "${{STUB_WRITE_NEW_READY:-1}}" == "1" ]]; then
        "${{python_bin}}" - "${{DALTON_USAGE_STATE_DIR}}/collector-status.json" <<'PY'
import datetime, json, sys
from pathlib import Path
p = Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
now_iso = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2)).isoformat()
p.write_text(json.dumps({{"ok": True, "collected_at": now_iso, "consecutive_failures": 0}}), encoding="utf-8")
PY
      fi
    elif [[ "${{label}}" == "{old_label}" && "${{STUB_WRITE_OLD_READY:-1}}" == "1" ]]; then
      "${{python_bin}}" - "${{DALTON_USAGE_SRC_STATE_DIR}}/collector-status.json" <<'PY'
import datetime, json, sys
from pathlib import Path
p = Path(sys.argv[1])
p.parent.mkdir(parents=True, exist_ok=True)
now_iso = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2)).isoformat()
p.write_text(json.dumps({{"ok": True, "collected_at": now_iso, "consecutive_failures": 0}}), encoding="utf-8")
PY
    fi
    exit 0
    ;;
  enable|disable)
    exit 0
    ;;
esac
""",
    )

    plutil_stub = _make_stub(
        stub_dir / "plutil",
        """
if [[ "${1:-}" == "-lint" && -f "${2:-}" ]]; then
  exit 0
fi
exit 1
""",
    )

    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DALTON_REPO_ROOT": str(REPO_ROOT),
        "DALTON_USAGE_SRC_STATE_DIR": str(src_state),
        "DALTON_USAGE_STATE_DIR": str(dst_state),
        "DALTON_USAGE_STATE_POINTER_FILE": str(pointer_file),
        "DALTON_LAUNCH_AGENTS_DIR": str(launch_agents),
        "DALTON_LOGS_DIR": str(logs_dir),
        "DALTON_USAGE_PYTHON": sys.executable,
        "LAUNCHCTL_BIN": str(launchctl_stub),
        "PLUTIL_BIN": str(plutil_stub),
        "DALTON_LAUNCHD_UID": "501",
        "USAGE_COLLECTOR_UNLOAD_TIMEOUT_S": "3",
        "USAGE_COLLECTOR_HEALTH_TIMEOUT_S": "3",
        "USAGE_COLLECTOR_HEALTH_INTERVAL_S": "0.1",
    }
    return {
        "src_state": src_state,
        "dst_state": dst_state,
        "pointer_file": pointer_file,
        "launch_agents": launch_agents,
        "logs_dir": logs_dir,
        "old_plist": old_plist,
        "new_plist": launch_agents / f"{new_label}.plist",
        "launchctl_log": launchctl_log,
        "loaded_file": loaded_file,
        "env": env,
    }


def test_switch_plan_mode_does_not_write_and_checks_python(tmp_path: Path) -> None:
    ctx = _setup_switch_sandbox(tmp_path)
    res_default = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH)], env=ctx["env"], capture_output=True, text=True)
    res_plan = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--plan"], env=ctx["env"], capture_output=True, text=True)
    assert res_default.returncode == 0, res_default.stderr
    assert res_plan.returncode == 0, res_plan.stderr
    assert "Switch Plan (dry-run)" in res_plan.stdout
    assert "ai.openclaw.model-usage-collector (loaded" in res_plan.stdout
    assert "wait <=300s" in res_plan.stdout or "wait <=3s" in res_plan.stdout
    assert not ctx["dst_state"].exists()
    assert not ctx["new_plist"].exists()
    assert not ctx["pointer_file"].exists()
    log_lines = ctx["launchctl_log"].read_text(encoding="utf-8").splitlines()
    assert all(line.startswith("print ") for line in log_lines)

    # --plan fails early when collector python is missing
    missing_py_env = {
        **ctx["env"],
        "DALTON_USAGE_SKILL_DIR": str(tmp_path / "no_skill"),
        "DALTON_HOMEBREW_PYTHON_312": str(tmp_path / "missing_py312"),
    }
    missing_py_env.pop("DALTON_USAGE_PYTHON", None)
    res_bad_py = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--plan"], env=missing_py_env, capture_output=True, text=True)
    assert res_bad_py.returncode != 0
    assert "collector Python not found" in res_bad_py.stderr


def test_switch_apply_custom_dir_plist_idempotent_and_force_overwrite(tmp_path: Path) -> None:
    ctx = _setup_switch_sandbox(tmp_path, custom_dst_name="custom/everflow-usage")
    res = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=ctx["env"], capture_output=True, text=True)
    assert res.returncode == 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"

    dst_db = ctx["dst_state"] / "evidence.sqlite"
    dst_status = ctx["dst_state"] / "collector-status.json"
    assert dst_db.is_file()
    assert dst_status.is_file()
    assert stat.S_IMODE(dst_db.stat().st_mode) == 0o600
    assert stat.S_IMODE(dst_status.stat().st_mode) == 0o600
    assert stat.S_IMODE(ctx["pointer_file"].stat().st_mode) == 0o600
    assert ctx["pointer_file"].read_text(encoding="utf-8").strip() == str(ctx["dst_state"].resolve())

    # Rendered plist uses bash wrapper and custom target DALTON_USAGE_STATE_DIR
    plist_bytes = ctx["new_plist"].read_bytes()
    plist_data = plistlib.loads(plist_bytes)
    assert plist_data["Label"] == "com.dalton.usage-collector"
    assert plist_data["StartInterval"] == 60
    assert plist_data["Nice"] == 10
    assert plist_data["ProgramArguments"] == [
        "/bin/bash",
        str(REPO_ROOT / "scripts" / "dalton" / "ops" / "usage_collector_run.sh"),
    ]
    assert plist_data["EnvironmentVariables"]["DALTON_USAGE_STATE_DIR"] == str(ctx["dst_state"].resolve())
    assert plist_data["EnvironmentVariables"]["DALTON_USAGE_PYTHON"] == sys.executable
    for placeholder in ("__REPO__", "__HOME__", "__LOG_DIR__", "__STATE_DIR__", "__USAGE_PYTHON__"):
        assert placeholder not in plist_bytes.decode("utf-8")

    # Re-running --apply when already switched is a no-op (exit 0, no launchctl bootout/bootstrap)
    before_log = ctx["launchctl_log"].read_text(encoding="utf-8")
    res_again = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=ctx["env"], capture_output=True, text=True)
    assert res_again.returncode == 0
    assert "已切换" in res_again.stdout
    after_log = ctx["launchctl_log"].read_text(encoding="utf-8")
    added_lines = after_log[len(before_log):].splitlines()
    assert all(line.startswith("print ") for line in added_lines)

    # Simulate unswitched state while dst_state already has a distinct evidence.sqlite
    ctx["pointer_file"].unlink()
    ctx["loaded_file"].write_text("ai.openclaw.model-usage-collector\n", encoding="utf-8")
    dst_db.unlink()
    _create_sqlite_db(dst_db, [("old_dst_row", 1700000050.0, '{"v":[9,0,0,0]}')])

    # Without --force -> refused, old collector NOT touched
    res_refuse = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=ctx["env"], capture_output=True, text=True)
    assert res_refuse.returncode != 0
    assert "--force" in res_refuse.stderr
    assert "ai.openclaw.model-usage-collector" in ctx["loaded_file"].read_text(encoding="utf-8")

    # With --force -> old dst_db renamed to evidence.sqlite.pre-apply.<ts> and new copy succeeds
    res_force = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply", "--force"], env=ctx["env"], capture_output=True, text=True)
    assert res_force.returncode == 0, res_force.stderr
    pre_apply_files = list(ctx["dst_state"].glob("evidence.sqlite.pre-apply.*"))
    assert len(pre_apply_files) == 1
    conn_bak = sqlite3.connect(pre_apply_files[0])
    assert conn_bak.execute("SELECT id FROM usage_evidence").fetchall() == [("old_dst_row",)]
    conn_bak.close()
    conn_new = sqlite3.connect(dst_db)
    assert conn_new.execute("SELECT id FROM usage_evidence").fetchall() == [("r1",)]
    conn_new.close()


def test_switch_apply_failure_after_pointer_copies_new_records_back_and_preserves_pre_rollback(tmp_path: Path) -> None:
    # Step 5 fails: new collector inserted 'r_new_during_cutover' into dst_db but did not write ok:true
    ctx = _setup_switch_sandbox(tmp_path / "post_ptr")
    env = {
        **ctx["env"],
        "STUB_WRITE_NEW_READY": "0",
        "STUB_NEW_RECORD_ID": "r_new_during_cutover",
    }
    res = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    assert "step2_copy_back:ok" in res.stderr
    assert "step3_restore_pointer:ok" in res.stderr
    assert "step4_restore_old:ok" in res.stderr

    # Old ledger now contains BOTH r1 and r_new_during_cutover!
    conn_src = sqlite3.connect(ctx["src_state"] / "evidence.sqlite")
    ids = {row[0] for row in conn_src.execute("SELECT id FROM usage_evidence").fetchall()}
    conn_src.close()
    assert ids == {"r1", "r_new_during_cutover"}

    # Original old ledger was preserved as evidence.sqlite.pre-rollback.<ts>
    pre_rb = list(ctx["src_state"].glob("evidence.sqlite.pre-rollback.*"))
    assert len(pre_rb) == 1
    conn_bak = sqlite3.connect(pre_rb[0])
    assert conn_bak.execute("SELECT id FROM usage_evidence").fetchall() == [("r1",)]
    conn_bak.close()

    # Pointer restored to old directory & new directory files preserved
    assert ctx["pointer_file"].read_text(encoding="utf-8").strip() == str(ctx["src_state"].resolve())
    assert (ctx["dst_state"] / "evidence.sqlite").is_file()
    assert "ai.openclaw.model-usage-collector" in ctx["loaded_file"].read_text(encoding="utf-8")


def test_switch_apply_failure_before_pointer_skips_copy_back_and_reports_step_failures(tmp_path: Path) -> None:
    # Step 2 fails (corrupt source DB) AND restore of old fails (missing old plist)
    ctx = _setup_switch_sandbox(tmp_path / "pre_ptr", corrupt_db=True)
    ctx["old_plist"].unlink()
    res = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=ctx["env"], capture_output=True, text=True)
    assert res.returncode != 0
    assert "step2_copy_back:skipped" in res.stderr
    assert "step4_restore_old:failed(ai.openclaw.model-usage-collector)" in res.stderr
    assert not ctx["pointer_file"].exists()
    assert list(ctx["src_state"].glob("evidence.sqlite.pre-rollback.*")) == []


def test_switch_rollback_uses_pointer_without_env_and_restores_ledger(tmp_path: Path) -> None:
    ctx = _setup_switch_sandbox(tmp_path, custom_dst_name="custom/switched-dir")
    env_apply = {**ctx["env"], "STUB_NEW_RECORD_ID": "r_added_in_dalton"}
    res_apply = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=env_apply, capture_output=True, text=True)
    assert res_apply.returncode == 0, res_apply.stderr

    # Run --rollback WITHOUT DALTON_USAGE_STATE_DIR in env: must find custom/switched-dir from pointer
    rb_env = {k: v for k, v in ctx["env"].items() if k != "DALTON_USAGE_STATE_DIR"}
    res_rb = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--rollback"], env=rb_env, capture_output=True, text=True)
    assert res_rb.returncode == 0, res_rb.stderr
    assert "step1_stop_new:ok(com.dalton.usage-collector)" in res_rb.stderr
    assert f"step2_copy_back:ok({ctx['dst_state'].resolve()}->{ctx['src_state'].resolve()})" in res_rb.stderr
    assert "step3_restore_pointer:ok" in res_rb.stderr
    assert "step4_restore_old:ok(ai.openclaw.model-usage-collector)" in res_rb.stderr

    # New record added while on Dalton is present in restored old ledger
    conn_src = sqlite3.connect(ctx["src_state"] / "evidence.sqlite")
    ids = {row[0] for row in conn_src.execute("SELECT id FROM usage_evidence").fetchall()}
    conn_src.close()
    assert ids == {"r1", "r_added_in_dalton"}

    # Original old ledger preserved as evidence.sqlite.pre-rollback.<ts>
    pre_rb = list(ctx["src_state"].glob("evidence.sqlite.pre-rollback.*"))
    assert len(pre_rb) == 1
    conn_bak = sqlite3.connect(pre_rb[0])
    assert conn_bak.execute("SELECT id FROM usage_evidence").fetchall() == [("r1",)]
    conn_bak.close()

    # Pointer restored and new directory preserved
    assert ctx["pointer_file"].read_text(encoding="utf-8").strip() == str(ctx["src_state"].resolve())
    assert (ctx["dst_state"] / "evidence.sqlite").is_file()
    assert "ai.openclaw.model-usage-collector" in ctx["loaded_file"].read_text(encoding="utf-8")

    # Running --rollback again when pointer already points to old dir skips copy back
    res_rb2 = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--rollback"], env=rb_env, capture_output=True, text=True)
    assert res_rb2.returncode == 0, res_rb2.stderr
    assert "step2_copy_back:skipped(not_switched)" in res_rb2.stderr


def test_switch_rollback_halts_subsequent_steps_when_step1_step2_or_step3_fails(tmp_path: Path) -> None:
    # Case 1: Rollback step 1 (bootout new collector) fails -> steps 2/3/4 skipped, old collector NOT started
    ctx1 = _setup_switch_sandbox(tmp_path / "rb_s1")
    res_a1 = subprocess.run(
        ["/bin/bash", str(SWITCH_SH_PATH), "--apply"],
        env={**ctx1["env"], "STUB_NEW_RECORD_ID": "r_new_1"},
        capture_output=True,
        text=True,
    )
    assert res_a1.returncode == 0, res_a1.stderr
    before_log1 = ctx1["launchctl_log"].read_text(encoding="utf-8")
    res_rb1 = subprocess.run(
        ["/bin/bash", str(SWITCH_SH_PATH), "--rollback"],
        env={**ctx1["env"], "STUB_LAUNCHCTL_FAIL": "bootout gui/501/com.dalton.usage-collector"},
        capture_output=True,
        text=True,
    )
    assert res_rb1.returncode != 0
    assert "step1_stop_new:failed(com.dalton.usage-collector)" in res_rb1.stderr
    assert "step2_copy_back:skipped(step1_failed)" in res_rb1.stderr
    assert "step3_restore_pointer:skipped(step1_failed)" in res_rb1.stderr
    assert "step4_restore_old:skipped(step1_failed)" in res_rb1.stderr
    added1 = ctx1["launchctl_log"].read_text(encoding="utf-8")[len(before_log1):]
    assert "enable gui/501/ai.openclaw.model-usage-collector" not in added1
    assert "bootstrap gui/501" not in added1
    assert ctx1["pointer_file"].read_text(encoding="utf-8").strip() == str(ctx1["dst_state"].resolve())
    assert list(ctx1["src_state"].glob("evidence.sqlite.pre-rollback.*")) == []

    # Case 2: Rollback step 2 (corrupt new DB backup()) fails -> steps 3/4 skipped, pointer stays at new dir
    ctx2 = _setup_switch_sandbox(tmp_path / "rb_s2")
    res_a2 = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=ctx2["env"], capture_output=True, text=True)
    assert res_a2.returncode == 0, res_a2.stderr
    (ctx2["dst_state"] / "evidence.sqlite").write_bytes(b"corrupted-new-ledger")
    before_log2 = ctx2["launchctl_log"].read_text(encoding="utf-8")
    res_rb2 = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--rollback"], env=ctx2["env"], capture_output=True, text=True)
    assert res_rb2.returncode != 0
    assert "step1_stop_new:ok(com.dalton.usage-collector)" in res_rb2.stderr
    assert "step2_copy_back:failed" in res_rb2.stderr
    assert "step3_restore_pointer:skipped(step2_failed)" in res_rb2.stderr
    assert "step4_restore_old:skipped(step2_failed)" in res_rb2.stderr
    added2 = ctx2["launchctl_log"].read_text(encoding="utf-8")[len(before_log2):]
    assert "enable gui/501/ai.openclaw.model-usage-collector" not in added2
    assert ctx2["pointer_file"].read_text(encoding="utf-8").strip() == str(ctx2["dst_state"].resolve())
    assert list(ctx2["src_state"].glob("evidence.sqlite.pre-rollback.*")) == []

    # Case 3: Rollback step 3 (restore pointer) fails -> step 4 skipped, old collector NOT started
    ctx3 = _setup_switch_sandbox(tmp_path / "rb_s3")
    res_a3 = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--apply"], env=ctx3["env"], capture_output=True, text=True)
    assert res_a3.returncode == 0, res_a3.stderr
    ptr_dir = ctx3["pointer_file"].parent
    before_log3 = ctx3["launchctl_log"].read_text(encoding="utf-8")
    ptr_dir.chmod(0o555)
    try:
        res_rb3 = subprocess.run(["/bin/bash", str(SWITCH_SH_PATH), "--rollback"], env=ctx3["env"], capture_output=True, text=True)
    finally:
        ptr_dir.chmod(0o755)
    assert res_rb3.returncode != 0
    assert "step1_stop_new:ok(com.dalton.usage-collector)" in res_rb3.stderr
    assert "step2_copy_back:ok" in res_rb3.stderr
    assert "step3_restore_pointer:failed" in res_rb3.stderr
    assert "step4_restore_old:skipped(step3_failed)" in res_rb3.stderr
    added3 = ctx3["launchctl_log"].read_text(encoding="utf-8")[len(before_log3):]
    assert "enable gui/501/ai.openclaw.model-usage-collector" not in added3
    assert "bootstrap gui/501" not in added3


# ---------------------------------------------------------------------------
# 4. Missing OpenClaw source directories handled cleanly
# ---------------------------------------------------------------------------


def test_missing_openclaw_dirs_silently_skipped_and_noted_in_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(SKILL_SCRIPTS_DIR))
    import model_usage_cost_cli as cli

    fake_home = tmp_path / "no_openclaw_home"
    fake_home.mkdir()
    state_dir = tmp_path / "usage_state"
    claude_dir = fake_home / ".claude" / "projects" / "demo"
    claude_dir.mkdir(parents=True)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    (claude_dir / "sess.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": now_iso,
                "message": {
                    "id": "msg_01",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 1000,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 2000,
                        "output_tokens": 300,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    tracker = _load_module("dalton_token_weekly_usage_tracker_test", TRACKER_PATH)
    with patch.dict(
        os.environ,
        {
            "HOME": str(fake_home),
            "DALTON_USAGE_STATE_DIR": str(state_dir),
            "DALTON_BACKEND_URL": "",
            "INTERNAL_SERVICE_TOKEN": "",
        },
    ):
        buf_summary = io.StringIO()
        with patch("sys.argv", ["model_usage_cost_cli.py", "--json", "summary", "--days", "7"]), patch(
            "sys.stdout", buf_summary
        ):
            rc_summary = cli.main()
        assert rc_summary == 0
        summary_out = json.loads(buf_summary.getvalue())
        assert summary_out["ok"] is True
        missing = summary_out["coverage"]["missing_sources"]
        for expected_kind in (
            "rollout-openclaw",
            "trajectory-db",
            "trajectory-file",
            "claude-gateway",
            "agy-gateway",
            "muse-gateway",
        ):
            assert missing.get(expected_kind) == "来源目录不存在"
        for issue_key in summary_out["coverage"]["issues"]:
            assert "openclaw" not in issue_key and "gateway" not in issue_key

        stats = tracker.process_usage_stats(summary_out)
        assert stats["total_tokens"] == 3300
        card = tracker.build_card(stats)
        assert card["schema"] == "2.0"

        buf_broker = io.StringIO()
        with patch("sys.argv", ["model_usage_cost_cli.py", "collect-broker", "--days", "2", "--json"]), patch(
            "sys.stdout", buf_broker
        ):
            rc_broker = cli.main()
        assert rc_broker == 0
        broker_out = json.loads(buf_broker.getvalue())
        assert broker_out["ok"] is True
        assert broker_out["missing_sources"].get("claude-gateway") == "来源目录不存在"
