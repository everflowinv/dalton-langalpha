"""Unit tests for AT3: versioned data-task scripts, parameterization, packaging, seeding, and drift checks."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton.data_jobs.batch3_messages import adjust_batch3_payload
from src.dalton.data_jobs.container_runner import build_container_create_config, build_path_map
from src.dalton.data_jobs.notification_payloads import DataTaskNotificationPrepError
from src.dalton.data_jobs.runner_wrapper import run_wrapped_command

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "src" / "dalton" / "data_jobs" / "manifest.yaml"
BATCH3_MESSAGES_PATH = REPO_ROOT / "src" / "dalton" / "data_jobs" / "batch3_messages.py"
PROD_README_PATH = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod" / "README.md"
DATA_ASSETS_DIR = REPO_ROOT / "scripts" / "dalton" / "data_assets"
DATA_ASSETS_SCRIPTS_DIR = DATA_ASSETS_DIR / "scripts"
EVERFLOW_ONLY_PATH = DATA_ASSETS_DIR / "everflow_only.txt"
SEED_SCRIPT = REPO_ROOT / "scripts" / "dalton" / "seed_openclaw_data.sh"
PACKAGE_RELEASE_SCRIPT = REPO_ROOT / "scripts" / "standalone" / "package_release.sh"
DRIFT_SCRIPT = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod" / "check_data_assets_drift.py"

# Matches top-level `scripts/<filename>` references while ignoring `skills/.../scripts/...`
# and `.../scripts/dalton/...` repo-level scripts.
_TOP_LEVEL_SCRIPT_REF_RE = re.compile(
    r"(?:^|[\s'\"])scripts/([A-Za-z0-9_.-]+\.(?:py|sh))(?=$|[\s'\"])"
)

_FORBIDDEN_DATA_ASSET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("feishu_chat_id", re.compile(r"oc_[0-9a-f]{20,}")),
    ("feishu_user_id", re.compile(r"ou_[0-9a-f]{20,}")),
    ("discord_target_id", re.compile(r"(?:user|channel):\d{15,}")),
    ("everflow_home_path", re.compile(r"/Users/everflow")),
    ("personal_home_path", re.compile(r"/Users/richardlu")),
    ("windows_eve_path", re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[/\\]Eve(?![A-Za-z0-9_])", re.IGNORECASE)),
    ("everflow_corp_email", re.compile(r"@everflowinv\.com", re.IGNORECASE)),
    ("gs_sender_email", re.compile(r"@mail\.marquee\.gs\.com", re.IGNORECASE)),
    ("tailscale_100_ip", re.compile(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    (
        "common_secret_literal",
        re.compile(
            r"(?:"
            r"cli_[A-Za-z0-9]{12,}"
            r"|sk-[A-Za-z0-9]{20,}"
            r"|ghp_[A-Za-z0-9]{20,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|AIza[0-9A-Za-z_-]{35}"
            r"|xox[baprs]-[0-9A-Za-z-]{10,}"
            r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r")"
        ),
    ),
)

_EVERFLOW_PREFLIGHT_KEYS: dict[str, str] = {
    "DALTON_DATA_TASK_BBG_HOST": "bbg@test.example",
    "DALTON_DATA_TASK_BBG_CSV_REMOTE": "C:/test/output/bbg_data_latest.csv",
    "DALTON_DATA_TASK_BBG_XLSX_REMOTE": "C:/test/data.xlsx",
    "DALTON_DATA_TASK_BBG_REFRESH_CMD": "cmd /c C:\\test\\run.bat",
    "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID": "oc_placeholder_tracking",
    "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID": "oc_placeholder_semi",
    "DALTON_DATA_TASK_FEISHU_ASIA_TARGET": "chat:oc_placeholder_asia",
    "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET": "oc_placeholder_megacap",
    "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID": "oc_placeholder_sentiment",
    "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET": "user:ou_placeholder_ashare",
    "DALTON_DATA_TASK_DISCORD_DM_TARGET": "user:100000000000000001",
    "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL": "channel:100000000000000002",
    "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL": "channel:100000000000000003",
    "DALTON_SEC_USER_AGENT": "TestBot test@example.com",
    "DALTON_DATA_TASK_GS_PB_PYTHON": "/opt/test/gs-pb/bin/python",
}


def _load_everflow_only_entries() -> list[str]:
    entries: list[str] = []
    for raw_line in EVERFLOW_ONLY_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def _write_data_tasks_env(target: Path, values: dict[str, str], rel: str = "config/data_tasks.env") -> Path:
    cfg = target / rel
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        "\n".join(f"{k}={v}" for k, v in values.items()) + "\n",
        encoding="utf-8",
    )
    return cfg


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("DALTON_", "OPENCLAW_", "SENTIMENT_DIGEST_", "APP_ENV", "SEC_USER_AGENT", "WORKDIR")
        )
    }
    env["DALTON_BACKEND_CONTAINER"] = "__dalton_unit_test_nonexistent_backend__"
    env.update(extra)
    return env


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_script_references_exist_in_data_assets() -> None:
    """Every `scripts/<name>` referenced in manifest.yaml exists under data_assets/scripts/."""
    raw = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    tasks = raw.get("tasks", [])
    assert tasks, "manifest.yaml must declare tasks"

    referenced: set[str] = set()
    missing: list[tuple[str, str]] = []
    for task in tasks:
        task_code = str(task.get("task_code", ""))
        cmd = str(task.get("command", ""))
        argv = [str(a) for a in task.get("command_argv", [])]

        for text in (cmd, *argv):
            assert "/Users/richardlu" not in text, f"{task_code} contains hardcoded user path"
            assert "/Users/everflow/.local/share/everflow-tools" not in text, (
                f"{task_code} contains hardcoded host tools path: {text}"
            )
            assert "/Users/everflow/.openclaw/workspace/scripts/" not in text, (
                f"{task_code} still uses absolute scripts/ path: {text}"
            )
            for match in _TOP_LEVEL_SCRIPT_REF_RE.finditer(text):
                script_name = match.group(1)
                referenced.add(script_name)
                if not (DATA_ASSETS_SCRIPTS_DIR / script_name).is_file():
                    missing.append((task_code, script_name))

    assert "bbg_remote_refresh.sh" in referenced
    assert "run_json_task.sh" in referenced
    assert not missing, f"manifest.yaml references scripts missing from data_assets/scripts/: {missing}"


def test_data_assets_contain_no_everflow_identifiers_or_secrets() -> None:
    """No data asset, batch3_messages, or prod README may contain Everflow IDs, home/Windows paths, corp emails, or secrets."""
    violations: list[str] = []
    scanned = 0
    targets: list[tuple[str, Path]] = []
    for path in sorted(DATA_ASSETS_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        targets.append((path.relative_to(REPO_ROOT).as_posix(), path))
    targets.append((BATCH3_MESSAGES_PATH.relative_to(REPO_ROOT).as_posix(), BATCH3_MESSAGES_PATH))
    targets.append((PROD_README_PATH.relative_to(REPO_ROOT).as_posix(), PROD_README_PATH))

    for rel, path in targets:
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule_name, pattern in _FORBIDDEN_DATA_ASSET_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{rule_name}: {rel}:{line_no}")

    # Manifest and all Core-shipped data_jobs modules / d029 migration must contain ZERO
    # Everflow home paths, personal home paths, Windows paths, IDs, or emails.
    for extra_path in (
        MANIFEST_PATH,
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "manifest.py",
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "task_config.py",
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "runner_wrapper.py",
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "container_runner.py",
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "evaluators.py",
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "notification_payloads.py",
        REPO_ROOT / "src" / "dalton" / "data_jobs" / "scheduler.py",
        REPO_ROOT / "src" / "dalton" / "migrations" / "versions" / "d029_data_tasks.py",
    ):
        rel = extra_path.relative_to(REPO_ROOT).as_posix()
        text = extra_path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for rule_name, pattern in _FORBIDDEN_DATA_ASSET_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{rule_name}: {rel}:{line_no}")

    assert scanned > 20, "expected to scan all versioned data_assets files"
    assert PROD_README_PATH.is_file(), "scripts/dalton/ops/prod/README.md must exist"
    assert not violations, "Forbidden Everflow identifiers/secrets found:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("cmd", "extra_env", "expected_key"),
    [
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "etf_monthly_flows_tracker.py"), "--send"],
            {},
            "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID",
        ),
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "etf_monthly_flows_tracker.py"), "--send", "--to", ""],
            {},
            "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID",
        ),
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "megacap_relative_signal_monitor.py"), "--json"],
            {},
            "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET",
        ),
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "pt_watchlist_monitor.py")],
            {},
            "DALTON_DATA_TASK_FEISHU_ASIA_TARGET",
        ),
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "e_watch_monitor.py")],
            {},
            "DALTON_DATA_TASK_DISCORD_DM_TARGET",
        ),
        (
            ["bash", str(DATA_ASSETS_SCRIPTS_DIR / "bbg_remote_refresh.sh"), "--pull-only"],
            {},
            "DALTON_DATA_TASK_BBG_HOST",
        ),
        (
            ["bash", str(DATA_ASSETS_SCRIPTS_DIR / "bbg_remote_refresh.sh"), "--pull-only"],
            {"DALTON_DATA_TASK_BBG_HOST": "bbg@test.example"},
            "DALTON_DATA_TASK_BBG_CSV_REMOTE",
        ),
        (
            ["bash", str(DATA_ASSETS_SCRIPTS_DIR / "bbg_remote_refresh.sh")],
            {
                "DALTON_DATA_TASK_BBG_HOST": "bbg@test.example",
                "DALTON_DATA_TASK_BBG_CSV_REMOTE": "C:/test/bbg.csv",
                "DALTON_DATA_TASK_BBG_XLSX_REMOTE": "C:/test/bbg.xlsx",
            },
            "DALTON_DATA_TASK_BBG_REFRESH_CMD",
        ),
        (
            ["bash", str(DATA_ASSETS_SCRIPTS_DIR / "sentiment_dashboard_digest.sh")],
            {},
            "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID",
        ),
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "softbank_nav_monitor.py"), "--feishu", ""],
            {},
            "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID",
        ),
        (
            [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "tsmc_adr_premium.py"), "--feishu", ""],
            {},
            "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID",
        ),
    ],
)
def test_parameterized_scripts_fail_clearly_when_unconfigured(
    tmp_path: Path, cmd: list[str], extra_env: dict[str, str], expected_key: str
) -> None:
    """Parameterized scripts fail non-zero with a clear '未配置' message when unset or empty."""
    missing_cfg = tmp_path / "nonexistent_data_tasks.env"
    proc = subprocess.run(
        cmd,
        cwd=str(tmp_path),
        env=_clean_env(DALTON_DATA_TASKS_CONFIG=str(missing_cfg), **extra_env),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"expected non-zero exit when {expected_key} is unset: {combined}"
    assert "未配置" in combined, f"expected '未配置' message for {expected_key}, got: {combined}"
    assert expected_key in combined, f"expected key name {expected_key} in error output: {combined}"


def test_cron_command_tasks_fail_fast_on_missing_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cron_command_tasks.py never builds `--feishu ""` or `--to ""` and fails fast on missing targets."""
    for key in list(os.environ):
        if key.startswith(("DALTON_", "OPENCLAW_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DALTON_DATA_TASKS_CONFIG", str(tmp_path / "missing.env"))
    mod = _load_module("cron_cmd_missing_targets", DATA_ASSETS_SCRIPTS_DIR / "cron_command_tasks.py")

    with pytest.raises(ValueError, match="DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID"):
        mod._build_simple_commands("softbank-nav-monitor")
    with pytest.raises(ValueError, match="DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID"):
        mod._build_simple_commands("tsmc-adr-premium")
    with pytest.raises(ValueError, match="DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID"):
        mod._build_simple_commands("us-etf-monthly-flows")
    with pytest.raises(ValueError, match="DALTON_DATA_TASK_DISCORD_DM_TARGET"):
        mod.e_watch_daily_delivery_spec({"messages": ["hi"], "report_file": "temp/r.txt"})
    with pytest.raises(ValueError, match="DALTON_DATA_TASK_DISCORD_DEV_CHANNEL"):
        mod.ai_chain_delivery_spec(
            {"sent": False, "quiet": False, "target": "channel:1", "summary": "s", "high_cards": []}
        )


def test_etf_and_pt_keys_unified_between_entry_and_script(tmp_path: Path) -> None:
    """ETF and PT entrypoints share the exact same canonical keys with their underlying scripts."""
    ws = tmp_path / "ws"
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "data_tasks.env").write_text(
        "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID=oc_unified_tracking_test\n"
        "DALTON_DATA_TASK_FEISHU_ASIA_TARGET=chat:oc_unified_asia_test\n"
        "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET=oc_unified_megacap_test\n"
        "DALTON_DATA_TASK_DISCORD_DM_TARGET=user:100000000000000099\n",
        encoding="utf-8",
    )

    # Run from cwd=ws with NO OPENCLAW_WORKSPACE/DALTON_WORKSPACE to prove runtime workspace config lookup
    py_check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, json\n"
                "def load(name, path):\n"
                "    s = importlib.util.spec_from_file_location(name, path)\n"
                "    m = importlib.util.module_from_spec(s)\n"
                "    s.loader.exec_module(m)\n"
                "    return m\n"
                f"cron = load('cron', {str(DATA_ASSETS_SCRIPTS_DIR / 'cron_command_tasks.py')!r})\n"
                f"etf = load('etf', {str(DATA_ASSETS_SCRIPTS_DIR / 'etf_monthly_flows_tracker.py')!r})\n"
                f"pt = load('pt', {str(DATA_ASSETS_SCRIPTS_DIR / 'pt_watchlist_monitor.py')!r})\n"
                f"mc = load('mc', {str(DATA_ASSETS_SCRIPTS_DIR / 'megacap_relative_signal_monitor.py')!r})\n"
                f"ew = load('ew', {str(DATA_ASSETS_SCRIPTS_DIR / 'e_watch_monitor.py')!r})\n"
                "pt_spec = cron.pt_watchlist_delivery_spec({'feishu_target': pt.resolve_feishu_target(), 'main_message': 'ok'})\n"
                "etf_cmd = cron._build_simple_commands('us-etf-monthly-flows')[0]\n"
                "print(json.dumps({\n"
                "  'cron_tracking': cron._require_tracking(),\n"
                "  'etf_default': etf.DEFAULT_EVE_TRACKING_CHAT_ID,\n"
                "  'etf_cmd_to': etf_cmd[-1],\n"
                "  'cron_asia': cron._require_asia(),\n"
                "  'pt_target': pt.resolve_feishu_target(),\n"
                "  'pt_spec_target': pt_spec[0]['target'],\n"
                "  'megacap': mc.resolve_feishu_target(),\n"
                "  'ewatch_dm': ew.resolve_discord_target(),\n"
                "}))\n"
            ),
        ],
        cwd=str(ws),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert py_check.returncode == 0, py_check.stdout + py_check.stderr
    resolved = json.loads(py_check.stdout.strip())
    assert resolved == {
        "cron_tracking": "oc_unified_tracking_test",
        "etf_default": "oc_unified_tracking_test",
        "etf_cmd_to": "oc_unified_tracking_test",
        "cron_asia": "chat:oc_unified_asia_test",
        "pt_target": "chat:oc_unified_asia_test",
        "pt_spec_target": "chat:oc_unified_asia_test",
        "megacap": "oc_unified_megacap_test",
        "ewatch_dm": "user:100000000000000099",
    }


