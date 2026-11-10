"""OC5 PR1 Batch 1 behavior tests.

Covers:
1. Dedicated Bridge Feishu configuration (`DALTON_BRIDGE_ENV_FILE` / `FEISHU_*`) without
   `~/.openclaw/openclaw.json` or `DALTON_FEISHU_*` fallback, and `feishu_not_configured` error message.
2. Repeatable secure installer (`install_bridge.sh` + `bridge_subprocess_env.py`) preserving existing
   values/comments, atomic `0600` writes, empty-value handling, duplicate-key / malformed-line /
   partial-Feishu / invalid-value rejection, and write-failure safety without secret leakage.
3. Real subprocess final environment inspection (boolean key-presence maps only) across all 8
   non-archive Bridge subprocess entrypoints, the Batch 1 transitional card-send subprocess, both
   skill `run.sh` launchers (`company-wiki` and `market-digest`), `archive_daily.sh`,
   `conversation_archive.py` (including post-`load_dotenv()` re-filtering and model-conditional
   provider keys), and all 5 `reextract_conversations.py` subcommands (`plan`, `extract`, `apply`,
   `render`, `rollback`).
4. Controlled end-to-end archive extraction, BYOK credential decryption, and host `/embed` HTTP
   execution under the filtered archive allowlist environment, plus `/feishu/*` OpenAPI proxy
   coverage (token auth, auth/permission/business errors, doc read/write, image upload, sharing,
   wiki read/write, bot info, chats list).
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import date
import email.message
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec

from cryptography.fernet import Fernet
import httpx
import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import bridge_subprocess_env as bse  # noqa: E402
import researchdata_bridge as rb  # noqa: E402
import seed_asr_client as sac  # noqa: E402
import scripts.dalton.conversation_archive as archive_mod  # noqa: E402
import scripts.dalton.reextract_conversations as reextract_mod  # noqa: E402
from src.dalton.conversations import embedder as embedder_mod  # noqa: E402
from src.dalton.conversations.extractor import SegmentExtractionOutcome  # noqa: E402
from src.dalton.feishu import describe_feishu_error  # noqa: E402
from src.server.database.encryption import decrypt_value, encrypt_value  # noqa: E402

INSTALL_SH = SCRIPTS_DALTON / "install_bridge.sh"
ARCHIVE_DAILY_SH = SCRIPTS_DALTON / "launchd" / "archive_daily.sh"
WIKI_RUN_SH = SCRIPTS_DALTON / "openclaw_overlays" / "skills" / "company-wiki" / "run.sh"
DIGEST_RUN_SH = SCRIPTS_DALTON / "openclaw_overlays" / "skills" / "market-digest" / "run.sh"

FORBIDDEN_NONARCHIVE_CHECK_KEYS: tuple[str, ...] = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_DEFAULT_GRANT_CHAT_ID",
    "DALTON_FEISHU_APP_ID",
    "DALTON_FEISHU_APP_SECRET",
    "OPENCLAW_FEISHU_APP_ID",
    "OPENCLAW_FEISHU_APP_SECRET",
    "LARK_APP_ID",
    "LARK_APP_SECRET",
    "OPENCLAW_GATEWAY_TOKEN",
    "DALTON_OPENCLAW_GATEWAY_TOKEN",
    "DALTON_BROKER_TOKEN",
    "DALTON_WEB_SEARCH_BRIDGE_TOKEN",
    "SEED_ASR_APP_ID",
    "SEED_ASR_ACCESS_KEY",
    "VOLC_TOS_AK",
    "VOLC_TOS_SK",
    "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    "DALTON_BRIDGE_ADMIN_TOKEN",
    "DALTON_BRIDGE_TOKEN",
)


def _PoisonEnvDict() -> dict[str, str]:
    """Synthetic credential dictionary used only in isolated subprocess tests."""
    return {
        "FEISHU_APP_ID": "cli_synth_poison_id",
        "FEISHU_APP_SECRET": "sec_synth_poison_secret",
        "FEISHU_DEFAULT_GRANT_CHAT_ID": "oc_synth_poison_chat",
        "DALTON_FEISHU_APP_ID": "cli_synth_dalton_feishu_id",
        "DALTON_FEISHU_APP_SECRET": "sec_synth_dalton_feishu_secret",
        "OPENCLAW_FEISHU_APP_ID": "cli_synth_ocw_feishu_id",
        "OPENCLAW_FEISHU_APP_SECRET": "sec_synth_ocw_feishu_secret",
        "LARK_APP_ID": "cli_synth_lark_id",
        "LARK_APP_SECRET": "sec_synth_lark_secret",
        "OPENCLAW_GATEWAY_TOKEN": "tok_synth_ocw_gateway",
        "DALTON_OPENCLAW_GATEWAY_TOKEN": "tok_synth_dalton_ocw_gateway",
        "DALTON_BROKER_TOKEN": "tok_synth_broker",
        "DALTON_WEB_SEARCH_BRIDGE_TOKEN": "tok_synth_ws_bridge",
        "SEED_ASR_APP_ID": "asr_synth_app",
        "SEED_ASR_ACCESS_KEY": "asr_synth_ak",
        "VOLC_TOS_AK": "tos_synth_ak",
        "VOLC_TOS_SK": "tos_synth_sk",
        "VOLC_TOS_BUCKET": "tos_synth_bucket",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "storage_synth_sk",
        "DALTON_BRIDGE_ADMIN_TOKEN": "tok_synth_bridge_admin",
        "DALTON_BRIDGE_TOKEN": "tok_synth_bridge_mcp",
    }


def _write_shell_rc_poison(path: Path) -> Path:
    """Write a synthetic shell rc file that re-exports forbidden credentials."""
    lines = ["#!/usr/bin/env bash"]
    for k, v in _PoisonEnvDict().items():
        lines.append(f"export {k}='{v}_from_shell_rc'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_key_presence_probe_py(path: Path, keys: tuple[str, ...] = FORBIDDEN_NONARCHIVE_CHECK_KEYS) -> Path:
    """Write a Python script that outputs ONLY boolean key-presence JSON (never values)."""
    code = (
        "#!/usr/bin/env python3\n"
        "import json, os\n"
        f"keys = {list(keys)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "print(json.dumps({'ok': True, 'presence': presence}, sort_keys=True))\n"
    )
    path.write_text(code, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_installer_stubs(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "stub_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "stub_calls.log"

    launchctl_stub = bin_dir / "launchctl"
    launchctl_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'launchctl %s\\n' \"$*\" >> {str(log_file)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    launchctl_stub.chmod(0o755)

    curl_stub = bin_dir / "curl"
    curl_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'curl %s\\n' \"$*\" >> {str(log_file)!r}\n"
        "printf '{\"ok\":true,\"status\":\"healthy\"}\\n'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    curl_stub.chmod(0o755)
    return launchctl_stub, curl_stub, log_file


# ---------------------------------------------------------------------------
# 1. Installer (`install_bridge.sh` & `bridge_subprocess_env.py`) behavior tests
# ---------------------------------------------------------------------------


def test_install_bridge_first_run_repeat_merge_permissions_and_error_handling(
    tmp_path: Path,
) -> None:
    """Exercise the real `install_bridge.sh` in temporary directories:
    - First install creates 0600 env files and 0644 plist without leaking secrets to logs/stdout.
    - Repeat install preserves comments, existing tokens, and FEISHU_* credentials while filling
      only missing or empty default keys.
    - Rejects partial Feishu config, duplicate keys, malformed lines, and invalid required values
      naming the key/line only and leaving existing files intact.
    - Preserves original 0600 file untouched when atomic write fails in a read-only directory.
    """
    launchctl_stub, curl_stub, stub_log = _make_installer_stubs(tmp_path)
    home_dir = tmp_path / "home"
    config_dir = home_dir / ".config" / "dalton"
    bridge_env = config_dir / "researchdata-bridge.env"
    repo_env = tmp_path / "repo.env"
    launch_agents = home_dir / "Library" / "LaunchAgents"
    logs_dir = home_dir / "Library" / "Logs"
    ocw_root = tmp_path / "ocw_workspace"
    ocw_root.mkdir(parents=True, exist_ok=True)

    base_env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home_dir),
        "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
        "DALTON_REPO_ENV_FILE": str(repo_env),
        "DALTON_LAUNCH_AGENTS_DIR": str(launch_agents),
        "DALTON_BRIDGE_LOGS_DIR": str(logs_dir),
        "DALTON_OCW_ROOT": str(ocw_root),
        "DALTON_OCW_PYTHON": sys.executable,
        "LAUNCHCTL_BIN": str(launchctl_stub),
        "CURL_BIN": str(curl_stub),
        "DALTON_BRIDGE_HEALTH_RETRIES": "2",
        "DALTON_BRIDGE_HEALTH_SLEEP_S": "0",
    }

    # 1. First install from scratch
    res1 = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr
    assert bridge_env.is_file()
    assert repo_env.is_file()
    plist_file = launch_agents / "com.dalton.researchdata-bridge.plist"
    assert plist_file.is_file()

    assert stat.S_IMODE(bridge_env.stat().st_mode) == 0o600
    assert stat.S_IMODE(repo_env.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700

    _, bridge_vals_1 = bse.parse_env_file_strict(bridge_env)
    _, repo_vals_1 = bse.parse_env_file_strict(repo_env)
    tok_1 = bridge_vals_1["DALTON_BRIDGE_TOKEN"]
    admin_tok_1 = bridge_vals_1["DALTON_BRIDGE_ADMIN_TOKEN"]
    assert len(tok_1) == 64
    assert len(admin_tok_1) == 64
    assert repo_vals_1["DALTON_BRIDGE_TOKEN"] == tok_1
    assert repo_vals_1["DALTON_BRIDGE_ADMIN_TOKEN"] == admin_tok_1
    assert repo_vals_1["DALTON_RESEARCHDATA_URL"] == "http://host.docker.internal:8960/mcp"
    assert tok_1 not in repo_vals_1["DALTON_RESEARCHDATA_URL"]

    # Secrets must NEVER appear in stdout, stderr, or launchctl/curl stub logs
    combined_out_1 = res1.stdout + res1.stderr + stub_log.read_text(encoding="utf-8")
    assert tok_1 not in combined_out_1
    assert admin_tok_1 not in combined_out_1

    # 2. Customize bridge_env with comments, dedicated Feishu credentials, custom token, empty default key, and missing key
    custom_bridge_text = (
        "# Dedicated Dalton Bridge config comment\n"
        "DALTON_BRIDGE_HOST=127.0.0.1\n"
        "DALTON_BRIDGE_PORT=8960\n"
        "DALTON_BRIDGE_TOKEN=custom_preserved_mcp_token_abc\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=custom_preserved_admin_token_xyz\n"
        f"DALTON_OCW_ROOT={ocw_root}\n"
        f"DALTON_OCW_PYTHON={sys.executable}\n"
        "DALTON_BRIDGE_MAX_INFLIGHT=\n"
        "FEISHU_APP_ID=cli_dedicated_keep_me\n"
        "FEISHU_APP_SECRET=sec_dedicated_keep_me\n"
        "FEISHU_DEFAULT_GRANT_CHAT_ID=\n"
    )
    bridge_env.write_text(custom_bridge_text, encoding="utf-8")
    os.chmod(bridge_env, 0o600)

    repo_env.write_text(
        "# Repo env comment\n"
        "DALTON_BRIDGE_TOKEN=repo_existing_mcp_tok\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=\n"
        "DALTON_RESEARCHDATA_URL=http://host.docker.internal:8960/mcp\n",
        encoding="utf-8",
    )
    os.chmod(repo_env, 0o600)

    res2 = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr
    assert stat.S_IMODE(bridge_env.stat().st_mode) == 0o600
    assert stat.S_IMODE(repo_env.stat().st_mode) == 0o600

    merged_bridge_raw = bridge_env.read_text(encoding="utf-8")
    assert "# Dedicated Dalton Bridge config comment" in merged_bridge_raw
    _, bridge_vals_2 = bse.parse_env_file_strict(bridge_env)
    assert bridge_vals_2["DALTON_BRIDGE_TOKEN"] == "custom_preserved_mcp_token_abc"
    assert bridge_vals_2["DALTON_BRIDGE_ADMIN_TOKEN"] == "custom_preserved_admin_token_xyz"
    assert bridge_vals_2["FEISHU_APP_ID"] == "cli_dedicated_keep_me"
    assert bridge_vals_2["FEISHU_APP_SECRET"] == "sec_dedicated_keep_me"
    assert bridge_vals_2["FEISHU_DEFAULT_GRANT_CHAT_ID"] == ""
    assert bridge_vals_2["DALTON_BRIDGE_MAX_INFLIGHT"] == "2"  # empty default filled
    assert bridge_vals_2["DALTON_BRIDGE_CALLER_BUDGET_S"] == "625"  # missing default appended

    _, repo_vals_2 = bse.parse_env_file_strict(repo_env)
    assert repo_vals_2["DALTON_BRIDGE_TOKEN"] == "repo_existing_mcp_tok"  # existing non-empty preserved
    assert repo_vals_2["DALTON_BRIDGE_ADMIN_TOKEN"] == "custom_preserved_admin_token_xyz"  # empty filled
    assert repo_vals_2["SEC_USER_AGENT"] == "DaltonResearch research@dalton.local"

    combined_out_2 = res2.stdout + res2.stderr + stub_log.read_text(encoding="utf-8")
    for secret_str in (
        "custom_preserved_mcp_token_abc",
        "custom_preserved_admin_token_xyz",
        "sec_dedicated_keep_me",
        "repo_existing_mcp_tok",
    ):
        assert secret_str not in combined_out_2

    # 3. Partial Feishu credentials (FEISHU_APP_ID set, FEISHU_APP_SECRET empty) -> fails closed, names FEISHU_APP_SECRET
    snapshot_repo = repo_env.read_text(encoding="utf-8")
    partial_feishu_text = merged_bridge_raw.replace(
        "FEISHU_APP_SECRET=sec_dedicated_keep_me",
        "FEISHU_APP_SECRET=",
    )
    bridge_env.write_text(partial_feishu_text, encoding="utf-8")
    res_partial = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_partial.returncode != 0
    assert "FEISHU_APP_SECRET" in res_partial.stderr
    assert "cli_dedicated_keep_me" not in (res_partial.stdout + res_partial.stderr)
    assert bridge_env.read_text(encoding="utf-8") == partial_feishu_text
    assert repo_env.read_text(encoding="utf-8") == snapshot_repo

    # 4. Duplicate key -> fails closed, names duplicate key and line number
    dup_text = merged_bridge_raw + "DALTON_BRIDGE_PORT=8961\n"
    bridge_env.write_text(dup_text, encoding="utf-8")
    res_dup = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_dup.returncode != 0
    assert "DALTON_BRIDGE_PORT" in res_dup.stderr
    assert "duplicate key" in res_dup.stderr
    assert bridge_env.read_text(encoding="utf-8") == dup_text

    # 5. Malformed line without '=' -> fails closed without echoing line content
    malformed_secret = "MALFORMED_LINE_SECRET_VALUE_999"
    malformed_text = merged_bridge_raw + f"{malformed_secret}\n"
    bridge_env.write_text(malformed_text, encoding="utf-8")
    res_mal = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_mal.returncode != 0
    assert "malformed line" in res_mal.stderr
    assert malformed_secret not in (res_mal.stdout + res_mal.stderr)
    assert bridge_env.read_text(encoding="utf-8") == malformed_text

    # 6. Invalid required value (DALTON_BRIDGE_PORT=70000 and non-executable DALTON_OCW_PYTHON)
    bad_port_text = merged_bridge_raw.replace("DALTON_BRIDGE_PORT=8960", "DALTON_BRIDGE_PORT=70000")
    bridge_env.write_text(bad_port_text, encoding="utf-8")
    res_port = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_port.returncode != 0
    assert "DALTON_BRIDGE_PORT" in res_port.stderr
    assert bridge_env.read_text(encoding="utf-8") == bad_port_text

    bad_py_text = merged_bridge_raw.replace(
        f"DALTON_OCW_PYTHON={sys.executable}",
        f"DALTON_OCW_PYTHON={tmp_path / 'missing_python'}",
    )
    bridge_env.write_text(bad_py_text, encoding="utf-8")
    res_py = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_py.returncode != 0
    assert "DALTON_OCW_PYTHON" in res_py.stderr

    # 7. Write failure when directory is read-only leaves existing 0600 file untouched and no temp files behind
    bridge_env.write_text(merged_bridge_raw, encoding="utf-8")
    os.chmod(bridge_env, 0o600)
    os.chmod(config_dir, 0o500)
    try:
        res_ro = subprocess.run(
            ["/bin/bash", str(INSTALL_SH)],
            env=base_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_ro.returncode != 0
        assert "sec_dedicated_keep_me" not in (res_ro.stdout + res_ro.stderr)
    finally:
        os.chmod(config_dir, 0o700)
    assert bridge_env.read_text(encoding="utf-8") == merged_bridge_raw
    assert stat.S_IMODE(bridge_env.stat().st_mode) == 0o600
    assert list(config_dir.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# 2. Subprocess final environment verification across all entrypoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_eight_nonarchive_bridge_entries_and_skill_launchers_strip_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn controlled subprocesses through all 8 non-archive Bridge entrypoints, the
    transitional card-send subprocess, and both skill `run.sh` launchers while poisoning
    parent `os.environ` and shell rc (`DALTON_SKILL_SHELL_RC` / `BASH_ENV`).
    Inspect only boolean key-presence maps after child initialization.
    """
    for k, v in _PoisonEnvDict().items():
        monkeypatch.setenv(k, v)

    shell_rc = _write_shell_rc_poison(tmp_path / "poison_rc.sh")
    monkeypatch.setenv("DALTON_SKILL_SHELL_RC", str(shell_rc))
    monkeypatch.setenv("BASH_ENV", str(shell_rc))

    ocw_root = tmp_path / "ocw_workspace"
    (ocw_root / "scripts").mkdir(parents=True, exist_ok=True)
    (ocw_root / "skills" / "employee-reviews" / "scripts").mkdir(parents=True, exist_ok=True)
    (ocw_root / "skills" / "employee-reviews" / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (ocw_root / "skills" / "company-wiki" / "scripts").mkdir(parents=True, exist_ok=True)
    (ocw_root / "skills" / "company-wiki" / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (ocw_root / "skills" / "market-digest" / "scripts").mkdir(parents=True, exist_ok=True)
    (ocw_root / "skills" / "market-digest" / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    (ocw_root / "wiki" / "companies" / "NVDA").mkdir(parents=True, exist_ok=True)

    probe_py = _write_key_presence_probe_py(tmp_path / "probe_env.py")

    # Symlink Python into skill venvs so Bridge uses real Python to execute controlled CLI probes
    for venv_py in (
        ocw_root / "skills" / "employee-reviews" / "venv" / "bin" / "python",
        ocw_root / "skills" / "company-wiki" / "venv" / "bin" / "python",
        ocw_root / "skills" / "market-digest" / "venv" / "bin" / "python",
    ):
        venv_py.symlink_to(sys.executable)

    # 1. MCP stdio child (`_run_stdio_rpc` :6259)
    mcp_script = ocw_root / "scripts" / "researchdata_mcp.py"
    mcp_script.write_text(
        "import json, os, sys\n"
        f"keys = {list(FORBIDDEN_NONARCHIVE_CHECK_KEYS)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "for line in sys.stdin:\n"
        "    if line.strip():\n"
        "        req = json.loads(line)\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id', 1), 'result': {'presence': presence}}), flush=True)\n",
        encoding="utf-8",
    )

    # 2. Xueqiu / Employee CLI (`_build_community_subprocess_env` :7762)
    xueqiu_script = ocw_root / "scripts" / "xueqiu_posts.py"
    xueqiu_script.write_text(probe_py.read_text(encoding="utf-8"), encoding="utf-8")
    emp_script = ocw_root / "skills" / "employee-reviews" / "scripts" / "employee_reviews_cli.py"
    emp_script.write_text(probe_py.read_text(encoding="utf-8"), encoding="utf-8")

    # 3. xreach binary (`probe_xreach_version` :7859)
    xreach_bin = tmp_path / "xreach"
    xreach_bin.write_text(
        f"#!{sys.executable}\n" + probe_py.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    xreach_bin.chmod(0o755)

    # 4. company-wiki CLI (used by `/embed` :10337 and `company-wiki/run.sh`)
    wiki_cli = ocw_root / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py"
    wiki_cli.write_text(
        "import json, os, sys\n"
        f"keys = {list(FORBIDDEN_NONARCHIVE_CHECK_KEYS + ('GEMINI_API_KEY',))!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "def load_config():\n"
        "    return {'embedding_model': 'gemini-embedding-001', 'embedding_dimensions': 2}\n"
        "def get_embeddings(texts, cfg, task_type='RETRIEVAL_DOCUMENT'):\n"
        "    if any(presence[k] for k in keys if k != 'GEMINI_API_KEY'):\n"
        "        raise RuntimeError('forbidden credential leaked into embed subprocess')\n"
        "    return [[1.0 if presence.get('GEMINI_API_KEY') else 0.0, 0.5] for _ in texts]\n"
        "if __name__ == '__main__':\n"
        "    print(json.dumps({'ok': True, 'status': 'ingested', 'presence': presence}))\n",
        encoding="utf-8",
    )
    (ocw_root / "skills" / "company-wiki" / "config.json").write_text(
        json.dumps({"wiki_dir": "wiki", "embedding_model": "gemini-embedding-001"}),
        encoding="utf-8",
    )

    # 5. market-digest CLI (used by `market-digest/run.sh`)
    digest_cli = ocw_root / "skills" / "market-digest" / "scripts" / "market_digest_cli.py"
    digest_cli.write_text(
        "import json, os\n"
        f"keys = {list(FORBIDDEN_NONARCHIVE_CHECK_KEYS + ('GEMINI_API_KEY',))!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "print(json.dumps({'ok': True, 'status': 'added', 'note_id': 'N1', 'presence': presence}))\n",
        encoding="utf-8",
    )

    bridge_env_file = tmp_path / "bridge.env"
    bridge_env_file.write_text(
        "DALTON_BRIDGE_TOKEN=bridge_mcp_tok_synth\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=bridge_admin_tok_synth\n"
        "GEMINI_API_KEY=gemini_synth_key_for_wiki\n"
        "FEISHU_APP_ID=cli_bridge_configured_app\n"
        "FEISHU_APP_SECRET=sec_bridge_configured_secret\n"
        "FEISHU_DEFAULT_GRANT_CHAT_ID=oc_bridge_configured_chat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env_file))
    cfg = rb.BridgeConfig()
    cfg.python_bin = sys.executable
    cfg.ocw_root = ocw_root
    cfg.mcp_script = mcp_script
    cfg.xueqiu_script = xueqiu_script
    cfg.employee_python = ocw_root / "skills" / "employee-reviews" / "venv" / "bin" / "python"
    cfg.employee_cli_script = emp_script
    cfg.xreach_bin = xreach_bin
    data_root = tmp_path / "openclaw_data"
    (data_root / "scripts").mkdir(parents=True, exist_ok=True)
    cfg.data_tasks_data_root = data_root
    bridge = rb.ResearchDataBridge(cfg)

    def _assert_all_forbidden_absent(presence: dict[str, bool], context: str) -> None:
        leaked = [k for k in FORBIDDEN_NONARCHIVE_CHECK_KEYS if presence.get(k)]
        assert leaked == [], f"{context} leaked forbidden keys: {leaked}"

    # Entry 1: MCP stdio subprocess (`_run_stdio_rpc`)
    mcp_res = await bridge._run_stdio_rpc(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
        timeout_s=10.0,
    )
    _assert_all_forbidden_absent(mcp_res[0]["result"]["presence"], "MCP stdio")

    # Entry 2 & 3: MCP one-shot community (`xueqiu`) & employee CLI (`employee`)
    comm_xq = await bridge._run_community_route(
        req_id=1,
        route="xueqiu",
        arguments={"route": "xueqiu", "operation": "posts", "ticker": "00700"},
        eff_timeout=10,
    )
    _assert_all_forbidden_absent(
        comm_xq["result"]["structuredContent"]["data"]["presence"],
        "community xueqiu",
    )
    comm_emp = await bridge._run_community_route(
        req_id=2,
        route="employee",
        arguments={"route": "employee", "operation": "blind", "company": "NVIDIA"},
        eff_timeout=10,
    )
    _assert_all_forbidden_absent(
        comm_emp["result"]["structuredContent"]["data"]["presence"],
        "employee CLI",
    )

    # Entry 4: xreach version probe (`probe_xreach_version`)
    xr_res = await bridge.probe_xreach_version()
    assert xr_res["ok"] is True
    xr_parsed = json.loads(xr_res["version"])
    _assert_all_forbidden_absent(xr_parsed["presence"], "probe_xreach_version")

    # Entry 5: Wiki embedding (`handle_embed`)
    emb_code, emb_body = await bridge.handle_embed({"texts": ["hello world"]})
    assert emb_code == 200 and emb_body["ok"] is True
    assert emb_body["embeddings"] == [[1.0, 0.5]]

    # Entry 6 & 7: Host data task gate & BBG refresh child (`handle_host_task_run` + `_run_host_task_gate_cli`)
    bbg_probe_sh = data_root / "scripts" / "bbg_remote_refresh.sh"
    bbg_out_file = tmp_path / "bbg_child_presence.json"
    bbg_probe_sh.write_text(
        "#!/usr/bin/env bash\n"
        f"{sys.executable} {probe_py} > {bbg_out_file}\n"
        'echo \'{"ok": true, "lines": 300}\'\n',
        encoding="utf-8",
    )
    bbg_probe_sh.chmod(0o755)
    gate_res = subprocess.run(
        [
            sys.executable,
            "-u",
            str(SCRIPTS_DALTON / "researchdata_bridge.py"),
            rb.HOST_TASK_GATE_FLAG,
            json.dumps(
                {
                    "mode": "command",
                    "command_argv": ["bash", str(bbg_probe_sh)],
                    "cwd": str(data_root),
                }
            ),
        ],
        input="GO\n",
        env=bse.build_nonarchive_subprocess_env(os.environ),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert gate_res.returncode == 0
    bbg_presence = json.loads(bbg_out_file.read_text(encoding="utf-8"))["presence"]
    _assert_all_forbidden_absent(bbg_presence, "host task gate / BBG refresh")

    # Entry 8: Ops launchctl probe (`_probe_launchd_label_status`) & ps probe (`_list_host_processes_ps`)
    bin_dir = tmp_path / "ops_stub_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ops_presence_file = tmp_path / "ops_launchctl_presence.json"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"keys = {list(FORBIDDEN_NONARCHIVE_CHECK_KEYS)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        f"open({str(ops_presence_file)!r}, 'w').write(json.dumps(presence))\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o755)
    with monkeypatch.context() as mp:
        mp.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
        assert bridge._probe_launchd_label_status("com.dalton.test") == "on"
    _assert_all_forbidden_absent(
        json.loads(ops_presence_file.read_text(encoding="utf-8")),
        "_probe_launchd_label_status",
    )

    # Both skill `run.sh` launchers (`company-wiki/run.sh` and `market-digest/run.sh`)
    for launcher_name, launcher_sh in (
        ("company-wiki/run.sh", WIKI_RUN_SH),
        ("market-digest/run.sh", DIGEST_RUN_SH),
    ):
        skill_env = bridge._build_wiki_subprocess_env()
        skill_env["DALTON_SKILL_SHELL_RC"] = str(shell_rc)
        skill_env["BASH_ENV"] = str(shell_rc)
        skill_res = subprocess.run(
            ["/bin/bash", str(launcher_sh), "--json", "test"],
            cwd=str(ocw_root),
            env=skill_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert skill_res.returncode == 0, skill_res.stderr
        parsed_skill = json.loads(skill_res.stdout.strip().splitlines()[-1])
        _assert_all_forbidden_absent(parsed_skill["presence"], launcher_name)
        assert parsed_skill["presence"]["GEMINI_API_KEY"] is True

    # Batch 1 transitional card-send subprocess (`_run_feishu_send_card`):
    # retains explicit FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_DEFAULT_GRANT_CHAT_ID,
    # strips ASR/TOS, gateway/broker, DALTON_FEISHU_*, OPENCLAW_FEISHU_*, bridge tokens, and DALTON_NO_DIRECT_SEND.
    card_probe_out = tmp_path / "card_send_presence.json"
    fake_card_script = tmp_path / "fake_feishu_send_card.py"
    card_check_keys = FORBIDDEN_NONARCHIVE_CHECK_KEYS + ("DALTON_NO_DIRECT_SEND",)
    fake_card_script.write_text(
        "import json, os\n"
        f"keys = {list(card_check_keys)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "explicit_match = (\n"
        "    os.environ.get('FEISHU_APP_ID') == 'cli_bridge_configured_app'\n"
        "    and os.environ.get('FEISHU_APP_SECRET') == 'sec_bridge_configured_secret'\n"
        "    and os.environ.get('FEISHU_DEFAULT_GRANT_CHAT_ID') == 'oc_bridge_configured_chat'\n"
        ")\n"
        f"open({str(card_probe_out)!r}, 'w').write(json.dumps({{'presence': presence, 'explicit_match': explicit_match}}))\n"
        "print(json.dumps({'status': 'ok'}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_NO_DIRECT_SEND", "1")
    card_res = rb._run_feishu_send_card(
        script_path=fake_card_script,
        receive_id="ou_test_receiver",
        card={"schema": "2.0", "body": {"elements": []}},
        feishu_app_id=cfg.feishu_app_id,
        feishu_app_secret=cfg.feishu_app_secret,
        feishu_default_grant_chat_id=cfg.feishu_default_grant_chat_id,
    )
    assert card_res["ok"] is True
    card_report = json.loads(card_probe_out.read_text(encoding="utf-8"))
    assert card_report["explicit_match"] is True
    card_pres = card_report["presence"]
    assert card_pres["FEISHU_APP_ID"] is True
    assert card_pres["FEISHU_APP_SECRET"] is True
    assert card_pres["FEISHU_DEFAULT_GRANT_CHAT_ID"] is True
    for stripped_in_card in (
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "OPENCLAW_FEISHU_APP_ID",
        "OPENCLAW_FEISHU_APP_SECRET",
        "LARK_APP_ID",
        "LARK_APP_SECRET",
        "OPENCLAW_GATEWAY_TOKEN",
        "DALTON_OPENCLAW_GATEWAY_TOKEN",
        "DALTON_BROKER_TOKEN",
        "DALTON_WEB_SEARCH_BRIDGE_TOKEN",
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_BRIDGE_TOKEN",
        "DALTON_NO_DIRECT_SEND",
    ):
        assert card_pres[stripped_in_card] is False, f"card-send leaked {stripped_in_card}"


def test_archive_and_reextract_subprocess_env_allowlist_and_post_dotenv_refiltering(
    tmp_path: Path,
) -> None:
    """Verify `archive_daily.sh`, `conversation_archive.py`, and `reextract_conversations.py`:
    - Retain ONLY §2.6.1.4 allowlist keys and the actually selected models' provider keys.
    - Strip unselected provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `FMP_API_KEY`, `TAVILY_API_KEY`)
      and forbidden credentials even when re-injected by `load_dotenv()` from `<cwd>/.env`.
    - Restore host `DALTON_BRIDGE_EMBED_URL=http://127.0.0.1:8960/embed` and `no_proxy`.
    - Fail closed (exit 1) when required or model-conditional keys (e.g. `DALTON_XAI_OAUTH_CLIENT_ID`
      for `grok-4.7-oauth`) are missing or `DALTON_OCW_AGENTS_ROOT` is invalid.
    - Scope `reextract_conversations.py` environment per subcommand (`plan`, `extract`, `apply`, `render`, `rollback`).
    """
    dotenv_dir = tmp_path / "dotenv_cwd"
    dotenv_dir.mkdir(parents=True, exist_ok=True)
    ocw_agents_dir = tmp_path / "ocw_agents"
    ocw_agents_dir.mkdir(parents=True, exist_ok=True)

    # Synthetic `.env` in `dotenv_dir` that `python-dotenv` (`find_dotenv(usecwd=True)`) loads on `import src.config`
    synth_repo_env = dotenv_dir / ".env"
    synth_repo_env.write_text(
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=5432\n"
        "DB_NAME=dalton_synth\n"
        "DB_USER=postgres\n"
        "DB_PASSWORD=synth_db_pass\n"
        "BYOK_ENCRYPTION_KEY=c3ludGgtYnlvay1rZXktMzItYnl0ZXMtbG9uZy0wMDA=\n"
        "AUTH_USER_ID=11111111-1111-4111-8111-111111111111\n"
        "DALTON_PM_USER_IDS=11111111-1111-4111-8111-111111111111\n"
        "DALTON_ENABLED=true\n"
        "DEEPSEEK_API_KEY=synth_deepseek_selected\n"
        "OPENAI_API_KEY=synth_openai_unselected_must_drop\n"
        "ANTHROPIC_API_KEY=synth_anthropic_unselected_must_drop\n"
        "GEMINI_API_KEY=synth_gemini_unselected_for_cash_must_drop\n"
        "XAI_API_KEY=synth_xai_unselected_must_drop\n"
        "FMP_API_KEY=synth_fmp_unrelated_must_drop\n"
        "TAVILY_API_KEY=synth_tavily_unrelated_must_drop\n"
        "FEISHU_APP_ID=cli_dotenv_poison\n"
        "FEISHU_APP_SECRET=sec_dotenv_poison\n"
        "OPENCLAW_GATEWAY_TOKEN=gw_dotenv_poison\n"
        "DALTON_BRIDGE_TOKEN=mcp_dotenv_poison\n"
        "DALTON_BRIDGE_EMBED_URL=http://host.docker.internal:8960/embed\n",
        encoding="utf-8",
    )
    synth_bridge_env = tmp_path / "bridge.env"
    synth_bridge_env.write_text(
        "DALTON_BRIDGE_TOKEN=bridge_mcp_poison\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=bridge_admin_kept_tok\n"
        "FEISHU_APP_ID=cli_bridge_poison\n"
        "FEISHU_APP_SECRET=sec_bridge_poison\n"
        "SEED_ASR_ACCESS_KEY=asr_bridge_poison\n",
        encoding="utf-8",
    )

    check_keys = list(FORBIDDEN_NONARCHIVE_CHECK_KEYS) + [
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "BYOK_ENCRYPTION_KEY",
        "AUTH_USER_ID",
        "DALTON_PM_USER_IDS",
        "DALTON_ENABLED",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "FMP_API_KEY",
        "TAVILY_API_KEY",
        "DALTON_XAI_OAUTH_CLIENT_ID",
        "DALTON_BRIDGE_EMBED_URL",
        "no_proxy",
        "DALTON_OCW_AGENTS_ROOT",
    ]

    # 1. Run `archive_daily.sh` with a probe script that imports `scripts.dalton.conversation_archive`
    #    (triggering `src.config` `load_dotenv()` from `dotenv_dir/.env`) and calls `enforce_archive_process_env_in_place`
    archive_probe = tmp_path / "archive_import_probe.py"
    archive_probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import scripts.dalton.conversation_archive as ca\n"
        "ca.bridge_subprocess_env.enforce_archive_process_env_in_place(\n"
        "    os.environ, explicit_model='deepseek-flash', repo_root=ca.REPO_ROOT, validate_required=True\n"
        ")\n"
        f"keys = {check_keys!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "embed_is_host = (os.environ.get('DALTON_BRIDGE_EMBED_URL') == 'http://127.0.0.1:8960/embed')\n"
        "print(json.dumps({'presence': presence, 'embed_is_host': embed_is_host}))\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "archive_state.json"
    state_file.write_text('{"enabled": true}\n', encoding="utf-8")
    status_file = tmp_path / "archive_status.json"
    pid_file = tmp_path / "archive.pid"

    res_daily = subprocess.run(
        ["/bin/bash", str(ARCHIVE_DAILY_SH)],
        cwd=str(dotenv_dir),
        env={
            **_PoisonEnvDict(),
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DALTON_REPO_PYTHON": sys.executable,
            "DALTON_REPO_ENV_FILE": str(synth_repo_env),
            "DALTON_BRIDGE_ENV_FILE": str(synth_bridge_env),
            "DALTON_ARCHIVE_STATE_FILE": str(state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(archive_probe),
            "DALTON_OCW_AGENTS_ROOT": str(ocw_agents_dir),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res_daily.returncode == 0, res_daily.stdout + "\n" + res_daily.stderr
    json_lines = [
        ln for ln in res_daily.stdout.splitlines() if ln.strip().startswith("{")
    ]
    assert len(json_lines) == 1
    daily_report = json.loads(json_lines[0])
    assert daily_report["embed_is_host"] is True
    pres = daily_report["presence"]

    for req_key in (
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
        "BYOK_ENCRYPTION_KEY",
        "AUTH_USER_ID",
        "DALTON_PM_USER_IDS",
        "DALTON_ENABLED",
        "DEEPSEEK_API_KEY",
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_BRIDGE_EMBED_URL",
        "no_proxy",
        "DALTON_OCW_AGENTS_ROOT",
    ):
        assert pres[req_key] is True, f"archive missing allowed key {req_key}"

    for dropped_key in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_DEFAULT_GRANT_CHAT_ID",
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "OPENCLAW_FEISHU_APP_ID",
        "OPENCLAW_FEISHU_APP_SECRET",
        "LARK_APP_ID",
        "LARK_APP_SECRET",
        "OPENCLAW_GATEWAY_TOKEN",
        "DALTON_OPENCLAW_GATEWAY_TOKEN",
        "DALTON_BROKER_TOKEN",
        "DALTON_WEB_SEARCH_BRIDGE_TOKEN",
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_BRIDGE_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "XAI_API_KEY",
        "FMP_API_KEY",
        "TAVILY_API_KEY",
        "DALTON_XAI_OAUTH_CLIENT_ID",
    ):
        assert pres[dropped_key] is False, f"archive retained unselected/forbidden key {dropped_key}"

    # 2. Selecting `--model grok-4.7-oauth` requires `DALTON_XAI_OAUTH_CLIENT_ID`:
    #    missing -> `archive_daily.sh` fails closed (exit 1) without retrying; present -> allowed.
    res_xai_missing = subprocess.run(
        ["/bin/bash", str(ARCHIVE_DAILY_SH), "--model", "grok-4.7-oauth"],
        cwd=str(dotenv_dir),
        env={
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DALTON_REPO_PYTHON": sys.executable,
            "DALTON_REPO_ENV_FILE": str(synth_repo_env),
            "DALTON_BRIDGE_ENV_FILE": str(synth_bridge_env),
            "DALTON_ARCHIVE_STATE_FILE": str(state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(archive_probe),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_xai_missing.returncode == 1
    assert "DALTON_XAI_OAUTH_CLIENT_ID" in (res_xai_missing.stdout + res_xai_missing.stderr)
    assert "attempt 2" not in res_xai_missing.stdout

    # 3. Missing `BYOK_ENCRYPTION_KEY` or unreadable `DALTON_OCW_AGENTS_ROOT` -> fails closed (exit 1)
    res_bad_agents = subprocess.run(
        ["/bin/bash", str(ARCHIVE_DAILY_SH)],
        cwd=str(dotenv_dir),
        env={
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DALTON_REPO_PYTHON": sys.executable,
            "DALTON_REPO_ENV_FILE": str(synth_repo_env),
            "DALTON_BRIDGE_ENV_FILE": str(synth_bridge_env),
            "DALTON_ARCHIVE_STATE_FILE": str(state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(archive_probe),
            "DALTON_OCW_AGENTS_ROOT": str(tmp_path / "does_not_exist_agents_dir"),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_bad_agents.returncode == 1
    assert "DALTON_OCW_AGENTS_ROOT" in (res_bad_agents.stdout + res_bad_agents.stderr)

    # 4. Verify `reextract_conversations.py` subcommand-scoped allowlists (`plan`, `extract`, `apply`, `render`, `rollback`)
    reextract_probe = tmp_path / "reextract_subcommand_probe.py"
    reextract_probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import scripts.dalton.reextract_conversations as rx\n"
        "results = {}\n"
        "for subcmd in ('plan', 'extract', 'apply', 'render', 'rollback'):\n"
        "    env_copy = {\n"
        "        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),\n"
        "        'HOME': os.environ.get('HOME', '/tmp'),\n"
        "        'LANG': 'en_US.UTF-8',\n"
        "        'TZ': 'America/New_York',\n"
        "        'DB_HOST': '127.0.0.1',\n"
        "        'DB_PORT': '5432',\n"
        "        'DB_NAME': 'dalton_synth',\n"
        "        'DB_USER': 'postgres',\n"
        "        'DB_PASSWORD': 'synth_pw',\n"
        "        'BYOK_ENCRYPTION_KEY': 'synth_byok',\n"
        "        'AUTH_USER_ID': '11111111-1111-4111-8111-111111111111',\n"
        "        'DALTON_PM_USER_IDS': '11111111-1111-4111-8111-111111111111',\n"
        "        'DALTON_ENABLED': 'true',\n"
        "        'DEEPSEEK_API_KEY': 'synth_ds_key',\n"
        "        'OPENAI_API_KEY': 'synth_openai_key',\n"
        "        'DALTON_BRIDGE_ADMIN_TOKEN': 'synth_admin_tok',\n"
        "        'DALTON_BRIDGE_TOKEN': 'synth_mcp_tok',\n"
        "        'FEISHU_APP_SECRET': 'synth_feishu_sec',\n"
        "        'OPENCLAW_GATEWAY_TOKEN': 'synth_gw_tok',\n"
        "        'SEED_ASR_ACCESS_KEY': 'synth_asr_ak',\n"
        "        'DALTON_BRIDGE_EMBED_URL': 'http://host.docker.internal:8960/embed',\n"
        "    }\n"
        "    rx.bridge_subprocess_env.enforce_reextract_process_env_in_place(\n"
        "        env_copy,\n"
        "        subcmd,\n"
        "        explicit_model='deepseek-flash',\n"
        "        explicit_fallback_model='gemini-4-argon-medium-oauth',\n"
        "        repo_root=rx.REPO_ROOT,\n"
        "        validate_required=True,\n"
        "    )\n"
        "    results[subcmd] = {\n"
        "        k: (k in env_copy and bool(env_copy.get(k)))\n"
        "        for k in (\n"
        "            'DB_HOST', 'BYOK_ENCRYPTION_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY',\n"
        "            'DALTON_BRIDGE_ADMIN_TOKEN', 'DALTON_BRIDGE_TOKEN', 'FEISHU_APP_SECRET',\n"
        "            'OPENCLAW_GATEWAY_TOKEN', 'SEED_ASR_ACCESS_KEY', 'DALTON_BRIDGE_EMBED_URL',\n"
        "        )\n"
        "    }\n"
        "print(json.dumps(results))\n",
        encoding="utf-8",
    )
    res_rx = subprocess.run(
        [sys.executable, str(reextract_probe)],
        cwd=str(dotenv_dir),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_rx.returncode == 0, res_rx.stderr
    rx_map = json.loads(res_rx.stdout.strip())

    # `plan`, `apply`, `rollback`: DB only; no BYOK, no LLM keys, no DALTON_BRIDGE_ADMIN_TOKEN
    for subcmd in ("plan", "apply", "rollback"):
        assert rx_map[subcmd]["DB_HOST"] is True
        assert rx_map[subcmd]["BYOK_ENCRYPTION_KEY"] is False
        assert rx_map[subcmd]["DEEPSEEK_API_KEY"] is False
        assert rx_map[subcmd]["OPENAI_API_KEY"] is False
        assert rx_map[subcmd]["DALTON_BRIDGE_ADMIN_TOKEN"] is False
        assert rx_map[subcmd]["FEISHU_APP_SECRET"] is False

    # `extract`: DB + BYOK + selected DEEPSEEK_API_KEY; no OPENAI_API_KEY, no DALTON_BRIDGE_ADMIN_TOKEN
    assert rx_map["extract"]["DB_HOST"] is True
    assert rx_map["extract"]["BYOK_ENCRYPTION_KEY"] is True
    assert rx_map["extract"]["DEEPSEEK_API_KEY"] is True
    assert rx_map["extract"]["OPENAI_API_KEY"] is False
    assert rx_map["extract"]["DALTON_BRIDGE_ADMIN_TOKEN"] is False
    assert rx_map["extract"]["FEISHU_APP_SECRET"] is False

    # `render`: DB + DALTON_BRIDGE_ADMIN_TOKEN + host DALTON_BRIDGE_EMBED_URL; no BYOK, no LLM keys
    assert rx_map["render"]["DB_HOST"] is True
    assert rx_map["render"]["DALTON_BRIDGE_ADMIN_TOKEN"] is True
    assert rx_map["render"]["DALTON_BRIDGE_EMBED_URL"] is True
    assert rx_map["render"]["BYOK_ENCRYPTION_KEY"] is False
    assert rx_map["render"]["DEEPSEEK_API_KEY"] is False
    assert rx_map["render"]["FEISHU_APP_SECRET"] is False


# ---------------------------------------------------------------------------
# 3. Controlled archive execution + `/feishu/*` resource API behavior tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_pipeline_executes_model_resolution_byok_decryption_and_host_embed_under_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the filtered archive allowlist environment:
    - `BYOK_ENCRYPTION_KEY` decrypts stored credentials via `decrypt_value`.
    - `embed_texts_via_bridge` posts to `http://127.0.0.1:8960/embed` with `Bearer <DALTON_BRIDGE_ADMIN_TOKEN>`.
    - `run_archive_pipeline` resolves the extractor model, runs segment extraction, embeds cards via Bridge,
      and persists cards + processed chunks.
    """
    fernet_key = Fernet.generate_key().decode("ascii")
    owner_uid = "11111111-1111-4111-8111-111111111111"

    allowlist_env = bse.build_archive_subprocess_env(
        base_env={
            **_PoisonEnvDict(),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "LANG": "en_US.UTF-8",
            "TZ": "America/New_York",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "5432",
            "DB_NAME": "dalton_test",
            "DB_USER": "postgres",
            "DB_PASSWORD": "synth_password",
            "BYOK_ENCRYPTION_KEY": fernet_key,
            "AUTH_USER_ID": owner_uid,
            "DALTON_PM_USER_IDS": owner_uid,
            "DALTON_ENABLED": "true",
            "DEEPSEEK_API_KEY": "synth_deepseek_key",
            "OPENAI_API_KEY": "synth_unselected_openai",
            "DALTON_BRIDGE_ADMIN_TOKEN": "synth_archive_admin_token_777",
        },
        explicit_model="deepseek-flash",
        validate_required=True,
    )
    for k in list(os.environ.keys()):
        if not k.startswith("PYTEST_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in allowlist_env.items():
        monkeypatch.setenv(k, v)

    assert "FEISHU_APP_SECRET" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["DALTON_BRIDGE_EMBED_URL"] == "http://127.0.0.1:8960/embed"

    # 1. Verify BYOK decryption works under filtered env
    ciphertext = encrypt_value("synth-provider-oauth-refresh-token")
    assert decrypt_value(ciphertext) == "synth-provider-oauth-refresh-token"

    # 2. Wire controlled `httpx.AsyncClient` mock transport for `embed_texts_via_bridge`
    embed_requests: list[tuple[str, str, dict[str, Any]]] = []

    def _embed_handler(request: httpx.Request) -> httpx.Response:
        embed_requests.append(
            (
                str(request.url),
                request.headers.get("Authorization", ""),
                json.loads(request.content.decode("utf-8")),
            )
        )
        body = json.loads(request.content.decode("utf-8"))
        texts = body.get("texts") or []
        return httpx.Response(
            200,
            json={
                "ok": True,
                "model": "gemini-embedding-001",
                "embeddings": [[0.25, 0.75] for _ in texts],
            },
        )

    mock_transport = httpx.MockTransport(_embed_handler)
    real_async_client = httpx.AsyncClient

    def _make_mock_http_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = mock_transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(embedder_mod.httpx, "AsyncClient", _make_mock_http_client)

    # 3. Run `run_archive_pipeline` with autospecced DB and extraction seams
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    inbox_file = inbox_dir / "run_batch1.jsonl"
    inbox_file.write_text(
        json.dumps(
            {
                "agent": "main",
                "session_id": "sess-1",
                "session_key": "main:sess-1",
                "event_id": "ev-101",
                "seq": 1,
                "timestamp": "2026-10-20T14:00:00Z",
                "date_et": "2026-10-20",
                "role": "user",
                "sender_label": "PM",
                "text": "NVDA 2026Q3 数据中心营收预计达到 420 亿美元，Blackwell 机柜出货节奏超预期。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    resolve_model_spy = create_autospec(
        archive_mod.resolve_dalton_extract_model,
        return_value="deepseek-flash",
    )
    monkeypatch.setattr(archive_mod, "resolve_dalton_extract_model", resolve_model_spy)

    async def _fake_extract(
        events: list[dict[str, Any]],
        *,
        model: str | None = None,
        prompt_version: str | None = None,
        user_id: str | None = None,
        llm_service: Any = None,
        alias_table: Any = None,
        resend_indeterminate: bool = False,
        persist_raw: bool = True,
        skip_journal: bool = False,
    ) -> SegmentExtractionOutcome:
        _ = (prompt_version, llm_service, alias_table, resend_indeterminate, persist_raw, skip_journal)
        assert model == "deepseek-flash"
        assert user_id == owner_uid
        assert decrypt_value(ciphertext) == "synth-provider-oauth-refresh-token"
        return SegmentExtractionOutcome(
            ok=True,
            model_used="deepseek-flash",
            status="completed",
            input_tokens=120,
            output_tokens=45,
            total_tokens=165,
            cards=[
                {
                    "card_id": "card-nvda-1",
                    "content_hash": "hash-nvda-1",
                    "source_type": "openclaw",
                    "agent": "main",
                    "session_id": "sess-1",
                    "session_key": "main:sess-1",
                    "date_et": "2026-10-20",
                    "card_type": "thesis",
                    "claim": "NVDA 2026Q3 数据中心营收预计达 420 亿美元。",
                    "verbatim_quote": "NVDA 2026Q3 数据中心营收预计达到 420 亿美元",
                    "search_text": "NVDA 2026Q3 数据中心营收预计达 420 亿美元",
                    "tickers": ["NVDA"],
                    "entities": [],
                    "numbers": [{"value": 420, "unit": "亿美元", "period": "2026Q3"}],
                    "confidence": 0.92,
                    "event_ids": ["ev-101"],
                    "seq_range": [1, 1],
                    "status": "active",
                }
            ],
        )

    extract_spy = create_autospec(archive_mod.extract_segment_cards, side_effect=_fake_extract)

    fake_lock_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_data_conn = create_autospec(psycopg.AsyncConnection, instance=True)

    @asynccontextmanager
    async def _fake_tx_scope(conn: Any = None):
        yield conn or fake_data_conn

    monkeypatch.setattr(
        archive_mod,
        "open_session_connection",
        create_autospec(archive_mod.open_session_connection, return_value=fake_lock_conn),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "try_acquire_archive_run_lock",
        create_autospec(archive_mod.db_conv.try_acquire_archive_run_lock, return_value=True),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "release_archive_run_lock",
        create_autospec(archive_mod.db_conv.release_archive_run_lock, return_value=True),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "mark_stale_running_runs_abandoned",
        create_autospec(archive_mod.db_conv.mark_stale_running_runs_abandoned, return_value=0),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "fetch_processed_event_chunks",
        create_autospec(archive_mod.db_conv.fetch_processed_event_chunks, return_value=set()),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "insert_run_start",
        create_autospec(archive_mod.db_conv.insert_run_start, return_value=None),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "find_existing_content_hashes",
        create_autospec(archive_mod.db_conv.find_existing_content_hashes, return_value=set()),
    )
    monkeypatch.setattr(archive_mod.db_conv, "transaction_scope", _fake_tx_scope)
    insert_cards_spy = create_autospec(
        archive_mod.db_conv.insert_cards_batch,
        return_value=(1, 0, [{"card_id": "card-nvda-1", "date_et": "2026-10-20"}]),
    )
    monkeypatch.setattr(archive_mod.db_conv, "insert_cards_batch", insert_cards_spy)
    monkeypatch.setattr(
        archive_mod.db_conv,
        "record_processed_event_chunks",
        create_autospec(archive_mod.db_conv.record_processed_event_chunks, return_value=1),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "update_run_progress",
        create_autospec(archive_mod.db_conv.update_run_progress, return_value=None),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "update_run_finish",
        create_autospec(archive_mod.db_conv.update_run_finish, return_value=None),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "list_unrendered_dates",
        create_autospec(archive_mod.db_conv.list_unrendered_dates, return_value=[]),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "list_cards_for_dates",
        create_autospec(archive_mod.db_conv.list_cards_for_dates, return_value=[]),
    )
    monkeypatch.setattr(
        archive_mod,
        "render_and_write_conversation_digests",
        create_autospec(
            archive_mod.render_and_write_conversation_digests,
            return_value={"rendered_dates": ["2026-10-20"], "card_locations": {}},
        ),
    )
    monkeypatch.setattr(
        archive_mod.db_conv,
        "mark_dates_rendered",
        create_autospec(archive_mod.db_conv.mark_dates_rendered, return_value=1),
    )

    summary = await archive_mod.run_archive_pipeline(
        mode="run",
        since_date=date(2026, 10, 15),
        until_date=date(2026, 10, 21),
        dry_run=False,
        inbox_file=inbox_file,
        inbox_dir=inbox_dir,
        cursors_file=tmp_path / "cursors.json",
        skip_host_export=True,
        extract_fn=extract_spy,
        embed_fn=embedder_mod.embed_texts_via_bridge,
        conn=fake_data_conn,
    )

    assert summary["ok"] is True
    assert summary["model"] == "deepseek-flash"
    resolve_model_spy.assert_awaited_once_with(owner_uid)
    extract_spy.assert_awaited_once()
    insert_cards_spy.assert_awaited_once()
    assert len(embed_requests) == 1
    req_url, req_auth, req_body = embed_requests[0]
    assert req_url == "http://127.0.0.1:8960/embed"
    assert req_auth == "Bearer synth_archive_admin_token_777"
    assert req_body["texts"] == ["NVDA 2026Q3 数据中心营收预计达 420 亿美元"]


class _FakeUrllibResponse(io.BytesIO):
    def __init__(
        self, body: bytes, status: int = 200, content_type: str = "application/json"
    ) -> None:
        super().__init__(body)
        self.status = status
        headers = email.message.Message()
        headers["Content-Type"] = content_type
        self.headers = headers


@pytest.mark.asyncio
async def test_feishu_resource_apis_with_dedicated_credentials_and_mock_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify existing `/feishu/*` resource APIs with dedicated Bridge credentials and mocked
    Feishu OpenAPI HTTP responses:
    - Valid `tenant_access_token` acquisition
    - Auth error (`99991663`), permission denied (`HTTP 403`), and non-zero business `code`
    - Doc read, doc create + markdown inline image upload + personal/group permission grant, doc append
    - Wiki spaces, wiki nodes, wiki node read, wiki doc create
    - Bot info and chats list
    """
    env_file = tmp_path / "bridge.env"
    env_file.write_text(
        "DALTON_BRIDGE_TOKEN=mcp_synth_tok\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=admin_synth_tok\n"
        "FEISHU_APP_ID=cli_dedicated_test_app\n"
        "FEISHU_APP_SECRET=sec_dedicated_test_secret\n"
        "FEISHU_DEFAULT_GRANT_CHAT_ID=oc_dedicated_team_group\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("FEISHU_DEFAULT_GRANT_CHAT_ID", raising=False)

    cfg = rb.BridgeConfig()
    assert cfg.feishu_app_id == "cli_dedicated_test_app"
    assert cfg.feishu_app_secret == "sec_dedicated_test_secret"
    assert cfg.feishu_default_grant_chat_id == "oc_dedicated_team_group"
    bridge = rb.ResearchDataBridge(cfg)

    recorded_calls: list[dict[str, Any]] = []
    mode_state = {"mode": "ok"}

    def _http_handler(req: Any, timeout: Any = None) -> _FakeUrllibResponse:
        _ = timeout
        url = req.full_url
        method = req.get_method()
        recorded_calls.append({"method": method, "url": url})

        if "/open-apis/auth/v3/tenant_access_token/internal" in url:
            if mode_state["mode"] == "auth_error":
                payload: dict[str, Any] = {"code": 99991663, "msg": "app secret invalid"}
                return _FakeUrllibResponse(json.dumps(payload).encode("utf-8"), 200)
            return _FakeUrllibResponse(
                json.dumps({"code": 0, "tenant_access_token": "t-dedicated-tok", "expire": 7200}).encode("utf-8"),
                200,
            )

        if mode_state["mode"] == "perm_403":
            err_body = json.dumps({"code": 1770032, "msg": "forbidden", "log_id": "log_403"}).encode("utf-8")
            hdrs = email.message.Message()
            hdrs["Content-Type"] = "application/json"
            raise rb.urllib.error.HTTPError(url, 403, "Forbidden", hdrs, io.BytesIO(err_body))

        if mode_state["mode"] == "biz_nonzero":
            return _FakeUrllibResponse(
                json.dumps({"code": 1770002, "msg": "not found", "log_id": "log_biz"}).encode("utf-8"),
                200,
            )

        if "/open-apis/bot/v3/info" in url:
            return _FakeUrllibResponse(
                json.dumps({"code": 0, "bot": {"app_name": "Dalton Research Bot", "open_id": "ou_bot_1"}}).encode("utf-8"),
                200,
            )
        if "/open-apis/im/v1/chats" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "items": [{"chat_id": "oc_dedicated_team_group", "name": "Eve tracking"}],
                            "has_more": False,
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/wiki/v2/spaces/get_node" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "node": {
                                "space_id": "sp_res",
                                "node_token": "wikinode_1",
                                "obj_token": "doccn_1",
                                "obj_type": "docx",
                                "title": "Wiki Doc Title",
                            }
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/wiki/v2/spaces/" in url and "/nodes" in url and method == "POST":
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "node": {
                                "space_id": "sp_res",
                                "node_token": "wikinode_created",
                                "obj_token": "doccn_wiki_created",
                                "obj_type": "docx",
                            }
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/wiki/v2/spaces/" in url and "/nodes" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "space_id": "sp_res",
                                    "node_token": "wikinode_1",
                                    "obj_token": "doccn_1",
                                    "obj_type": "docx",
                                    "title": "Node 1",
                                    "parent_node_token": "",
                                    "has_child": False,
                                }
                            ],
                            "has_more": False,
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/wiki/v2/spaces" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "items": [{"space_id": "sp_res", "name": "Research Wiki", "description": ""}],
                            "has_more": False,
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/raw_content" in url:
            return _FakeUrllibResponse(
                json.dumps({"code": 0, "data": {"content": "Document body content"}}).encode("utf-8"),
                200,
            )
        if "/blocks/convert" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "first_level_block_ids": ["tmp_img_1"],
                            "blocks": [{"block_id": "tmp_img_1", "block_type": 27, "image": {}}],
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/descendant" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "block_id_relations": [
                                {"temporary_block_id": "tmp_img_1", "block_id": "real_img_blk_1"}
                            ],
                            "children": [{"block_id": "real_img_blk_1", "block_type": 27}],
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/drive/v1/medias/upload_all" in url:
            return _FakeUrllibResponse(
                json.dumps({"code": 0, "data": {"file_token": "img_tok_uploaded_1"}}).encode("utf-8"),
                200,
            )
        if "/blocks/real_img_blk_1" in url and method == "PATCH":
            return _FakeUrllibResponse(
                json.dumps({"code": 0, "data": {"block": {"block_id": "real_img_blk_1"}}}).encode("utf-8"),
                200,
            )
        if "/blocks/real_img_blk_1" in url and method == "GET":
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "block": {
                                "block_id": "real_img_blk_1",
                                "block_type": 27,
                                "image": {"token": "img_tok_uploaded_1"},
                            }
                        },
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/contact/v3/users/batch_get_id" in url:
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {"user_list": [{"email": "analyst@example.com", "user_id": "ou_analyst_1"}]},
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/drive/v1/permissions/" in url and "/members" in url:
            return _FakeUrllibResponse(
                json.dumps({"code": 0, "data": {"member": {"perm": "full_access"}}}).encode("utf-8"),
                200,
            )
        if "/open-apis/docx/v1/documents" in url and method == "POST":
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {"document": {"document_id": "doccn_created_1", "title": "Created Doc"}},
                    }
                ).encode("utf-8"),
                200,
            )
        if "/open-apis/docx/v1/documents/" in url and method == "GET":
            return _FakeUrllibResponse(
                json.dumps(
                    {
                        "code": 0,
                        "data": {"document": {"document_id": "doccn_1", "title": "Existing Doc"}},
                    }
                ).encode("utf-8"),
                200,
            )
        return _FakeUrllibResponse(json.dumps({"code": 0, "data": {}}).encode("utf-8"), 200)

    opener_cls = create_autospec(rb.urllib.request.build_opener)
    opener_cls.return_value.open.side_effect = _http_handler
    monkeypatch.setattr(rb.urllib.request, "build_opener", opener_cls)

    # 1. Bot info & chats list
    code_bot, body_bot = await bridge.handle_feishu_bot_info()
    assert code_bot == 200 and body_bot["ok"] is True
    assert body_bot["app_name"] == "Dalton Research Bot"

    code_chats, body_chats = await bridge.handle_feishu_chats_list()
    assert code_chats == 200 and body_chats["ok"] is True
    assert body_chats["chats"][0]["chat_id"] == "oc_dedicated_team_group"

    # 2. Doc read & Wiki spaces/nodes/node read
    code_read, body_read = await bridge.handle_feishu_doc_read(
        {"token": "https://example.feishu.cn/docx/doccn_1"}
    )
    assert code_read == 200 and body_read["ok"] is True
    assert body_read["content"] == "Document body content"

    code_sp, body_sp = await bridge.handle_feishu_wiki_spaces()
    assert code_sp == 200 and body_sp["spaces"][0]["space_id"] == "sp_res"

    code_nd, body_nd = await bridge.handle_feishu_wiki_nodes({"space_id": "sp_res"})
    assert code_nd == 200 and body_nd["nodes"][0]["node_token"] == "wikinode_1"

    code_wn, body_wn = await bridge.handle_feishu_wiki_node({"token": "wikinode_1"})
    assert code_wn == 200 and body_wn["obj_token"] == "doccn_1"

    # 3. Doc create with inline image upload + personal & group permission grant + Wiki create + Doc append
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nsynth").decode("ascii")
    code_create, body_create = await bridge.handle_feishu_doc_create(
        {
            "title": "Created Doc",
            "markdown": "![chart](chart.png)\n",
            "caller_email": "analyst@example.com",
            "image_files": {"chart.png": {"filename": "chart.png", "content_b64": png_b64}},
        }
    )
    assert code_create == 200 and body_create["ok"] is True
    assert body_create["document_id"] == "doccn_created_1"
    assert body_create["granted_to"] == "open_id:ou_analyst_1"
    assert body_create["images"]["bound"] == 1

    code_wcreate, body_wcreate = await bridge.handle_feishu_doc_create(
        {
            "title": "Created Wiki Doc",
            "markdown": "# Section\nBody",
            "space_id": "sp_res",
        }
    )
    assert code_wcreate == 200 and body_wcreate["ok"] is True
    assert body_wcreate["node_token"] == "wikinode_created"

    code_app, body_app = await bridge.handle_feishu_doc_append(
        {"token": "doccn_1", "markdown": "![chart](chart.png)", "image_files": {"chart.png": {"filename": "chart.png", "content_b64": png_b64}}}
    )
    assert code_app == 200 and body_app["ok"] is True

    # 4. Permission error (HTTP 403), business code != 0, and auth error
    mode_state["mode"] = "perm_403"
    code_403, body_403 = await bridge.handle_feishu_doc_read({"token": "doccn_1"})
    assert code_403 == 502 and body_403["ok"] is False
    assert body_403["error_code"] == "feishu_http_403"
    assert body_403["feishu_code"] == "1770032"

    mode_state["mode"] = "biz_nonzero"
    code_biz, body_biz = await bridge.handle_feishu_doc_read({"token": "doccn_1"})
    assert code_biz == 502 and body_biz["ok"] is False
    assert body_biz["error_code"] == "feishu_api_error"
    assert body_biz["feishu_code"] == "1770002"

    bridge._feishu_client = None
    mode_state["mode"] = "auth_error"
    code_auth, body_auth = await bridge.handle_feishu_doc_read({"token": "doccn_1"})
    assert code_auth == 502 and body_auth["ok"] is False
    assert body_auth["error_code"] in ("feishu_api_error", "feishu_auth_failed")
    assert "sec_dedicated_test_secret" not in json.dumps(body_auth)
    assert "openclaw.json" not in describe_feishu_error("feishu_not_configured")
