"""Unit tests for Dalton usage collector, pointer resolution, and missing OpenClaw sources."""

from __future__ import annotations

import datetime as dt
import fcntl
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECTOR_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "usage_collector.py"
COLLECTOR_RUN_SH_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "usage_collector_run.sh"
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
# 3. Missing OpenClaw source directories handled cleanly
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