def test_runtime_workspace_resolution_without_openclaw_env(tmp_path: Path) -> None:
    """run_json_task.sh and quantum_ew_monitor.sh default to runtime $PWD when workspace env vars are unset."""
    ws = tmp_path / "runtime_ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "quantum_ew_daily_v4.py").write_text(
        "print('{\"ok\": true, \"source\": \"runtime_cwd\"}')\n",
        encoding="utf-8",
    )

    env = _clean_env()

    q_proc = subprocess.run(
        ["bash", str(DATA_ASSETS_SCRIPTS_DIR / "quantum_ew_monitor.sh")],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert q_proc.returncode == 0, q_proc.stderr
    assert '"source": "runtime_cwd"' in q_proc.stdout
    assert "ops override active" not in q_proc.stderr

    r_ok = subprocess.run(
        [
            "bash",
            str(DATA_ASSETS_SCRIPTS_DIR / "run_json_task.sh"),
            sys.executable,
            "-c",
            'import json, os; print(json.dumps({"ok": True, "cwd": os.getcwd()}))',
        ],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_ok.returncode == 0, r_ok.stderr
    assert json.loads(r_ok.stdout.strip())["cwd"] == str(ws.resolve())

    r_fail = subprocess.run(
        [
            "bash",
            str(DATA_ASSETS_SCRIPTS_DIR / "run_json_task.sh"),
            sys.executable,
            "-c",
            'import json; print(json.dumps({"ok": False, "error": "boom"}))',
        ],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_fail.returncode == 1
    assert "ok:false" in r_fail.stderr


def test_semi_rescue_alert_reads_sec_user_agent_from_config_and_container_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """semi_rescue_alert reads DALTON_SEC_USER_AGENT from config/data_tasks.env, fails when missing, and container_runner forwards it."""
    ws = tmp_path / "semi_ws"
    (ws / "config").mkdir(parents=True)
    for key in ("DALTON_SEC_USER_AGENT", "DALTON_DATA_TASK_SEC_USER_AGENT", "SEC_USER_AGENT", "DALTON_DATA_TASKS_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DALTON_DATA_TASK_WORKSPACE", str(ws))

    mod = _load_module("semi_rescue_sec_test", DATA_ASSETS_SCRIPTS_DIR / "semi_rescue_alert.py")
    sample_cfg = {
        "sec_watch": [
            {
                "name": "TestCo",
                "cik": "0000000001",
                "forms": ["8-K"],
                "since": "2026-01-01",
                "meaning": {"8-K": "filed"},
            }
        ]
    }

    # 1. Missing everywhere -> raises RuntimeError instead of silently swallowing EDGAR loss
    with pytest.raises(RuntimeError, match="DALTON_SEC_USER_AGENT"):
        mod.sec_latest(sample_cfg)

    # 2. Configured in <runtime_workspace>/config/data_tasks.env -> resolves and passes User-Agent header
    (ws / "config" / "data_tasks.env").write_text(
        'DALTON_SEC_USER_AGENT="DaltonUnitTest test@example.com"\n',
        encoding="utf-8",
    )
    captured_headers: list[dict | None] = []

    def _fake_http_get(url: str, timeout: int = 30, headers: dict | None = None) -> bytes:
        captured_headers.append(headers)
        return json.dumps(
            {
                "filings": {
                    "recent": {
                        "form": ["8-K"],
                        "filingDate": ["2026-11-01"],
                        "accessionNumber": ["0001-26-000001"],
                    }
                }
            }
        ).encode("utf-8")

    monkeypatch.setattr(mod, "http_get", create_autospec(mod.http_get, side_effect=_fake_http_get))
    rows = mod.sec_latest(sample_cfg)
    assert len(rows) == 1
    assert rows[0]["form"] == "8-K"
    assert captured_headers == [{"User-Agent": "DaltonUnitTest test@example.com"}]

    # 3. container_runner forwards DALTON_SEC_USER_AGENT from backend env into ephemeral task container Env
    monkeypatch.setenv("DALTON_SEC_USER_AGENT", "ContainerBot sec@example.com")
    cfg = build_container_create_config(
        job_key="semi-rescue-alert-am",
        run_id="11111111-1111-4111-8111-111111111111",
        generation=1,
        command_argv=["python3", "scripts/semi_rescue_alert.py", "run", "--slot", "am", "--json"],
        path_map=build_path_map(host_var_dir=tmp_path / "var", backend_var_dir=tmp_path / "var"),
    )
    assert "DALTON_SEC_USER_AGENT=ContainerBot sec@example.com" in cfg["Env"]


def test_batch3_messages_targets_require_config_and_read_data_tasks_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core profile fails clearly when unset; Everflow profile loads everflow_defaults.yaml and lets env/config override."""
    for key in (
        "APP_ENV",
        "DALTON_PROFILE",
        "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL",
        "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL",
        "DALTON_DATA_TASK_DISCORD_DM_TARGET",
        "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET",
        "DALTON_DATA_TASKS_CONFIG",
        "DALTON_DATA_TASK_WORKSPACE",
        "DALTON_WORKSPACE",
        "OPENCLAW_WORKSPACE",
        "WORKDIR",
    ):
        monkeypatch.delenv(key, raising=False)

    # 1. Core profile with no env/config -> MUST raise clear error for T2, T38, T73, T83
    monkeypatch.setenv("DALTON_PROFILE", "core")
    for job_key, expected_key in (
        ("fde-hiring-weekly-report", "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL"),
        ("semi-bottom-checklist-1030et", "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL"),
        ("e-watch-intraday", "DALTON_DATA_TASK_DISCORD_DM_TARGET"),
        ("a-share-bottom-volume-watch", "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET"),
    ):
        with pytest.raises(DataTaskNotificationPrepError, match=expected_key):
            adjust_batch3_payload(
                job_key=job_key,
                payload=None,
                stdout_text="alert payload\n",
                workspace_dir=tmp_path,
            )

    # 2. Core profile WITH config/data_tasks.env -> resolves configured values
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "data_tasks.env").write_text(
        "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL=channel:200000000000000001\n"
        "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL=channel:200000000000000002\n"
        "DALTON_DATA_TASK_DISCORD_DM_TARGET=user:200000000000000003\n"
        "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET=user:ou_unit_test_ashare\n",
        encoding="utf-8",
    )
    fde = adjust_batch3_payload(
        job_key="fde-hiring-weekly-report",
        payload=None,
        stdout_text="weekly report\n",
        workspace_dir=tmp_path,
    )
    assert isinstance(fde, dict) and fde["target"] == "channel:200000000000000001"
    semi = adjust_batch3_payload(
        job_key="semi-bottom-checklist-1030et",
        payload=None,
        stdout_text="checklist\n",
        workspace_dir=tmp_path,
    )
    assert isinstance(semi, dict) and semi["target"] == "channel:200000000000000002"
    ashare = adjust_batch3_payload(
        job_key="a-share-bottom-volume-watch",
        payload=None,
        stdout_text="alert\n",
        workspace_dir=tmp_path,
    )
    assert isinstance(ashare, dict) and ashare["target"] == "user:ou_unit_test_ashare"


def test_everflow_defaults_preserve_t2_t38_t58_t73_t83_without_env_and_core_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 #1: under Everflow profile with ZERO new env vars, T2/T38/T58/T73/T83 match pre-AT3 defaults; env overrides; Core fails."""
    import scripts.dalton.researchdata_bridge as rb

    for key in (
        "APP_ENV",
        "DALTON_PROFILE",
        "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL",
        "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL",
        "DALTON_DATA_TASK_DISCORD_DM_TARGET",
        "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET",
        "DALTON_DATA_TASK_GS_PB_PYTHON",
        "DALTON_DATA_TASKS_CONFIG",
        "DALTON_DATA_TASK_WORKSPACE",
        "DALTON_WORKSPACE",
        "OPENCLAW_WORKSPACE",
        "WORKDIR",
    ):
        monkeypatch.delenv(key, raising=False)

    defaults_path = REPO_ROOT / "src" / "dalton" / "data_jobs" / "everflow_defaults.yaml"
    assert defaults_path.is_file(), "src/dalton/data_jobs/everflow_defaults.yaml must exist"
    expected_defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
    assert isinstance(expected_defaults, dict)
    for req_key in (
        "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL",
        "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL",
        "DALTON_DATA_TASK_DISCORD_DM_TARGET",
        "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET",
        "DALTON_DATA_TASK_GS_PB_PYTHON",
    ):
        assert str(expected_defaults.get(req_key) or "").strip(), f"missing {req_key} in everflow_defaults.yaml"

    empty_ws = tmp_path / "empty_ws"
    empty_ws.mkdir()

    # 1. Everflow profile with ZERO new env vars and empty workspace -> exact pre-AT3 defaults
    monkeypatch.setenv("APP_ENV", "dalton")
    t73 = adjust_batch3_payload(
        job_key="fde-hiring-weekly-report", payload=None, stdout_text="report\n", workspace_dir=empty_ws
    )
    t83 = adjust_batch3_payload(
        job_key="semi-bottom-checklist-1030et", payload=None, stdout_text="check\n", workspace_dir=empty_ws
    )
    t2 = adjust_batch3_payload(
        job_key="e-watch-intraday", payload=None, stdout_text="watch\n", workspace_dir=empty_ws
    )
    t38 = adjust_batch3_payload(
        job_key="a-share-bottom-volume-watch", payload=None, stdout_text="vol\n", workspace_dir=empty_ws
    )
    assert isinstance(t73, dict) and t73["target"] == expected_defaults["DALTON_DATA_TASK_DISCORD_DEV_CHANNEL"]
    assert isinstance(t83, dict) and t83["target"] == expected_defaults["DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL"]
    assert isinstance(t2, dict) and t2["target"] == expected_defaults["DALTON_DATA_TASK_DISCORD_DM_TARGET"]
    assert isinstance(t38, dict) and t38["target"] == expected_defaults["DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET"]

    t58_entry = rb._load_host_job_manifest().by_job_key()["gs-pb-weekly-rundown"]
    t58_spec = rb.resolve_host_run_command_spec(
        job_key="gs-pb-weekly-rundown",
        command_argv=list(t58_entry.command_argv),
        cwd=t58_entry.cwd,
        data_root=empty_ws,
        ocw_root=tmp_path / "ocw",
    )
    assert t58_spec["command_argv"][0] == expected_defaults["DALTON_DATA_TASK_GS_PB_PYTHON"]

    # 2. Environment variable overrides everflow_defaults.yaml under Everflow profile
    monkeypatch.setenv("DALTON_DATA_TASK_DISCORD_DEV_CHANNEL", "channel:999999999999999999")
    monkeypatch.setenv("DALTON_DATA_TASK_GS_PB_PYTHON", "/opt/override/gs-pb/bin/python")
    t73_override = adjust_batch3_payload(
        job_key="fde-hiring-weekly-report", payload=None, stdout_text="report\n", workspace_dir=empty_ws
    )
    assert isinstance(t73_override, dict) and t73_override["target"] == "channel:999999999999999999"
    t58_override = rb.resolve_host_run_command_spec(
        job_key="gs-pb-weekly-rundown",
        command_argv=list(t58_entry.command_argv),
        cwd=t58_entry.cwd,
        data_root=empty_ws,
        ocw_root=tmp_path / "ocw",
    )
    assert t58_override["command_argv"][0] == "/opt/override/gs-pb/bin/python"

    # 3. Core profile ignores everflow_defaults.yaml and fails explicitly when unset
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DALTON_DATA_TASK_DISCORD_DEV_CHANNEL", raising=False)
    monkeypatch.delenv("DALTON_DATA_TASK_GS_PB_PYTHON", raising=False)
    monkeypatch.setenv("DALTON_PROFILE", "core")
    with pytest.raises(DataTaskNotificationPrepError, match="DALTON_DATA_TASK_DISCORD_DEV_CHANNEL"):
        adjust_batch3_payload(
            job_key="fde-hiring-weekly-report", payload=None, stdout_text="report\n", workspace_dir=empty_ws
        )
    with pytest.raises(rb.HostCommandRejected, match="DALTON_DATA_TASK_GS_PB_PYTHON"):
        rb.resolve_host_run_command_spec(
            job_key="gs-pb-weekly-rundown",
            command_argv=list(t58_entry.command_argv),
            cwd=t58_entry.cwd,
            data_root=empty_ws,
            ocw_root=tmp_path / "ocw",
        )


def test_china_2c_monitor_runs_cleanly_in_empty_core_workspace(tmp_path: Path) -> None:
    """china_2c_monitor creates missing output/state dirs under runtime workspace without ~/.openclaw."""
    ws = tmp_path / "clean_core_ws"
    ws.mkdir()
    fake_home = tmp_path / "empty_home"
    fake_home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "china_2c_monitor.py"), "--market", "hk"],
        cwd=str(ws),
        env=_clean_env(HOME=str(fake_home)),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    # Empty universe produces no alerts (exit 1 is normal 'no alerts' contract) without FileNotFoundError
    assert "FileNotFoundError" not in proc.stderr
    assert "Traceback" not in proc.stderr
    assert (ws / "state" / "china_2c_monitor_state.json").is_file()
    assert (ws / "temp" / "china_2c_monitor_latest.txt").is_file()


def test_seed_preflight_blocks_everflow_overwrite_when_keys_missing(tmp_path: Path) -> None:
    """seed_openclaw_data.sh --apply refuses to overwrite var/scripts/ in everflow mode if any required key is missing."""
    target = tmp_path / "prod_var"
    existing_script = target / "scripts" / "cron_command_tasks.py"
    existing_script.parent.mkdir(parents=True)
    existing_script.write_text("# existing production script\n", encoding="utf-8")

    # 1. Missing all keys -> exit non-zero, list missing keys, leave existing script untouched
    blocked = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(target)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert blocked.returncode != 0
    assert "预检失败" in blocked.stderr
    for key in _EVERFLOW_PREFLIGHT_KEYS:
        assert key in blocked.stderr, f"missing key {key} not listed in preflight error"
    assert existing_script.read_text(encoding="utf-8") == "# existing production script\n"

    # 2. Keys ONLY in seed shell env (not persisted in container-readable config) -> MUST be refused!
    blocked_env_only = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(target)],
        env=_clean_env(APP_ENV="dalton", **_EVERFLOW_PREFLIGHT_KEYS),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert blocked_env_only.returncode != 0
    assert "预检失败" in blocked_env_only.stderr
    assert existing_script.read_text(encoding="utf-8") == "# existing production script\n"

    # 3. Missing just ONE key in <target>/config/data_tasks.env -> still refused
    partial_cfg = dict(_EVERFLOW_PREFLIGHT_KEYS)
    partial_cfg.pop("DALTON_DATA_TASK_DISCORD_DM_TARGET")
    _write_data_tasks_env(target, partial_cfg)
    blocked_one = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(target)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert blocked_one.returncode != 0
    assert "DALTON_DATA_TASK_DISCORD_DM_TARGET" in blocked_one.stderr
    assert existing_script.read_text(encoding="utf-8") == "# existing production script\n"

    # 4. All required keys present in <target>/config/data_tasks.env -> apply succeeds
    _write_data_tasks_env(target, _EVERFLOW_PREFLIGHT_KEYS)
    ok = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(target)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert existing_script.read_bytes() == (DATA_ASSETS_SCRIPTS_DIR / "cron_command_tasks.py").read_bytes()


def test_drift_check_matched_drifted_one_sided_missing_root_and_seed_baseline(tmp_path: Path) -> None:
    """Drift checker reports 4 sections, rejects missing roots, and accepts seed-generated Antigravity files."""
    drift_mod = _load_module("check_data_assets_drift", DRIFT_SCRIPT)

    # Case 0: Missing check root directories must exit non-zero (exit 2), NEVER exit 0
    missing_proc = subprocess.run(
        [
            sys.executable,
            str(DRIFT_SCRIPT),
            "--repo-dir",
            str(tmp_path / "no_repo"),
            "--var-dir",
            str(tmp_path / "no_var"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert missing_proc.returncode == 2
    assert "不存在" in missing_proc.stderr

    # Case 1: All matched (with __pycache__ noise on both sides) -> exit 0, strictly read-only
    repo1 = tmp_path / "case1_repo"
    var1 = tmp_path / "case1_var"
    for root in (repo1, var1):
        (root / "scripts" / "__pycache__").mkdir(parents=True)
        (root / "skills" / "demo").mkdir(parents=True)
        (root / "scripts" / "common.py").write_text("print('same')\n", encoding="utf-8")
        (root / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (repo1 / "scripts" / "__pycache__" / "common.cpython-314.pyc").write_bytes(b"repo-pyc")
    (var1 / "scripts" / "__pycache__" / "common.cpython-314.pyc").write_bytes(b"var-pyc-diff")
    (var1 / "scripts" / "stray.pyc").write_bytes(b"pyc-ignored")

    before_repo = sorted(p.relative_to(repo1).as_posix() for p in repo1.rglob("*"))
    before_var = sorted(p.relative_to(var1).as_posix() for p in var1.rglob("*"))

    proc1 = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "--repo-dir", str(repo1), "--var-dir", str(var1)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr
    assert "一致 (2):" in proc1.stdout
    assert "漂移 (0):" in proc1.stdout
    assert "只在仓库 (0):" in proc1.stdout
    assert "只在 var (0):" in proc1.stdout
    assert "__pycache__" not in proc1.stdout
    assert sorted(p.relative_to(repo1).as_posix() for p in repo1.rglob("*")) == before_repo
    assert sorted(p.relative_to(var1).as_posix() for p in var1.rglob("*")) == before_var

    # Case 2: Content drifted -> exit 1
    (var1 / "scripts" / "common.py").write_text("print('modified in var')\n", encoding="utf-8")
    proc2 = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "--repo-dir", str(repo1), "--var-dir", str(var1), "--json"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc2.returncode == 1
    data2 = json.loads(proc2.stdout)
    assert data2["ok"] is False
    assert data2["drifted"] == ["scripts/common.py"]

    # Case 3: One-sided files (repo_only and var_only) -> exit 1
    repo3 = tmp_path / "case3_repo"
    var3 = tmp_path / "case3_var"
    (repo3 / "scripts").mkdir(parents=True)
    (var3 / "scripts").mkdir(parents=True)
    (repo3 / "scripts" / "same.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (var3 / "scripts" / "same.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo3 / "scripts" / "only_in_repo.py").write_text("x = 1\n", encoding="utf-8")
    (var3 / "scripts" / "only_in_var.py").write_text("y = 2\n", encoding="utf-8")
    report3 = drift_mod.compare_data_assets(repo3, var3)
    assert report3.has_drift is True
    assert report3.repo_only == ("scripts/only_in_repo.py",)
    assert report3.var_only == ("scripts/only_in_var.py",)

    # Case 4: Real Everflow seed output (including generated Antigravity files) has 0 drift;
    # adding an extra script under var/scripts/ is reported in 只在 var with exit 1.
    seeded_var = tmp_path / "seeded_prod_var"
    _write_data_tasks_env(seeded_var, _EVERFLOW_PREFLIGHT_KEYS)
    seed_proc = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(seeded_var)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert seed_proc.returncode == 0, seed_proc.stdout + seed_proc.stderr

    clean_drift = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "--var-dir", str(seeded_var), "--json"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert clean_drift.returncode == 0, clean_drift.stdout + clean_drift.stderr
    clean_data = json.loads(clean_drift.stdout)
    assert clean_data["ok"] is True
    assert clean_data["drifted"] == []
    assert clean_data["repo_only"] == []
    assert clean_data["var_only"] == []
    assert "skills/model-usage-cost/scripts/antigravity_series.py" in clean_data["matched"]
    assert "skills/model-usage-cost/references/antigravity_series.json" in clean_data["matched"]

    (seeded_var / "scripts" / "prod_only_untracked.py").write_text("print('prod')\n", encoding="utf-8")
    extra_drift = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), "--var-dir", str(seeded_var), "--json"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert extra_drift.returncode == 1
    extra_data = json.loads(extra_drift.stdout)
    assert extra_data["var_only"] == ["scripts/prod_only_untracked.py"]


def test_package_release_core_strict_scan_and_end_to_end_core_seed(tmp_path: Path) -> None:
    """package_release.sh --strict-scan exports a clean Core tree and seed_openclaw_data.sh --apply passes inside it."""
    everflow_only = _load_everflow_only_entries()
    assert everflow_only, "everflow_only.txt must not be empty"
    for entry in everflow_only:
        assert (DATA_ASSETS_DIR / entry).is_file(), f"everflow_only.txt entry missing in repo: {entry}"

    all_scripts = {
        f"scripts/{p.name}"
        for p in DATA_ASSETS_SCRIPTS_DIR.iterdir()
        if p.is_file() and not p.name.endswith(".pyc")
    }
    generic_scripts = sorted(all_scripts - set(everflow_only))
    assert generic_scripts, "expected at least one generic script in data_assets/scripts/"

    # Build a minimal git repo with our REAL working-tree profile, data_jobs, seed script, and data_assets
    repo = tmp_path / "pkg-repo"
    repo.mkdir()
    required_files = [
        "LICENSE",
        "README.md",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "server.py",
        "alembic.ini",
        "config.yaml",
        "config.core.yaml",
        "agent_config.yaml",
        "agent_config.core.yaml",
        "docker-compose.yml",
        ".env.example",
        ".dockerignore",
        "Dockerfile.sandbox",
        "libs/market-protocol/README.md",
        "libs/chartkit/README.md",
        "libs/edgar-engine/README.md",
        "migrations/README.md",
        "mcp_servers/README.md",
        "plugins/README.md",
        "plugins/alternative_data/README.md",
        "plugins/dalton/README.md",
        "plugins/firecrawl/README.md",
        "plugins/langalpha_deliverables/README.md",
        "plugins/langalpha_market_data/README.md",
        "plugins/langalpha_research/README.md",
        "plugins/langalpha_service/README.md",
        "plugins/yfinance/README.md",
        "workflows/README.md",
        "config/dalton/.gitkeep",
        "config/dalton/README.md",
        "deploy/Dockerfile.backend",
        "deploy/Dockerfile.dev",
        "deploy/Dockerfile.web",
        "scripts/configure.sh",
        "scripts/docker_entrypoint.sh",
        "scripts/_errors.py",
        "scripts/dalton/Dockerfile.dalton",
        "scripts/dalton/Dockerfile.postgres",
        "scripts/dalton/docker_entrypoint_dalton.sh",
        "scripts/dalton/configure_minio.sh",
        "scripts/dalton/log_redaction.py",
        "scripts/dalton/sql/personal_integrations_restore_reset.sql",
        "web/package.json",
        "docs-site/package.json",
    ]
    for rel in required_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("ok\n", encoding="utf-8")

    # Copy real src/dalton/__init__.py, profile.py, llm/antigravity_series.py, data_jobs/, and d029 migration
    for rel_src in (
        "src/dalton/__init__.py",
        "src/dalton/profile.py",
        "src/dalton/llm/antigravity_series.py",
        "src/dalton/data_jobs/manifest.yaml",
        "src/dalton/data_jobs/manifest.py",
        "src/dalton/data_jobs/task_config.py",
        "src/dalton/data_jobs/workspace.dalton.yaml",
        "src/dalton/data_jobs/everflow_defaults.yaml",
        "src/dalton/data_jobs/batch3_messages.py",
        "src/dalton/data_jobs/container_runner.py",
        "src/dalton/data_jobs/runner_wrapper.py",
        "src/dalton/data_jobs/evaluators.py",
        "src/dalton/data_jobs/notification_payloads.py",
        "src/dalton/data_jobs/scheduler.py",
        "src/dalton/migrations/versions/d029_data_tasks.py",
    ):
        dst = repo / rel_src
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel_src, dst)

    shutil.copy2(SEED_SCRIPT, repo / "scripts" / "dalton" / "seed_openclaw_data.sh")
    reset_sql_dst = repo / "scripts" / "dalton" / "sql" / "personal_integrations_restore_reset.sql"
    reset_sql_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        REPO_ROOT / "scripts" / "dalton" / "sql" / "personal_integrations_restore_reset.sql",
        reset_sql_dst,
    )
    shutil.copytree(
        DATA_ASSETS_SCRIPTS_DIR,
        repo / "scripts" / "dalton" / "data_assets" / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(
        EVERFLOW_ONLY_PATH,
        repo / "scripts" / "dalton" / "data_assets" / "everflow_only.txt",
    )

    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(repo), check=True)

    out_dir = tmp_path / "core-dist"
    proc = subprocess.run(
        [
            "bash",
            str(PACKAGE_RELEASE_SCRIPT),
            "--repo",
            str(repo),
            "--ref",
            "HEAD",
            "--strict-scan",
            "--output",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    exported_assets = out_dir / "scripts" / "dalton" / "data_assets"
    assert not (exported_assets / "everflow_only.txt").exists()
    assert not (exported_assets / "skills").exists()
    assert not (out_dir / "src" / "dalton" / "data_jobs" / "workspace.dalton.yaml").exists()
    assert not (out_dir / "src" / "dalton" / "data_jobs" / "everflow_defaults.yaml").exists()
    for entry in everflow_only:
        assert not (exported_assets / entry).exists(), (
            f"Everflow-only script leaked into Core release: {entry}"
        )
    for rel in generic_scripts:
        assert (exported_assets / rel).is_file(), (
            f"Generic script missing from Core release: {rel}"
        )

    # Execute seed_openclaw_data.sh --apply INSIDE the exported Core release tree
    # (without skills/ and without any Everflow env vars!) to prove end-to-end Core installation works.
    exported_seed = out_dir / "scripts" / "dalton" / "seed_openclaw_data.sh"
    core_var_target = tmp_path / "core-installed-var"
    venv_bin_dir = str(Path(sys.executable).parent)
    core_seed_env = _clean_env(
        DALTON_PROFILE="core",
        PATH=f"{venv_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    core_seed = subprocess.run(
        ["bash", str(exported_seed), "--apply", "--target", str(core_var_target)],
        cwd=str(out_dir),
        env=core_seed_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert core_seed.returncode == 0, f"stdout:\n{core_seed.stdout}\nstderr:\n{core_seed.stderr}"
    for rel in generic_scripts:
        assert (core_var_target / rel).is_file(), f"Core release seed failed to install {rel}"
    for entry in everflow_only:
        assert not (core_var_target / entry).exists()
    assert not (core_var_target / "skills").exists()
    assert (core_var_target / "state" / "a-share-volume-watch").is_dir()


def test_seed_openclaw_data_respects_profile_for_everflow_only(tmp_path: Path) -> None:
    """seed_openclaw_data.sh skips everflow_only.txt under core profile and installs all under everflow."""
    everflow_only = _load_everflow_only_entries()
    all_scripts = {
        f"scripts/{p.name}"
        for p in DATA_ASSETS_SCRIPTS_DIR.iterdir()
        if p.is_file() and not p.name.endswith(".pyc")
    }
    generic_scripts = sorted(all_scripts - set(everflow_only))

    # 1. Core profile via DALTON_PROFILE=core (requires ZERO Everflow keys)
    core_target = tmp_path / "core-target"
    core_proc = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(core_target)],
        env=_clean_env(DALTON_PROFILE="core"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert core_proc.returncode == 0, core_proc.stdout + core_proc.stderr
    assert not (core_target / "everflow_only.txt").exists()
    assert not (core_target / "skills" / "model-usage-cost" / "references" / "antigravity_series.json").exists()
    for entry in everflow_only:
        assert not (core_target / entry).exists(), f"core seed should skip {entry}"
    for rel in generic_scripts:
        assert (core_target / rel).is_file(), f"core seed should install {rel}"

    # 2. Everflow profile via APP_ENV=dalton (with required preflight keys persisted in target config)
    ef_target = tmp_path / "everflow-target"
    _write_data_tasks_env(ef_target, _EVERFLOW_PREFLIGHT_KEYS)
    ef_proc = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(ef_target)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert ef_proc.returncode == 0, ef_proc.stdout + ef_proc.stderr
    assert not (ef_target / "everflow_only.txt").exists()
    assert (ef_target / "skills" / "model-usage-cost" / "references" / "antigravity_series.json").is_file()
    for entry in everflow_only:
        assert (ef_target / entry).is_file(), f"everflow seed should install {entry}"
    for rel in generic_scripts:
        assert (ef_target / rel).is_file(), f"everflow seed should install {rel}"


def test_seed_preflight_single_file_only_and_requires_backend_custom_config(
    tmp_path: Path,
) -> None:
    """R4 #2 + R5 #2: preflight reads live backend container env, warns '需要重建后端' on .env drift,
    shares /workspace/ path mapping with container_runner, and checks only the single active file."""
    fake_repo = tmp_path / "fake_repo"
    for rel_src in (
        "src/dalton/__init__.py",
        "src/dalton/profile.py",
        "src/dalton/config_env.py",
        "src/dalton/data_jobs/__init__.py",
        "src/dalton/data_jobs/task_config.py",
        "src/dalton/data_jobs/workspace.dalton.yaml",
        "src/dalton/data_jobs/everflow_defaults.yaml",
        "scripts/dalton/seed_openclaw_data.sh",
        "scripts/dalton/data_assets/everflow_only.txt",
    ):
        dst = fake_repo / rel_src
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel_src, dst)
    shutil.copytree(
        DATA_ASSETS_SCRIPTS_DIR,
        fake_repo / "scripts" / "dalton" / "data_assets" / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    # Fake `docker` binary returning the running backend container's live .Config.Env from a state file
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    live_env_file = tmp_path / "backend_live.env"
    live_env_file.write_text("APP_ENV=dalton\n", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$1" == "inspect" ]]; then cat "{live_env_file}"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    # Put ALL required keys ONLY in backend repo `.env` (not in target var/config/data_tasks.env)
    (fake_repo / ".env").write_text(
        "APP_ENV=dalton\n" + "\n".join(f"{k}={v}" for k, v in _EVERFLOW_PREFLIGHT_KEYS.items()) + "\n",
        encoding="utf-8",
    )
    target = fake_repo / "var" / "dalton" / "openclaw-data"
    fake_seed = fake_repo / "scripts" / "dalton" / "seed_openclaw_data.sh"
    venv_bin = str(Path(sys.executable).parent)
    base_env = _clean_env(
        DALTON_BACKEND_CONTAINER="dalton-langalpha-backend-1",
        PATH=f"{fake_bin}{os.pathsep}{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    )

    # 1. Keys ONLY in backend `.env` -> preflight MUST fail
    res_dotenv = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_dotenv.returncode != 0
    assert "预检失败" in res_dotenv.stderr
    assert "DALTON_SEC_USER_AGENT" in res_dotenv.stderr

    # 2. Custom DALTON_DATA_TASKS_CONFIG written ONLY to `.env` while running backend still lacks it -> MUST fail with "需要重建后端"
    inside_cfg = _write_data_tasks_env(target, _EVERFLOW_PREFLIGHT_KEYS, rel="config/custom_prod.env")
    (fake_repo / ".env").write_text(
        f"APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG={inside_cfg}\n",
        encoding="utf-8",
    )
    live_env_file.write_text("APP_ENV=dalton\n", encoding="utf-8")
    res_need_recreate = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_need_recreate.returncode != 0
    assert "需要重建后端" in res_need_recreate.stderr

    # 2b. Running backend holds stale old path while `.env` was updated to new path -> MUST fail with "需要重建后端"
    live_env_file.write_text(
        "APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG=/workspace/config/old_prod.env\n",
        encoding="utf-8",
    )
    res_stale_backend = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_stale_backend.returncode != 0
    assert "需要重建后端" in res_stale_backend.stderr

    # 3. Custom DALTON_DATA_TASKS_CONFIG pointing OUTSIDE target mount (even in live backend) -> MUST fail
    outside_cfg = tmp_path / "outside_only.env"
    outside_cfg.write_text(
        "\n".join(f"{k}={v}" for k, v in _EVERFLOW_PREFLIGHT_KEYS.items()) + "\n",
        encoding="utf-8",
    )
    (fake_repo / ".env").write_text(
        f"APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG={outside_cfg}\n",
        encoding="utf-8",
    )
    live_env_file.write_text(
        f"APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG={outside_cfg}\n",
        encoding="utf-8",
    )
    res_outside = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env={**base_env, "DALTON_DATA_TASKS_CONFIG": str(outside_cfg)},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_outside.returncode != 0
    assert "预检失败" in res_outside.stderr

    # 4. Custom DALTON_DATA_TASKS_CONFIG passed ONLY to seed (absent from both .env and live backend) -> MUST fail!
    (fake_repo / ".env").write_text("APP_ENV=dalton\n", encoding="utf-8")
    live_env_file.write_text("APP_ENV=dalton\n", encoding="utf-8")
    res_seed_only_cfg = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env={**base_env, "DALTON_DATA_TASKS_CONFIG": str(inside_cfg)},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_seed_only_cfg.returncode != 0
    assert "预检失败" in res_seed_only_cfg.stderr
    assert "仅传给了 seed" in res_seed_only_cfg.stderr

    # 5. Required keys SPLIT across custom file AND default config/data_tasks.env -> MUST fail (no cross-file merging)!
    custom_partial = dict(_EVERFLOW_PREFLIGHT_KEYS)
    missing_from_custom = custom_partial.pop("DALTON_SEC_USER_AGENT")
    custom_partial.pop("DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID")
    _write_data_tasks_env(target, custom_partial, rel="config/custom_prod.env")
    _write_data_tasks_env(
        target,
        {
            "DALTON_SEC_USER_AGENT": missing_from_custom,
            "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID": _EVERFLOW_PREFLIGHT_KEYS[
                "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID"
            ],
        },
        rel="config/data_tasks.env",
    )
    (fake_repo / ".env").write_text(
        f"APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG={inside_cfg}\n",
        encoding="utf-8",
    )
    live_env_file.write_text(
        f"APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG={inside_cfg}\n",
        encoding="utf-8",
    )
    res_split = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env={**base_env, "DALTON_DATA_TASKS_CONFIG": str(inside_cfg)},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_split.returncode != 0
    assert "预检失败" in res_split.stderr
    assert "DALTON_SEC_USER_AGENT" in res_split.stderr
    assert "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID" in res_split.stderr

    # 6. Mismatched DALTON_DATA_TASKS_CONFIG between seed shell and live backend -> MUST fail
    other_cfg = _write_data_tasks_env(target, _EVERFLOW_PREFLIGHT_KEYS, rel="config/other_prod.env")
    res_mismatch = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env={**base_env, "DALTON_DATA_TASKS_CONFIG": str(other_cfg)},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert res_mismatch.returncode != 0
    assert "不一致" in res_mismatch.stderr

    # 7. Shared /workspace/ path mapping: `/workspace/config/custom_prod.env` maps identically in seed AND container_runner!
    (target / "config" / "data_tasks.env").unlink()
    _write_data_tasks_env(target, _EVERFLOW_PREFLIGHT_KEYS, rel="config/custom_prod.env")
    (fake_repo / ".env").write_text(
        "APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG=/workspace/config/custom_prod.env\n",
        encoding="utf-8",
    )
    live_env_file.write_text(
        "APP_ENV=dalton\nDALTON_DATA_TASKS_CONFIG=/workspace/config/custom_prod.env\n",
        encoding="utf-8",
    )
    res_workspace_prefix = subprocess.run(
        ["bash", str(fake_seed), "--apply", "--target", str(target)],
        cwd=str(fake_repo),
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res_workspace_prefix.returncode == 0, res_workspace_prefix.stdout + res_workspace_prefix.stderr

    # Verify container_runner maps `/workspace/config/custom_prod.env` via the exact same function to the Everflow bind mount!
    path_map = build_path_map(
        host_var_dir=fake_repo / "var" / "dalton",
        backend_var_dir=Path("/app/var/dalton"),
    )
    container_cfg = build_container_create_config(
        job_key="semi-rescue-alert-am",
        run_id="55555555-5555-4555-8555-555555555555",
        generation=1,
        command_argv=["python3", "scripts/semi_rescue_alert.py", "run", "--slot", "am", "--json"],
        path_map=path_map,
        env_source={"APP_ENV": "dalton", "DALTON_DATA_TASKS_CONFIG": "/workspace/config/custom_prod.env"},
    )
    container_env = dict(item.split("=", 1) for item in container_cfg["Env"])
    assert (
        container_env["DALTON_DATA_TASKS_CONFIG"]
        == f"{path_map.container_workspace_dir.as_posix()}/config/custom_prod.env"
    )


def test_duplicate_keys_last_wins_consistent_between_seed_and_tasks(tmp_path: Path) -> None:
    """R5 #3: duplicate keys in data_tasks.env consistently take the LAST entry in seed, Python tasks, and Bash tasks."""
    target = tmp_path / "dup_var"
    cfg = target / "config" / "data_tasks.env"
    cfg.parent.mkdir(parents=True, exist_ok=True)

    # 1. Key first set to a valid value, then cleared to empty -> MUST fail preflight AND fail tasks!
    lines = [f"{k}={v}" for k, v in _EVERFLOW_PREFLIGHT_KEYS.items()]
    lines.append("DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID=")
    lines.append("DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID=")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")

    seed_blocked = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(target)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert seed_blocked.returncode != 0
    assert "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID" in seed_blocked.stderr
    assert "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID" in seed_blocked.stderr

    etf_blocked = subprocess.run(
        [sys.executable, str(DATA_ASSETS_SCRIPTS_DIR / "etf_monthly_flows_tracker.py"), "--send"],
        cwd=str(target),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert etf_blocked.returncode != 0
    assert "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID" in (etf_blocked.stdout + etf_blocked.stderr)

    sentiment_blocked = subprocess.run(
        ["bash", str(DATA_ASSETS_SCRIPTS_DIR / "sentiment_dashboard_digest.sh")],
        cwd=str(target),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert sentiment_blocked.returncode != 0
    assert "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID" in (sentiment_blocked.stdout + sentiment_blocked.stderr)

    # 2. Key first empty, later populated -> seed preflight passes AND both Python & Bash read the last value!
    lines2 = [f"{k}={v}" for k, v in _EVERFLOW_PREFLIGHT_KEYS.items()]
    lines2.insert(0, "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID=")
    lines2.insert(0, "DALTON_DATA_TASK_BBG_HOST=")
    lines2.append("DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID=oc_last_wins_tracking")
    lines2.append("DALTON_DATA_TASK_BBG_HOST=bbg@last-wins.example")
    cfg.write_text("\n".join(lines2) + "\n", encoding="utf-8")

    seed_ok = subprocess.run(
        ["bash", str(SEED_SCRIPT), "--apply", "--target", str(target)],
        env=_clean_env(APP_ENV="dalton"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert seed_ok.returncode == 0, seed_ok.stdout + seed_ok.stderr

    py_val = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util\n"
                f"s = importlib.util.spec_from_file_location('etf_dup', {str(DATA_ASSETS_SCRIPTS_DIR / 'etf_monthly_flows_tracker.py')!r})\n"
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                "print(m.DEFAULT_EVE_TRACKING_CHAT_ID)\n"
            ),
        ],
        cwd=str(target),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert py_val.returncode == 0, py_val.stderr
    assert py_val.stdout.strip() == "oc_last_wins_tracking"

    bash_val = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"eval \"$(sed -n '/^_read_cfg_key()/,/^}}/p' {str(DATA_ASSETS_SCRIPTS_DIR / 'bbg_remote_refresh.sh')!r})\"\n"
                f'_read_cfg_key DALTON_DATA_TASK_BBG_HOST {str(cfg)!r}'
            ),
        ],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert bash_val.returncode == 0, bash_val.stderr
    assert bash_val.stdout == "bbg@last-wins.example"


@pytest.mark.asyncio
async def test_container_runner_forwards_all_keys_and_container_script_reads_them(
    tmp_path: Path,
) -> None:
    """R3 Lead #1 / Codex 高2 / Grok 高1: container_runner forwards SEC_USER_AGENT, notify keys, and custom config path."""
    ws = tmp_path / "var" / "dalton" / "openclaw-data"
    custom_cfg = _write_data_tasks_env(
        ws,
        {"DALTON_DATA_TASK_FEISHU_ASIA_TARGET": "chat:oc_from_custom_cfg"},
        rel="config/custom_tasks.env",
    )
    path_map = build_path_map(host_var_dir=tmp_path / "var" / "dalton", backend_var_dir=tmp_path / "var" / "dalton")

    backend_env = {
        "SEC_USER_AGENT": "AliasSecBot sec-alias@example.com",
        "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID": "oc_forwarded_tracking",
        "DALTON_DATA_TASK_DISCORD_DM_TARGET": "user:900000000000000001",
        "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET": "oc_forwarded_megacap",
        "DALTON_DATA_TASKS_CONFIG": str(custom_cfg),
    }
    cfg = build_container_create_config(
        job_key="semi-rescue-alert-am",
        run_id="22222222-2222-4222-8222-222222222222",
        generation=1,
        command_argv=["python3", "scripts/semi_rescue_alert.py", "run", "--slot", "am", "--json"],
        path_map=path_map,
        env_source=backend_env,
    )
    env_dict = dict(item.split("=", 1) for item in cfg["Env"])
    assert env_dict["SEC_USER_AGENT"] == "AliasSecBot sec-alias@example.com"
    assert env_dict["DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID"] == "oc_forwarded_tracking"
    assert env_dict["DALTON_DATA_TASK_DISCORD_DM_TARGET"] == "user:900000000000000001"
    assert env_dict["DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET"] == "oc_forwarded_megacap"
    # Host custom config path under host_data_root is translated to container workspace path
    assert env_dict["DALTON_DATA_TASKS_CONFIG"] == "/Users/everflow/.openclaw/workspace/config/custom_tasks.env"
    assert "DALTON_DATA_ASSETS_DIR" not in env_dict

    # Simulate container execution via runner_wrapper with forwarded env and local mounted workspace
    sim_env = dict(env_dict)
    sim_env["DALTON_DATA_TASKS_CONFIG"] = str(custom_cfg)
    probe = (
        "import importlib.util, json\n"
        "def load(name, path):\n"
        "    s = importlib.util.spec_from_file_location(name, path)\n"
        "    m = importlib.util.module_from_spec(s)\n"
        "    s.loader.exec_module(m)\n"
        "    return m\n"
        f"semi = load('semi', {str(DATA_ASSETS_SCRIPTS_DIR / 'semi_rescue_alert.py')!r})\n"
        f"cron = load('cron', {str(DATA_ASSETS_SCRIPTS_DIR / 'cron_command_tasks.py')!r})\n"
        f"pt = load('pt', {str(DATA_ASSETS_SCRIPTS_DIR / 'pt_watchlist_monitor.py')!r})\n"
        "print(json.dumps({\n"
        "    'sec_ua': semi._resolve_sec_user_agent({}),\n"
        "    'tracking': cron._require_tracking(),\n"
        "    'asia_from_custom_cfg': pt.resolve_feishu_target(),\n"
        "}))\n"
    )
    res = await run_wrapped_command(
        [sys.executable, "-c", probe],
        run_id="22222222-2222-4222-8222-222222222222",
        job_key="semi-rescue-alert-am",
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=None,
        outbox_tmp_dir=tmp_path / "tmpfs",
        base_env=sim_env,
    )
    assert res.exit_code == 0, res.redacted_stderr
    observed = json.loads(res.redacted_stdout.strip())
    assert observed == {
        "sec_ua": "AliasSecBot sec-alias@example.com",
        "tracking": "oc_forwarded_tracking",
        "asia_from_custom_cfg": "chat:oc_from_custom_cfg",
    }


@pytest.mark.asyncio
async def test_host_bridge_t58_missing_key_preserves_diagnostic_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 Codex 高2 + R5 #1: under Core profile, T58 missing DALTON_DATA_TASK_GS_PB_PYTHON returns diagnostic detail."""
    import scripts.dalton.researchdata_bridge as rb

    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("DALTON_PROFILE", "core")
    monkeypatch.delenv("DALTON_DATA_TASK_GS_PB_PYTHON", raising=False)
    monkeypatch.delenv("DALTON_DATA_TASKS_CONFIG", raising=False)
    cfg = rb.BridgeConfig()
    cfg.data_tasks_data_root = tmp_path / "openclaw-data"
    cfg.data_tasks_data_root.mkdir(parents=True)
    bridge = rb.ResearchDataBridge(cfg)

    manifest_entry = rb._load_host_job_manifest().by_job_key()["gs-pb-weekly-rundown"]
    status, body = await bridge.handle_data_task_host_run(
        {
            "generation": 1,
            "run_id": "run-t58-missing-key",
            "job_key": "gs-pb-weekly-rundown",
            "host_mode": "run",
            "timeout": 30,
            "no_output_timeout": 15,
            "command_argv": list(manifest_entry.command_argv),
            "cwd": manifest_entry.cwd,
        }
    )
    assert status == 400
    assert body["error"] == "host_command_rejected"
    assert "DALTON_DATA_TASK_GS_PB_PYTHON" in str(body.get("detail") or "")
    assert "未配置" in str(body.get("detail") or "")


@pytest.mark.asyncio
async def test_runtime_executes_workspace_scripts_without_repo_switch_or_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4 #1: container & host runtime continue executing <workspace>/scripts/ directly; no repo switch or overrides/scripts/."""
    import scripts.dalton.researchdata_bridge as rb
    from src.dalton.data_jobs import batch3_messages

    ws = tmp_path / "var_ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "scripts" / "run_json_task.sh").write_text(
        '#!/usr/bin/env bash\necho \'{"ok": true, "source": "workspace_scripts"}\'\n',
        encoding="utf-8",
    )
    # Plant a decoy under overrides/scripts/ — runtime MUST ignore it with zero override logs
    decoy_override = ws / "overrides" / "scripts" / "run_json_task.sh"
    decoy_override.parent.mkdir(parents=True)
    decoy_override.write_text(
        '#!/usr/bin/env bash\necho \'{"ok": true, "source": "decoy_override"}\'\n',
        encoding="utf-8",
    )

    # 1. Container wrapper executes <workspace>/scripts/run_json_task.sh directly
    ws_res = await run_wrapped_command(
        ["bash", "scripts/run_json_task.sh", "true"],
        run_id="33333333-3333-4333-8333-333333333331",
        job_key="quantum-ew-monitor",
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=None,
        outbox_tmp_dir=tmp_path / "tmpfs1",
        base_env=_clean_env(),
    )
    assert ws_res.exit_code == 0, ws_res.redacted_stderr
    assert json.loads(ws_res.redacted_stdout.strip()) == {"ok": True, "source": "workspace_scripts"}
    assert "ops override active" not in ws_res.redacted_stderr

    # 2. Host bridge executes <data_root>/scripts/bbg_remote_refresh.sh and relative scripts/gs_pb_weekly.py
    monkeypatch.setenv("DALTON_DATA_TASK_GS_PB_PYTHON", "/opt/test/gs-pb/bin/python")
    t1_spec = rb.resolve_host_run_command_spec(
        job_key="bbg-data-pull-asia",
        command_argv=["sh", "-c", "bash scripts/bbg_remote_refresh.sh"],
        cwd="/Users/everflow/.openclaw/workspace",
        data_root=ws,
        ocw_root=tmp_path / "ocw",
    )
    assert t1_spec["command_argv"] == ["bash", str(ws / "scripts" / "bbg_remote_refresh.sh")]

    t58_entry = rb._load_host_job_manifest().by_job_key()["gs-pb-weekly-rundown"]
    t58_spec = rb.resolve_host_run_command_spec(
        job_key="gs-pb-weekly-rundown",
        command_argv=list(t58_entry.command_argv),
        cwd=t58_entry.cwd,
        data_root=ws,
        ocw_root=tmp_path / "ocw",
    )
    assert t58_spec["command_argv"] == [
        "/opt/test/gs-pb/bin/python",
        "-I",
        "scripts/gs_pb_weekly.py",
        "run",
        "--json",
    ]

    # 3. T76 eve_tracking_mirror loads strictly from <workspace>/scripts/ (never silently from repo copy)
    assert batch3_messages._load_eve_tracking_mirror(ws) is None
    (ws / "scripts" / "eve_tracking_mirror.py").write_text(
        "MARKER = 'workspace_mirror'\n",
        encoding="utf-8",
    )
    mirror_mod = batch3_messages._load_eve_tracking_mirror(ws)
    assert mirror_mod is not None and mirror_mod.MARKER == "workspace_mirror"


@pytest.mark.asyncio
async def test_wrapper_nonzero_exit_reads_failure_notify_targets_from_workspace_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 Codex 中4: T38 and FDE weekly failure alerts still resolve targets from config/data_tasks.env on non-zero exit."""
    for key in (
        "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET",
        "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL",
        "DALTON_DATA_TASKS_CONFIG",
        "DALTON_DATA_TASK_WORKSPACE",
        "DALTON_WORKSPACE",
        "OPENCLAW_WORKSPACE",
        "WORKDIR",
    ):
        monkeypatch.delenv(key, raising=False)

    ws = tmp_path / "fail_notify_ws"
    _write_data_tasks_env(
        ws,
        {
            "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET": "user:ou_unit_test_ashare_cfg",
            "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL": "channel:300000000000000001",
        },
    )

    # 1. T38 exits 1 after printing the consecutive-failure alert
    t38_res = await run_wrapped_command(
        [
            sys.executable,
            "-c",
            'import sys; print("底部放量监控：腾讯行情已连续约 16 分钟取不到数据"); sys.exit(1)',
        ],
        run_id="44444444-4444-4444-8444-444444444441",
        job_key="a-share-bottom-volume-watch",
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=None,
        outbox_tmp_dir=tmp_path / "tmpfs_t38",
        base_env=_clean_env(),
    )
    assert t38_res.exit_code == 1
    assert t38_res.outbox_payload is not None
    assert t38_res.outbox_payload["notify_on_failure"] is True
    assert t38_res.outbox_payload["target"] == "user:ou_unit_test_ashare_cfg"
    assert t38_res.outbox_payload["route_target"] == "feishu:user:ou_unit_test_ashare_cfg"
    assert t38_res.outbox_path is not None and t38_res.outbox_path.is_file()

    # 2. T73 FDE weekly report exits 1 after printing the failure prefix
    t73_res = await run_wrapped_command(
        [
            sys.executable,
            "-c",
            'import sys; print("⚠️ FDE 周报生成失败（2026-11-27），最后 15 行错误：\\nboom"); sys.exit(1)',
        ],
        run_id="44444444-4444-4444-8444-444444444442",
        job_key="fde-hiring-weekly-report",
        workdir=str(ws),
        workspace_dir=str(ws),
        secrets_dir=None,
        outbox_tmp_dir=tmp_path / "tmpfs_t73",
        base_env=_clean_env(),
    )
    assert t73_res.exit_code == 1
    assert t73_res.outbox_payload is not None
    assert t73_res.outbox_payload["notify_on_failure"] is True
    assert t73_res.outbox_payload["target"] == "channel:300000000000000001"
    assert t73_res.outbox_path is not None and t73_res.outbox_path.is_file()


def test_drift_check_derived_baseline_writes_zero_bytecode_files(tmp_path: Path) -> None:
    """R3 Codex 中5: running drift check with derived Antigravity baseline never creates __pycache__ or .pyc."""
    checkout = tmp_path / "writable_checkout"
    repo_assets = checkout / "scripts" / "dalton" / "data_assets"
    var_dir = checkout / "var" / "dalton" / "openclaw-data"

    # Copy real usage_sources.py, antigravity_series.py, native_overlay.json, dalton.yaml WITHOUT any __pycache__
    for rel in (
        "scripts/dalton/data_assets/skills/model-usage-cost/scripts/usage_sources.py",
        "src/dalton/llm/antigravity_series.py",
        "src/dalton/llm/native_overlay.json",
        "src/dalton/dalton.yaml",
    ):
        dst = checkout / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst)
    (var_dir / "skills" / "model-usage-cost" / "scripts").mkdir(parents=True)
    shutil.copy2(
        checkout / "scripts/dalton/data_assets/skills/model-usage-cost/scripts/usage_sources.py",
        var_dir / "skills/model-usage-cost/scripts/usage_sources.py",
    )

    before = sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("*"))
    assert not any("__pycache__" in item or item.endswith(".pyc") for item in before)

    proc = subprocess.run(
        [
            sys.executable,
            str(DRIFT_SCRIPT),
            "--repo-dir",
            str(repo_assets),
            "--var-dir",
            str(var_dir),
            "--subdirs",
            "skills",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    after = sorted(p.relative_to(checkout).as_posix() for p in checkout.rglob("*"))
    assert after == before, f"drift check mutated directory tree: {set(after) - set(before)}"


def test_seed_core_initialized_release_with_only_dotenv_resolves_core_profile(
    tmp_path: Path,
) -> None:
    """R3 Codex 中6: initialized Core release with only .env (no shell DALTON_PROFILE) resolves core profile."""
    core_install = tmp_path / "core_release_tree"
    for rel_src in (
        "src/dalton/__init__.py",
        "src/dalton/profile.py",
        "src/dalton/config_env.py",
        "scripts/dalton/seed_openclaw_data.sh",
        "scripts/dalton/data_assets/everflow_only.txt",
    ):
        dst = core_install / rel_src
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel_src, dst)
    shutil.copytree(
        DATA_ASSETS_SCRIPTS_DIR,
        core_install / "scripts" / "dalton" / "data_assets" / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # Match scripts/standalone/init.sh:1099-1100 writing APP_ENV=core and DALTON_PROFILE=core into .env
    (core_install / ".env").write_text("APP_ENV=core\nDALTON_PROFILE=core\n", encoding="utf-8")

    target = tmp_path / "core_var"
    venv_bin = str(Path(sys.executable).parent)
    # Fresh shell with ZERO DALTON_PROFILE / APP_ENV / Everflow keys
    proc = subprocess.run(
        ["bash", str(core_install / "scripts" / "dalton" / "seed_openclaw_data.sh"), "--apply", "--target", str(target)],
        cwd=str(core_install),
        env=_clean_env(PATH=f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "Profile: core" in proc.stdout
    for entry in _load_everflow_only_entries():
        assert not (target / entry).exists()


def test_everflow_only_strips_softbank_tsmc_and_generic_core_has_no_feishu_card_dependency() -> None:
    """R3 Codex 中7: softbank_nav_monitor.py and tsmc_adr_premium.py are in everflow_only.txt; Core scripts never call feishu_send_card.py."""
    everflow_only = set(_load_everflow_only_entries())
    assert "scripts/softbank_nav_monitor.py" in everflow_only
    assert "scripts/tsmc_adr_premium.py" in everflow_only

    for path in sorted(DATA_ASSETS_SCRIPTS_DIR.iterdir()):
        if not path.is_file() or path.suffix in (".pyc", ".pyo"):
            continue
        rel = f"scripts/{path.name}"
        if rel in everflow_only:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        assert "feishu_send_card.py" not in content, (
            f"Generic Core script {rel} still depends on stripped feishu_send_card.py"
        )


def _snapshot_working_tree_repo(tmp_path: Path) -> Path:
    """Return REPO_ROOT if clean, else a temp git repo snapshotting HEAD + working-tree changes."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not status.strip():
        return REPO_ROOT

    snap = tmp_path / "working-tree-repo"
    snap.mkdir()
    # Export committed HEAD into snap, then overlay modified/untracked files and drop deleted ones.
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
    )
    try:
        subprocess.run(["tar", "-xf", "-", "-C", str(snap)], stdin=archive.stdout, check=True)
    finally:
        if archive.stdout is not None:
            archive.stdout.close()
        assert archive.wait() == 0

    for line in status.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        rel = line[3:].strip().strip('"')
        if " -> " in rel:
            old_rel, rel = rel.split(" -> ", 1)
            old_dst = snap / old_rel
            if old_dst.exists() or old_dst.is_symlink():
                old_dst.unlink()
        src = REPO_ROOT / rel
        dst = snap / rel
        if "D" in xy and not src.exists():
            if dst.exists() or dst.is_symlink():
                dst.unlink()
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    subprocess.run(["git", "init", "-q"], cwd=str(snap), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(snap), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(snap), check=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=str(snap), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "working-tree-snapshot"], cwd=str(snap), check=True)
    return snap


def test_package_release_real_repo_passes_zero_leak_gate_and_rejects_injected_leaks(
    tmp_path: Path,
) -> None:
    """R4 #3: full real-repo Core export passes zero-leak dir & tarball scan (0 hits for /Users/richardlu & /Users/everflow)."""
    source_repo = _snapshot_working_tree_repo(tmp_path)
    out_dir = tmp_path / "real-core-dist"
    tar_path = tmp_path / "real-core-dist.tar.gz"
    proc = subprocess.run(
        [
            "bash",
            str(PACKAGE_RELEASE_SCRIPT),
            "--repo",
            str(source_repo),
            "--ref",
            "HEAD",
            "--output",
            str(out_dir),
            "--tar",
            str(tar_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert tar_path.is_file() and tar_path.stat().st_size > 0
    assert not (out_dir / "scripts" / "standalone" / "package_release.sh").exists()
    assert not (out_dir / "scripts" / "standalone" / "verify_release_tarball.py").exists()
    assert not (out_dir / "src" / "dalton" / "data_jobs" / "workspace.dalton.yaml").exists()
    assert not (out_dir / "src" / "dalton" / "data_jobs" / "everflow_defaults.yaml").exists()
    bdc_skill = (out_dir / "plugins" / "dalton" / "skills" / "dalton-bdc-analyst" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "everflowinv" not in bdc_skill.lower()
    assert "sec-contact@example.com" in bdc_skill

    # Inject a forbidden pattern into a minimal git repo and assert packaging FAILS by default (even without --strict-scan)
    leaky_repo = tmp_path / "leaky-repo"
    leaky_repo.mkdir()
    for rel in (
        "LICENSE",
        "README.md",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "server.py",
        "alembic.ini",
        "config.yaml",
        "config.core.yaml",
        "agent_config.yaml",
        "agent_config.core.yaml",
        "docker-compose.yml",
        ".env.example",
        ".dockerignore",
        "Dockerfile.sandbox",
        "src/app.py",
        "libs/market-protocol/README.md",
        "libs/chartkit/README.md",
        "libs/edgar-engine/README.md",
        "migrations/README.md",
        "mcp_servers/README.md",
        "plugins/README.md",
        "plugins/alternative_data/README.md",
        "plugins/dalton/README.md",
        "plugins/firecrawl/README.md",
        "plugins/langalpha_deliverables/README.md",
        "plugins/langalpha_market_data/README.md",
        "plugins/langalpha_research/README.md",
        "plugins/langalpha_service/README.md",
        "plugins/yfinance/README.md",
        "workflows/README.md",
        "config/dalton/.gitkeep",
        "config/dalton/README.md",
        "deploy/Dockerfile.backend",
        "deploy/Dockerfile.dev",
        "deploy/Dockerfile.web",
        "scripts/configure.sh",
        "scripts/docker_entrypoint.sh",
        "scripts/_errors.py",
        "scripts/dalton/Dockerfile.dalton",
        "scripts/dalton/Dockerfile.postgres",
        "scripts/dalton/docker_entrypoint_dalton.sh",
        "scripts/dalton/configure_minio.sh",
        "scripts/dalton/log_redaction.py",
        "scripts/dalton/sql/personal_integrations_restore_reset.sql",
        "web/package.json",
        "docs-site/package.json",
    ):
        p = leaky_repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("ok\n", encoding="utf-8")

    # Inject synthetic forbidden patterns (zero real secrets/IDs): quoted Windows path, home prefixes, AND symlink target
    (leaky_repo / "src" / "app.py").write_text(
        'A = "/Users/" + "placeholder"\n'
        'LEAK_EVERFLOW = "/Users/everflow/any-subpath"\n'
        'LEAK_WIN_QUOTED = "D:/Eve"\n',
        encoding="utf-8",
    )
    os.symlink("/Users/richardlu/symlink-leak", leaky_repo / "src" / "bad_link")
    subprocess.run(["git", "init", "-q"], cwd=str(leaky_repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(leaky_repo), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(leaky_repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(leaky_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "leaky"], cwd=str(leaky_repo), check=True)

    leaky_out = tmp_path / "leaky-dist"
    leaky_proc = subprocess.run(
        [
            "bash",
            str(PACKAGE_RELEASE_SCRIPT),
            "--repo",
            str(leaky_repo),
            "--ref",
            "HEAD",
            "--output",
            str(leaky_out),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert leaky_proc.returncode != 0
    assert "failed zero-leak scan" in leaky_proc.stderr
    assert "personal_home_path" in leaky_proc.stdout
    assert "[symlink]" in leaky_proc.stdout
    assert "everflow_home_path" in leaky_proc.stdout
    assert "windows_eve_path" in leaky_proc.stdout
    assert not leaky_out.exists()


def test_release_scan_and_in_memory_tarball_reject_user_home_prefixes() -> None:
    """R4 #3 + R5 #3: in-memory .tar.gz rejects home prefixes, quoted Windows paths, symlink targets, and everflow_defaults.yaml."""
    verify_mod = _load_module(
        "verify_release_tarball_r4_mem",
        REPO_ROOT / "scripts" / "standalone" / "verify_release_tarball.py",
    )

    def _build_mem_tar_gz(
        files: dict[str, bytes],
        *,
        symlinks: dict[str, str] | None = None,
        top_dir: str = "dalton-core-test",
    ) -> io.BytesIO:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            top_info = tarfile.TarInfo(name=top_dir)
            top_info.type = tarfile.DIRTYPE
            top_info.mode = 0o755
            tf.addfile(top_info)
            for rel, content in files.items():
                info = tarfile.TarInfo(name=f"{top_dir}/{rel}")
                info.size = len(content)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(content))
            for rel, target in (symlinks or {}).items():
                link_info = tarfile.TarInfo(name=f"{top_dir}/{rel}")
                link_info.type = tarfile.SYMTYPE
                link_info.linkname = target
                tf.addfile(link_info)
        buf.seek(0)
        return buf

    # 1. Clean in-memory .tar.gz -> 0 violations (including non-matching C:/Events)
    clean_buf = _build_mem_tar_gz(
        {"src/manifest.yaml": b'cwd: "${DALTON_DATA_TASK_WORKSPACE}"\nlog: "C:/Events/ok.log"\n'}
    )
    assert verify_mod.inspect_tarball(fileobj=clean_buf, expected_top_dir="dalton-core-test") == []

    # 2. Bare /Users/richardlu prefix (not just a specific subdir) -> rejected
    personal_buf = _build_mem_tar_gz({"src/demo.yaml": b"path: /Users/richardlu/custom-dir\n"})
    personal_violations = verify_mod.inspect_tarball(
        fileobj=personal_buf, expected_top_dir="dalton-core-test"
    )
    assert any("personal_home_path" in v for v in personal_violations), personal_violations

    # 3. Bare /Users/everflow prefix (e.g. .openclaw/workspace, not just .local/share/everflow-tools) -> rejected
    everflow_buf = _build_mem_tar_gz({"src/manifest.yaml": b"cwd: /Users/everflow/.openclaw/workspace\n"})
    everflow_violations = verify_mod.inspect_tarball(
        fileobj=everflow_buf, expected_top_dir="dalton-core-test"
    )
    assert any("everflow_home_path" in v for v in everflow_violations), everflow_violations

    # 4. Quoted Windows paths ("D:/Eve" and 'C:\Eve') -> rejected
    quoted_win_buf = _build_mem_tar_gz({"src/demo.py": b'P1 = "D:/Eve"\nP2 = \'C:\\Eve\'\n'})
    quoted_win_violations = verify_mod.inspect_tarball(
        fileobj=quoted_win_buf, expected_top_dir="dalton-core-test"
    )
    assert len([v for v in quoted_win_violations if "windows_eve_path" in v]) == 2, quoted_win_violations

    # 5. Symlink target pointing to /Users/richardlu -> rejected
    symlink_buf = _build_mem_tar_gz(
        {"src/ok.py": b"x = 1\n"},
        symlinks={"src/bad_link": "/Users/richardlu/private"},
    )
    symlink_violations = verify_mod.inspect_tarball(
        fileobj=symlink_buf, expected_top_dir="dalton-core-test"
    )
    assert any("personal_home_path" in v and "link target" in v for v in symlink_violations), symlink_violations

    # 6. Any *.dalton.yaml overlay or everflow_defaults.yaml leaked into a tarball -> rejected
    overlay_buf = _build_mem_tar_gz(
        {
            "src/dalton/data_jobs/workspace.dalton.yaml": b"container_home: /home/x\n",
            "src/dalton/data_jobs/everflow_defaults.yaml": b"DALTON_DATA_TASK_GS_PB_PYTHON: /opt/py\n",
        }
    )
    overlay_violations = verify_mod.inspect_tarball(
        fileobj=overlay_buf, expected_top_dir="dalton-core-test"
    )
    assert any("workspace.dalton.yaml" in v for v in overlay_violations), overlay_violations
    assert any("everflow_defaults.yaml" in v for v in overlay_violations), overlay_violations
