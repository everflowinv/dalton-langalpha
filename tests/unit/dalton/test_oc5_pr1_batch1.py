"""OC5 PR1 Batch 1 behavior tests.

Covers:
1. Dedicated Bridge Feishu configuration (`DALTON_BRIDGE_ENV_FILE` / `FEISHU_*`) without
   `~/.openclaw/openclaw.json` or `DALTON_FEISHU_*` fallback, and `feishu_not_configured` error message.
2. Repeatable secure installer (`install_bridge.sh` + `bridge_subprocess_env.py`) preserving existing
   values/comments, atomic `0600` writes, empty-value handling, duplicate-key / malformed-line /
   partial-Feishu / invalid-value / symlink-target / dangling-link / symlink-parent rejection,
   deterministic directory permission failure handling, and first-install MCP authentication
   through `plugins/dalton_data/mcp.json` (`bundled_mcp_servers`).
3. Real subprocess final environment inspection (boolean key-presence maps only) across all 8
   non-archive Bridge subprocess entrypoints, the Batch 1 transitional card-send subprocess, both
   skill `run.sh` launchers (`company-wiki` and `market-digest`, including workspaces containing
   legacy `run.sh`), `archive_daily.sh`, `conversation_archive.py` (including import-time safety,
   CLI/plan/preference model-conditional provider keys from synthetic `.env`, and late
   `load_from_files()` / `load_dotenv_async()` re-filtering), `src/dalton/conversations/render.py`,
   and all 5 `reextract_conversations.py` subcommands (`plan`, `extract`, `apply`, `render`,
   `rollback`) including dotenv-only / pre-exported / `--dsn` `DALTON_REEXTRACT_DSN` preservation.
4. Controlled end-to-end archive extraction, `BYOK_ENCRYPTION_KEY` configuration + autospecced
   credential store lookup (plus `@pytest.mark.integration` real PostgreSQL `pgcrypto`
   `user_api_keys` and `user_oauth_tokens` round-trip), and host `/embed` HTTP execution under the
   filtered archive allowlist environment, plus `/feishu/*` OpenAPI proxy coverage.
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
from urllib.parse import urlparse

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import bridge_subprocess_env as bse  # noqa: E402
import researchdata_bridge as rb  # noqa: E402
import scripts.dalton.conversation_archive as archive_mod  # noqa: E402
import scripts.dalton.reextract_conversations as reextract_mod  # noqa: E402
from src.dalton.conversations import embedder as embedder_mod  # noqa: E402
from src.dalton.conversations import render as render_mod  # noqa: E402
from src.dalton.conversations.extractor import SegmentExtractionOutcome  # noqa: E402
from src.dalton.feishu import describe_feishu_error  # noqa: E402
from src.ptc_agent.config.plugins import bundled_mcp_servers  # noqa: E402
from src.server.database import api_keys as db_api_keys  # noqa: E402
from src.server.database import oauth_tokens as db_oauth_tokens  # noqa: E402
from src.server.database.encryption import (  # noqa: E402
    encryption_configured,
    get_encryption_key,
)

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
    "BASH_ENV",
    "ENV",
    "ZDOTDIR",
    "PROMPT_COMMAND",
    "PYTHONSTARTUP",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "XAI_API_KEY",
    "ZAI_API_KEY",
    "MOONSHOT_API_KEY",
    "FMP_API_KEY",
    "TAVILY_API_KEY",
    "BYOK_ENCRYPTION_KEY",
    "DB_PASSWORD",
    "DALTON_REEXTRACT_DSN",
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
        "ENV": "/tmp/synth_sh_env_rc",
        "ZDOTDIR": "/tmp/synth_zdotdir",
        "PROMPT_COMMAND": "export FEISHU_APP_SECRET=prompt_reinjected",
        "PYTHONSTARTUP": "/tmp/synth_python_startup.py",
        "OPENAI_API_KEY": "sk_synth_openai_unrelated",
        "ANTHROPIC_API_KEY": "sk_synth_anthropic_unrelated",
        "GEMINI_API_KEY": "sk_synth_gemini_unrelated",
        "DEEPSEEK_API_KEY": "sk_synth_deepseek_unrelated",
        "XAI_API_KEY": "sk_synth_xai_unrelated",
        "ZAI_API_KEY": "sk_synth_zai_unrelated",
        "MOONSHOT_API_KEY": "sk_synth_moonshot_unrelated",
        "FMP_API_KEY": "fmp_synth_unrelated",
        "TAVILY_API_KEY": "tvly_synth_unrelated",
        "BYOK_ENCRYPTION_KEY": "byok_synth_unrelated",
        "DB_PASSWORD": "db_pass_synth_unrelated",
        "DALTON_REEXTRACT_DSN": "postgresql://postgres:synth@127.0.0.1:5432/scratch",
    }


def _write_shell_rc_poison(path: Path) -> Path:
    """Write a synthetic shell rc file that re-exports forbidden credentials."""
    lines = ["#!/usr/bin/env bash"]
    for k, v in _PoisonEnvDict().items():
        if k not in ("ENV", "ZDOTDIR", "PROMPT_COMMAND", "PYTHONSTARTUP"):
            lines.append(f"export {k}='{v}_from_shell_rc'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_key_presence_probe_py(
    path: Path, keys: tuple[str, ...] = FORBIDDEN_NONARCHIVE_CHECK_KEYS + ("GEMINI_API_KEY",)
) -> Path:
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


async def _send_raw_bridge_http(
    bridge: rb.ResearchDataBridge,
    method: str,
    path: str,
    headers: dict[str, str],
    body_obj: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Invoke `ResearchDataBridge._handle_client` over an in-memory StreamReader/StreamWriter."""
    body_bytes = (
        json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        if body_obj is not None
        else b""
    )
    req_lines = [f"{method} {path} HTTP/1.1"]
    for k, v in headers.items():
        req_lines.append(f"{k}: {v}")
    if body_bytes:
        req_lines.append(f"Content-Length: {len(body_bytes)}")
    raw_req = ("\r\n".join(req_lines) + "\r\n\r\n").encode("utf-8") + body_bytes

    reader = asyncio.StreamReader()
    reader.feed_data(raw_req)
    reader.feed_eof()

    written = bytearray()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    writer.write.side_effect = lambda data: written.extend(data)
    writer.drain.return_value = None
    writer.wait_closed.return_value = None
    await rb._handle_client(bridge, reader, writer)
    raw_resp = bytes(written).decode("utf-8", errors="replace")
    head, _, body_text = raw_resp.partition("\r\n\r\n")
    status_code = int(head.splitlines()[0].split()[1])
    return status_code, json.loads(body_text) if body_text.strip() else {}


# ---------------------------------------------------------------------------
# 1. Installer (`install_bridge.sh`, `bridge_subprocess_env.py`, & `plugins/dalton_data/mcp.json`)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_bridge_first_run_mcp_plugin_auth_repeat_merge_and_symlink_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise `install_bridge.sh` + `plugins/dalton_data/mcp.json` in temporary directories:
    - First install creates 0600 env files, 0700 parent dir, and 0644 plist without leaking secrets.
    - `bundled_mcp_servers()` loads `plugins/dalton_data/mcp.json` with first-install
      `DALTON_RESEARCHDATA_URL` and `DALTON_BRIDGE_TOKEN`, producing `Authorization: Bearer <token>`
      that authenticates against `ResearchDataBridge` HTTP `/mcp`.
    - Repeat install preserves comments, existing tokens, existing path-token URLs, and `FEISHU_*`
      credentials while filling only missing or empty default keys.
    - Rejects partial Feishu config, duplicate keys, malformed lines, invalid required values,
      live symlink targets, dangling symlink targets, and symlink parent directories.
    - Stops before config publication when required directory permission setting (`0700`) fails.
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

    combined_out_1 = res1.stdout + res1.stderr + stub_log.read_text(encoding="utf-8")
    assert tok_1 not in combined_out_1
    assert admin_tok_1 not in combined_out_1

    # 2. First-install MCP plugin config (`plugins/dalton_data/mcp.json`) authentication check
    with monkeypatch.context() as mp:
        mp.setenv("DALTON_RESEARCHDATA_URL", repo_vals_1["DALTON_RESEARCHDATA_URL"])
        mp.setenv("DALTON_BRIDGE_TOKEN", repo_vals_1["DALTON_BRIDGE_TOKEN"])
        mp.setenv("SEC_USER_AGENT", repo_vals_1["SEC_USER_AGENT"])
        servers = {s.name: s for s in bundled_mcp_servers()}
        assert "researchdata" in servers
        rd_cfg = servers["researchdata"]
        assert rd_cfg.url == "http://host.docker.internal:8960/mcp"
        assert rd_cfg.headers == {"Authorization": f"Bearer {tok_1}"}

        mp.setenv("DALTON_BRIDGE_ENV_FILE", str(bridge_env))
        bridge_cfg = rb.BridgeConfig()
        bridge_inst = rb.ResearchDataBridge(bridge_cfg)
        mcp_script = ocw_root / "scripts" / "researchdata_mcp.py"
        mcp_script.parent.mkdir(parents=True, exist_ok=True)
        mcp_script.write_text(
            "import json, sys\n"
            "for ln in sys.stdin:\n"
            "    if ln.strip():\n"
            "        req = json.loads(ln)\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id', 1), 'result': {'tools': []}}), flush=True)\n",
            encoding="utf-8",
        )
        bridge_inst.cfg.mcp_script = mcp_script
        bridge_inst.cfg.python_bin = sys.executable

        # Authenticated request using parsed plugin headers succeeds
        code_ok, body_ok = await _send_raw_bridge_http(
            bridge_inst,
            "POST",
            "/mcp",
            headers={**rd_cfg.headers, "Content-Type": "application/json"},
            body_obj={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert code_ok == 200
        assert body_ok["result"] == {"tools": []}

        # Unauthenticated request without plugin header is rejected (401)
        code_unauth, body_unauth = await _send_raw_bridge_http(
            bridge_inst,
            "POST",
            "/mcp",
            headers={"Content-Type": "application/json"},
            body_obj={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert code_unauth == 401
        assert tok_1 not in json.dumps(body_unauth)

    # 3. Repeat install preserves comments, existing tokens, existing path-token URLs, and FEISHU_*
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

    existing_path_token_url = "http://host.docker.internal:8960/mcp/legacy_path_token_keep"
    repo_env.write_text(
        "# Repo env comment\n"
        "DALTON_BRIDGE_TOKEN=repo_existing_mcp_tok\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=\n"
        f"DALTON_RESEARCHDATA_URL={existing_path_token_url}\n",
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
    assert bridge_vals_2["DALTON_BRIDGE_MAX_INFLIGHT"] == "2"
    assert bridge_vals_2["DALTON_BRIDGE_CALLER_BUDGET_S"] == "625"

    _, repo_vals_2 = bse.parse_env_file_strict(repo_env)
    assert repo_vals_2["DALTON_BRIDGE_TOKEN"] == "repo_existing_mcp_tok"
    assert repo_vals_2["DALTON_BRIDGE_ADMIN_TOKEN"] == "custom_preserved_admin_token_xyz"
    assert repo_vals_2["DALTON_RESEARCHDATA_URL"] == existing_path_token_url
    assert repo_vals_2["SEC_USER_AGENT"] == "DaltonResearch research@dalton.local"

    combined_out_2 = res2.stdout + res2.stderr + stub_log.read_text(encoding="utf-8")
    for secret_str in (
        "custom_preserved_mcp_token_abc",
        "custom_preserved_admin_token_xyz",
        "sec_dedicated_keep_me",
        "repo_existing_mcp_tok",
        "legacy_path_token_keep",
    ):
        assert secret_str not in combined_out_2

    # 4. Partial Feishu credentials -> fails closed
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

    # 5. Duplicate key & malformed line & invalid port/python -> fail closed
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

    malformed_secret = "MALFORMED_LINE_SECRET_VALUE_999"
    bridge_env.write_text(merged_bridge_raw + f"{malformed_secret}\n", encoding="utf-8")
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

    # Restore valid bridge_env before symlink & permission tests
    bridge_env.write_text(merged_bridge_raw, encoding="utf-8")
    os.chmod(bridge_env, 0o600)

    # 6. Symlink target, dangling symlink target, and symlink parent rejection
    real_target = tmp_path / "real_target.env"
    real_target.write_text("SECRET_TARGET=do_not_touch\n", encoding="utf-8")
    symlink_env = tmp_path / "symlink_bridge.env"
    symlink_env.symlink_to(real_target)
    with pytest.raises(bse.EnvFileValidationError, match="symlink"):
        bse.merge_bridge_and_repo_env_files(
            bridge_env_path=symlink_env,
            repo_env_path=repo_env,
            default_port="8960",
            default_ocw_root=str(ocw_root),
            default_python_bin=sys.executable,
        )
    assert symlink_env.is_symlink()
    assert real_target.read_text(encoding="utf-8") == "SECRET_TARGET=do_not_touch\n"

    dangling_env = tmp_path / "dangling_bridge.env"
    dangling_env.symlink_to(tmp_path / "nonexistent_target.env")
    res_dangling = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env={**base_env, "DALTON_BRIDGE_ENV_FILE": str(dangling_env)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_dangling.returncode != 0
    assert "symlink" in res_dangling.stderr
    assert dangling_env.is_symlink()

    real_parent_dir = tmp_path / "real_parent_dir"
    real_parent_dir.mkdir(parents=True, exist_ok=True)
    symlink_parent_dir = tmp_path / "symlink_parent_dir"
    symlink_parent_dir.symlink_to(real_parent_dir)
    res_sym_parent = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env={
            **base_env,
            "DALTON_BRIDGE_ENV_FILE": str(symlink_parent_dir / "researchdata-bridge.env"),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_sym_parent.returncode != 0
    assert "symlink" in res_sym_parent.stderr
    assert not (real_parent_dir / "researchdata-bridge.env").exists()

    # 7. Deterministic directory permission failure stops before config publication
    perm_fail_dir = tmp_path / "perm_fail_config"
    perm_fail_bridge_env = perm_fail_dir / "researchdata-bridge.env"
    perm_fail_repo_env = tmp_path / "perm_fail_repo.env"
    real_chmod = os.chmod

    def _fail_dir_chmod(target: Any, mode: int, *args: Any, **kwargs: Any) -> None:
        if Path(target) == perm_fail_dir:
            raise PermissionError(13, "Operation not permitted on config directory", str(target))
        real_chmod(target, mode, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(bse.os, "chmod", _fail_dir_chmod)
        with pytest.raises(PermissionError):
            bse.merge_bridge_and_repo_env_files(
                bridge_env_path=perm_fail_bridge_env,
                repo_env_path=perm_fail_repo_env,
                default_port="8960",
                default_ocw_root=str(ocw_root),
                default_python_bin=sys.executable,
            )
    assert not perm_fail_bridge_env.exists()
    assert not perm_fail_repo_env.exists()

    # 8. Managed config_dir at 0500 is normalized back to 0700; real write failure preserves original 0600 files
    os.chmod(config_dir, 0o500)
    res_norm = subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_norm.returncode == 0, res_norm.stderr
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert bridge_env.read_text(encoding="utf-8") == merged_bridge_raw
    assert stat.S_IMODE(bridge_env.stat().st_mode) == 0o600

    ro_repo_dir = tmp_path / "ro_repo_dir"
    ro_repo_dir.mkdir(parents=True, exist_ok=True)
    ro_repo_env = ro_repo_dir / ".env"
    merged_repo_raw = repo_env.read_text(encoding="utf-8")
    ro_repo_env.write_text(merged_repo_raw, encoding="utf-8")
    os.chmod(ro_repo_env, 0o600)
    os.chmod(ro_repo_dir, 0o500)
    try:
        res_ro = subprocess.run(
            ["/bin/bash", str(INSTALL_SH)],
            env={**base_env, "DALTON_REPO_ENV_FILE": str(ro_repo_env)},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert res_ro.returncode != 0
        assert "sec_dedicated_keep_me" not in (res_ro.stdout + res_ro.stderr)
    finally:
        os.chmod(ro_repo_dir, 0o700)
    assert ro_repo_env.read_text(encoding="utf-8") == merged_repo_raw
    assert stat.S_IMODE(ro_repo_env.stat().st_mode) == 0o600
    assert list(ro_repo_dir.glob(".*tmp*")) == []
    assert bridge_env.read_text(encoding="utf-8") == merged_bridge_raw
    assert stat.S_IMODE(bridge_env.stat().st_mode) == 0o600
    assert list(config_dir.glob(".*tmp*")) == []


# ---------------------------------------------------------------------------
# 2. Subprocess final environment verification across all entrypoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_eight_nonarchive_bridge_entries_and_skill_launchers_strip_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spawn controlled subprocesses through all 8 non-archive Bridge entrypoints, the
    transitional card-send subprocess, and both skill `run.sh` launchers (with old workspace
    `run.sh` files present on disk) while poisoning parent `os.environ` and shell rc
    (`DALTON_SKILL_SHELL_RC` / `BASH_ENV`).
    Verify:
    - All forbidden Feishu, gateway, ASR, broker, bridge-token, shell-injection (`BASH_ENV`, etc.),
      and unrelated LLM/vendor/DB/encryption secrets (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
      `DEEPSEEK_API_KEY`, `BYOK_ENCRYPTION_KEY`, etc.) are stripped.
    - `GEMINI_API_KEY` is stripped from non-wiki subprocesses (`_run_stdio_rpc`, `xueqiu`,
      `employee`, `probe_xreach_version`, `bash bbg_remote_refresh.sh`, `launchctl`, `feishu_send_card.py`)
      and provided only to `/embed` and the `company-wiki` / `market-digest` skill launchers.
    - Old workspace `skills/*/run.sh` files are preserved untouched on disk while Bridge resolves
      and executes the maintained repo overlay `run.sh` via the production argv.
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

    # Create legacy unmaintained workspace `run.sh` files that would fail (exit 99) if executed,
    # and verify Bridge preserves their content while executing the maintained repo overlay launchers.
    legacy_wiki_run_sh = ocw_root / "skills" / "company-wiki" / "run.sh"
    legacy_wiki_content = "#!/usr/bin/env bash\n# LEGACY WORKSPACE WIKI RUN_SH\nexit 99\n"
    legacy_wiki_run_sh.write_text(legacy_wiki_content, encoding="utf-8")
    legacy_wiki_run_sh.chmod(0o755)

    legacy_digest_run_sh = ocw_root / "skills" / "market-digest" / "run.sh"
    legacy_digest_content = "#!/usr/bin/env bash\n# LEGACY WORKSPACE DIGEST RUN_SH\nexit 99\n"
    legacy_digest_run_sh.write_text(legacy_digest_content, encoding="utf-8")
    legacy_digest_run_sh.chmod(0o755)

    probe_keys = FORBIDDEN_NONARCHIVE_CHECK_KEYS + ("GEMINI_API_KEY",)
    probe_py = _write_key_presence_probe_py(tmp_path / "probe_env.py", keys=probe_keys)

    for venv_py in (
        ocw_root / "skills" / "employee-reviews" / "venv" / "bin" / "python",
        ocw_root / "skills" / "company-wiki" / "venv" / "bin" / "python",
        ocw_root / "skills" / "market-digest" / "venv" / "bin" / "python",
    ):
        venv_py.symlink_to(sys.executable)

    # 1. MCP stdio child (`_run_stdio_rpc`)
    mcp_script = ocw_root / "scripts" / "researchdata_mcp.py"
    mcp_script.write_text(
        "import json, os, sys\n"
        f"keys = {list(probe_keys)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "for line in sys.stdin:\n"
        "    if line.strip():\n"
        "        req = json.loads(line)\n"
        "        print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id', 1), 'result': {'presence': presence}}), flush=True)\n",
        encoding="utf-8",
    )

    # 2. Xueqiu / Employee CLI (`_build_community_subprocess_env`)
    xueqiu_script = ocw_root / "scripts" / "xueqiu_posts.py"
    xueqiu_script.write_text(probe_py.read_text(encoding="utf-8"), encoding="utf-8")
    emp_script = ocw_root / "skills" / "employee-reviews" / "scripts" / "employee_reviews_cli.py"
    emp_script.write_text(probe_py.read_text(encoding="utf-8"), encoding="utf-8")

    # 3. xreach binary (`probe_xreach_version`)
    xreach_out_file = tmp_path / "xreach_presence.json"
    xreach_bin = tmp_path / "xreach"
    xreach_bin.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "from pathlib import Path\n"
        f"keys = {list(probe_keys)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        f"Path({str(xreach_out_file)!r}).write_text(json.dumps({{'presence': presence}}), encoding='utf-8')\n"
        "print('xreach 0.4.2-synth')\n",
        encoding="utf-8",
    )
    xreach_bin.chmod(0o755)

    # 4. company-wiki CLI (used by `/embed` and `company-wiki/run.sh`)
    wiki_cli = ocw_root / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py"
    wiki_cli.write_text(
        "import json, os, sys\n"
        f"keys = {list(probe_keys)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "def resolve_embedding_model(cfg):\n"
        "    return ('gemini-embedding-001', 2)\n"
        "def embed_texts(texts, model_name):\n"
        "    if any(presence[k] for k in keys if k != 'GEMINI_API_KEY'):\n"
        "        raise RuntimeError('forbidden credential leaked into embed subprocess')\n"
        "    return [[1.0 if presence.get('GEMINI_API_KEY') else 0.0, 0.5] for _ in texts]\n"
        "def embed_query(text, model_name):\n"
        "    return embed_texts([text], model_name)[0]\n"
        "if __name__ == '__main__':\n"
        "    print(json.dumps({'ok': True, 'status': 'ingested', 'doc_id': 101, 'presence': presence, 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    (ocw_root / "skills" / "company-wiki" / "config.json").write_text(
        json.dumps({"wiki_dir": "wiki", "embedding_model": "gemini-embedding-001"}),
        encoding="utf-8",
    )

    # 5. market-digest CLI (used by `market-digest/run.sh`)
    digest_cli = ocw_root / "skills" / "market-digest" / "scripts" / "market_digest_cli.py"
    digest_cli.write_text(
        "import json, os, sys\n"
        f"keys = {list(probe_keys)!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "print(json.dumps({'ok': True, 'status': 'added', 'note_id': 'N1', 'presence': presence, 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )

    bridge_env_file = tmp_path / "bridge.env"
    bridge_env_file.write_text(
        "DALTON_BRIDGE_TOKEN=bridge_mcp_tok_synth\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=bridge_admin_tok_synth\n"
        "GEMINI_API_KEY=gemini_synth_key_for_wiki\n"
        "OPENAI_API_KEY=openai_synth_in_bridge_env_must_drop\n"
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
    cfg.wiki_ingest_temp_dir = tmp_path / "wiki_tmp"
    cfg.wiki_ingest_audit_file = tmp_path / "wiki_audit.jsonl"
    data_root = tmp_path / "openclaw_data"
    (data_root / "scripts").mkdir(parents=True, exist_ok=True)
    cfg.data_tasks_data_root = data_root
    bridge = rb.ResearchDataBridge(cfg)

    def _assert_all_forbidden_absent(
        presence: dict[str, bool], context: str, *, allow_gemini: bool = False
    ) -> None:
        check_list = list(FORBIDDEN_NONARCHIVE_CHECK_KEYS)
        if not allow_gemini:
            check_list.append("GEMINI_API_KEY")
        leaked = [k for k in check_list if presence.get(k)]
        assert leaked == [], f"{context} leaked forbidden keys: {leaked}"

    # Entry 1: MCP stdio subprocess (`_run_stdio_rpc`)
    mcp_res = await bridge._run_stdio_rpc(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
        timeout_s=10.0,
    )
    _assert_all_forbidden_absent(mcp_res[0]["result"]["presence"], "MCP stdio")

    # Entry 2 & 3: MCP one-shot community (`xueqiu`) & employee CLI (`employee`)
    comm_xq = await bridge._handle_community_call(
        req_id=1,
        route="xueqiu",
        arguments={"route": "xueqiu", "operation": "posts", "ticker": "00700"},
        eff_timeout=10,
    )
    _assert_all_forbidden_absent(
        comm_xq["result"]["structuredContent"]["data"]["presence"],
        "community xueqiu",
    )
    comm_emp = await bridge._handle_community_call(
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
    assert xr_res["version"] == "xreach 0.4.2-synth"
    xr_parsed = json.loads(xreach_out_file.read_text(encoding="utf-8"))
    _assert_all_forbidden_absent(xr_parsed["presence"], "probe_xreach_version")

    # Entry 5: Wiki embedding (`handle_embed`) — retains GEMINI_API_KEY, strips OPENAI_API_KEY & all forbidden keys
    emb_code, emb_body = await bridge.handle_embed({"texts": ["hello world"]})
    assert emb_code == 200 and emb_body["ok"] is True
    assert emb_body["embeddings"] == [[1.0, 0.5]]

    # Entry 6 & 7: Host data task gate & BBG refresh child (`bash bbg_remote_refresh.sh`)
    # Verify that `BASH_ENV` pointing to `poison_rc.sh` is stripped before `bash bbg_remote_refresh.sh` runs!
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

    # Entry 8: Ops launchctl probe (`_probe_launchd_label_status`)
    bin_dir = tmp_path / "ops_stub_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    ops_presence_file = tmp_path / "ops_launchctl_presence.json"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"keys = {list(probe_keys)!r}\n"
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

    # Write a workspace `.env` with GEMINI_API_KEY + forbidden poison keys to verify
    # direct `/bin/bash` invocations of both maintained skill `run.sh` launchers under
    # both a clean empty environment and a poison environment.
    (ocw_root / ".env").write_text(
        "GEMINI_API_KEY=gemini_from_ocw_dotenv\n"
        "OPENAI_API_KEY=openai_poison_in_ocw_dotenv\n"
        "FEISHU_APP_SECRET=feishu_poison_in_ocw_dotenv\n"
        "OPENCLAW_GATEWAY_TOKEN=gw_poison_in_ocw_dotenv\n",
        encoding="utf-8",
    )

    for launcher_sh, cli_argv, label in (
        (WIKI_RUN_SH, ["--json", "add", "--company", "NVDA"], "company-wiki/run.sh"),
        (DIGEST_RUN_SH, ["--json", "add-note", "--title", "T1"], "market-digest/run.sh"),
    ):
        clean_res = subprocess.run(
            ["/bin/bash", str(launcher_sh), *cli_argv],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "DALTON_OCW_ROOT": str(ocw_root),
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert clean_res.returncode == 0, f"{label} clean env failed: {clean_res.stderr}"
        clean_payload = json.loads(clean_res.stdout.strip())
        assert clean_payload["argv"] == cli_argv
        assert clean_payload["presence"]["GEMINI_API_KEY"] is True
        _assert_all_forbidden_absent(
            clean_payload["presence"], f"{label} (clean env)", allow_gemini=True
        )

        poison_res = subprocess.run(
            ["/bin/bash", str(launcher_sh), *cli_argv],
            env={
                **os.environ,
                "DALTON_OCW_ROOT": str(ocw_root),
                "DALTON_SKILL_SHELL_RC": str(shell_rc),
                "BASH_ENV": str(shell_rc),
                "GEMINI_API_KEY": "gemini_synth_key_for_wiki",
            },
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert poison_res.returncode == 0, f"{label} poison env failed: {poison_res.stderr}"
        poison_payload = json.loads(poison_res.stdout.strip())
        assert poison_payload["argv"] == cli_argv
        assert poison_payload["presence"]["GEMINI_API_KEY"] is True
        _assert_all_forbidden_absent(
            poison_payload["presence"], f"{label} (poison env)", allow_gemini=True
        )

    # Both skill `run.sh` launchers through Bridge's actual resolution + production argv
    # even when legacy `ocw_root/skills/*/run.sh` exists on disk or is explicitly configured!
    resolved_wiki_sh = bridge._resolve_wiki_run_sh()
    resolved_digest_sh = bridge._resolve_market_digest_run_sh()
    assert resolved_wiki_sh == WIKI_RUN_SH
    assert resolved_digest_sh == DIGEST_RUN_SH

    bridge.cfg.wiki_run_sh = legacy_wiki_run_sh
    bridge.cfg.market_digest_run_sh = legacy_digest_run_sh
    assert bridge._resolve_wiki_run_sh() == WIKI_RUN_SH
    assert bridge._resolve_market_digest_run_sh() == DIGEST_RUN_SH
    explicit_skill_env = bridge._build_wiki_subprocess_env()
    assert explicit_skill_env.get("DALTON_WIKI_SKILL_DIR") == str(legacy_wiki_run_sh.parent)
    assert explicit_skill_env.get("DALTON_MARKET_DIGEST_SKILL_DIR") == str(
        legacy_digest_run_sh.parent
    )
    bridge.cfg.wiki_run_sh = None
    bridge.cfg.market_digest_run_sh = None

    assert legacy_wiki_run_sh.read_text(encoding="utf-8") == legacy_wiki_content
    assert legacy_digest_run_sh.read_text(encoding="utf-8") == legacy_digest_content

    wiki_env = bridge._build_wiki_subprocess_env()
    wiki_env["DALTON_SKILL_SHELL_RC"] = str(shell_rc)
    wiki_res = await bridge._run_company_wiki_ingest_cli(
        [str(resolved_wiki_sh), "--json", "add", "--company", "NVDA"],
        env=wiki_env,
        timeout_s=15.0,
    )
    assert wiki_res["ok"] is True, wiki_res
    assert wiki_res["argv"] == ["--json", "add", "--company", "NVDA"]
    _assert_all_forbidden_absent(
        wiki_res["presence"], "company-wiki/run.sh", allow_gemini=True
    )
    assert wiki_res["presence"]["GEMINI_API_KEY"] is True

    sales_code, sales_body = await bridge.handle_sales_notes_ingest(
        {
            "institution": "Morgan Stanley",
            "title": "NVDA Blackwell Supply Chain Note",
            "content": "Blackwell GB200 rack shipments tracking ahead of expectations.",
            "tickers": ["NVDA"],
            "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
            "dry_run": True,
        }
    )
    assert sales_code == 200, sales_body
    _assert_all_forbidden_absent(
        sales_body["cli_preview"]["presence"], "market-digest/run.sh", allow_gemini=True
    )
    assert sales_body["cli_preview"]["presence"]["GEMINI_API_KEY"] is True
    assert legacy_wiki_run_sh.read_text(encoding="utf-8") == legacy_wiki_content
    assert legacy_digest_run_sh.read_text(encoding="utf-8") == legacy_digest_content

    # Verify caller-over-file priority for GEMINI_API_KEY, removal of PYTHONPATH / PYTHONSTARTUP
    # before starting the Python filtering helper, and refusal of unsupported explicit skill launchers.
    hijack_dir = tmp_path / "hijack_py"
    hijack_dir.mkdir(parents=True, exist_ok=True)
    # If PYTHONPATH were not unset before starting `bridge_subprocess_env.py`, this `sitecustomize.py`
    # would execute inside the filtering helper and write `hijack_marker`!
    hijack_marker = tmp_path / "pythonpath_hijack_ran.txt"
    (hijack_dir / "sitecustomize.py").write_text(
        f"open({str(hijack_marker)!r}, 'w').write('hijacked')\n",
        encoding="utf-8",
    )
    startup_marker = tmp_path / "pythonstartup_hijack_ran.txt"
    startup_py = tmp_path / "evil_startup.py"
    startup_py.write_text(
        f"open({str(startup_marker)!r}, 'w').write('startup_hijacked')\n",
        encoding="utf-8",
    )
    shell_rc_with_py_controls = tmp_path / "rc_with_py_controls.sh"
    shell_rc_with_py_controls.write_text(
        f"export PYTHONPATH={str(hijack_dir)!r}\n"
        f"export PYTHONSTARTUP={str(startup_py)!r}\n"
        "export FEISHU_APP_SECRET=rc_feishu_secret\n"
        "export GEMINI_API_KEY=gemini_from_shell_rc_must_lose_to_caller\n",
        encoding="utf-8",
    )
    with monkeypatch.context() as mp:
        mp.setenv("GEMINI_API_KEY", "caller_gemini_priority_key")
        mp.setenv("DALTON_SKILL_SHELL_RC", str(shell_rc_with_py_controls))
        bridge.cfg.wiki_run_sh = legacy_wiki_run_sh
        env_with_caller_gemini = bridge._build_wiki_subprocess_env()
        assert env_with_caller_gemini["GEMINI_API_KEY"] == "caller_gemini_priority_key"
        wiki_res_prio = await bridge._run_company_wiki_ingest_cli(
            [str(bridge._resolve_wiki_run_sh()), "--json", "add", "--company", "NVDA"],
            env=env_with_caller_gemini,
            timeout_s=15.0,
        )
        assert wiki_res_prio["ok"] is True
        assert not hijack_marker.exists(), "PYTHONPATH was not scrubbed before starting Python helper!"
        assert not startup_marker.exists(), "PYTHONSTARTUP was not scrubbed!"
        bridge.cfg.wiki_run_sh = None

    # Unsupported explicit launcher (directory without expected skill CLI) must be refused clearly
    unsupported_dir = tmp_path / "unsupported_skill_dir"
    unsupported_dir.mkdir(parents=True, exist_ok=True)
    unsupported_run_sh = unsupported_dir / "run.sh"
    unsupported_run_sh_content = "#!/usr/bin/env bash\necho 'unverified custom script'\n"
    unsupported_run_sh.write_text(unsupported_run_sh_content, encoding="utf-8")
    unsupported_run_sh.chmod(0o755)

    bridge.cfg.wiki_run_sh = unsupported_run_sh
    with pytest.raises(rb.UnsupportedSkillLauncherError):
        bridge._resolve_wiki_run_sh()
    bad_wiki_code, bad_wiki_body = await bridge.handle_wiki_ingest(
        {
            "target_type": "company",
            "target": "NVDA",
            "content_type": "专家访谈",
            "date": "2026-10-25",
            "content": "NVDA architectural notes for Blackwell rack deployment.",
            "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
            "dry_run": False,
        }
    )
    assert bad_wiki_code == 400
    assert bad_wiki_body["error_code"] == "unsupported_launcher"
    bridge.cfg.wiki_run_sh = None

    bridge.cfg.market_digest_run_sh = unsupported_run_sh
    with pytest.raises(rb.UnsupportedSkillLauncherError):
        bridge._resolve_market_digest_run_sh()
    bad_md_code, bad_md_body = await bridge.handle_sales_notes_ingest(
        {
            "institution": "Morgan Stanley",
            "title": "NVDA Note",
            "content": "Supply chain update.",
            "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
            "dry_run": False,
        }
    )
    assert bad_md_code == 400
    assert bad_md_body["error_code"] == "unsupported_launcher"
    bridge.cfg.market_digest_run_sh = None
    assert unsupported_run_sh.read_text(encoding="utf-8") == unsupported_run_sh_content

    # Direct invocation of maintained `run.sh` with invalid explicit `DALTON_*_SKILL_DIR` must also fail
    bad_dir_res = subprocess.run(
        ["/bin/bash", str(WIKI_RUN_SH), "--json", "add"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DALTON_WIKI_SKILL_DIR": str(unsupported_dir),
            "DALTON_OCW_ROOT": str(ocw_root),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert bad_dir_res.returncode != 0
    assert "unsupported DALTON_WIKI_SKILL_DIR" in bad_dir_res.stderr

    # Batch 1 transitional card-send subprocess (`_run_feishu_send_card`):
    card_probe_out = tmp_path / "card_send_presence.json"
    fake_card_script = tmp_path / "fake_feishu_send_card.py"
    card_check_keys = probe_keys + ("DALTON_NO_DIRECT_SEND",)
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
        feishu_app_id="cli_bridge_configured_app",
        feishu_app_secret="sec_bridge_configured_secret",
        feishu_default_grant_chat_id="oc_bridge_configured_chat",
    )
    assert card_res["ok"] is True
    card_report = json.loads(card_probe_out.read_text(encoding="utf-8"))
    assert card_report["explicit_match"] is True
    card_pres = card_report["presence"]
    assert card_pres["FEISHU_APP_ID"] is True
    assert card_pres["FEISHU_APP_SECRET"] is True
    assert card_pres["FEISHU_DEFAULT_GRANT_CHAT_ID"] is True
    for k in card_check_keys:
        if k not in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DEFAULT_GRANT_CHAT_ID"):
            assert card_pres[k] is False, f"card-send leaked {k}"


@pytest.mark.asyncio
async def test_archive_and_reextract_import_safety_dsn_selection_model_providers_and_late_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify:
    1. Importing `conversation_archive` and `reextract_conversations` does not mutate `os.environ`.
    2. `DALTON_REEXTRACT_DSN` is preserved across dotenv-only, pre-exported, and CLI `--dsn` cases
       for `plan`, `apply`, `render`, and `rollback`, and validation failure exits non-zero before
       opening any database connection.
    3. Primary/fallback/assist models from CLI (`grok-4.7-oauth`), `plan.json` (`glm-5-turbo`),
       and DB user preferences (`kimi-k2.5`) distinct from `dalton.yaml` default (`deepseek-flash`)
       hydrate ONLY the needed provider keys from synthetic `.env` and strip unselected providers.
    4. Late `load_from_files()` / `load_dotenv_async()` calls in `conversation_archive.py`,
       `reextract_conversations.py:_build_shared_llm_service`, and
       `src/dalton/conversations/render.py:_resolve_workspace_backend` load from the bound
       synthetic `.env` (`DALTON_DOTENV_PATH`) and re-apply final filtering so forbidden/unrelated
       keys do not persist while selected model and workspace runtime keys are preserved.
    """
    dotenv_dir = tmp_path / "dotenv_isolated"
    dotenv_dir.mkdir(parents=True, exist_ok=True)
    ocw_agents_dir = tmp_path / "ocw_agents"
    ocw_agents_dir.mkdir(parents=True, exist_ok=True)

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
        "DALTON_REEXTRACT_DSN=postgresql://postgres:synth@127.0.0.1:5432/dalton_scratch_dotenv\n"
        "SANDBOX_PROVIDER=docker\n"
        "DOCKER_SANDBOX_IMAGE=dalton-sandbox:synth\n"
        "DEEPSEEK_API_KEY=synth_deepseek_key\n"
        "ZAI_API_KEY=synth_zai_key_only_in_dotenv\n"
        "MOONSHOT_API_KEY=synth_moonshot_key_only_in_dotenv\n"
        "DALTON_XAI_OAUTH_CLIENT_ID=synth_xai_oauth_client_only_in_dotenv\n"
        "OPENAI_API_KEY=synth_openai_unselected_must_drop\n"
        "ANTHROPIC_API_KEY=synth_anthropic_unselected_must_drop\n"
        "GEMINI_API_KEY=synth_gemini_unselected_must_drop\n"
        "XAI_API_KEY=synth_xai_cash_unselected_must_drop\n"
        "FMP_API_KEY=synth_fmp_unrelated_must_drop\n"
        "TAVILY_API_KEY=synth_tavily_unrelated_must_drop\n"
        "FEISHU_APP_ID=cli_dotenv_poison\n"
        "FEISHU_APP_SECRET=sec_dotenv_poison\n"
        "OPENCLAW_GATEWAY_TOKEN=gw_dotenv_poison\n"
        "SEED_ASR_ACCESS_KEY=asr_dotenv_poison\n"
        "DALTON_BRIDGE_TOKEN=mcp_dotenv_poison\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=admin_dotenv_tok\n"
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

    # 1. Verify in a fresh subprocess that importing `conversation_archive` and `reextract_conversations`
    #    does NOT delete caller variables or dotenv-only `DALTON_REEXTRACT_DSN`, that `src.config`
    #    honors `DALTON_DOTENV_PATH` rather than loading a poison `.env` from CWD, and that
    #    `reextract_conversations` preserves dotenv-only `DALTON_REEXTRACT_DSN` across apply/rollback/plan/render!
    cwd_poison_dir = tmp_path / "cwd_poison"
    cwd_poison_dir.mkdir(parents=True, exist_ok=True)
    (cwd_poison_dir / ".env").write_text(
        "CWD_POISON_SENTINEL=leaked_from_cwd_dotenv\n"
        "DALTON_REEXTRACT_DSN=postgresql://postgres:wrong@127.0.0.1:5432/wrong_cwd_db\n",
        encoding="utf-8",
    )

    import_and_dsn_probe = tmp_path / "import_and_dsn_probe.py"
    import_and_dsn_probe.write_text(
        "import argparse, json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from dotenv import dotenv_values\n"
        "os.environ['CALLER_SENTINEL_VAR'] = 'keep_on_import'\n"
        "env_before_src_config = dict(os.environ)\n"
        "import src.config\n"
        "env_after_src_config = dict(os.environ)\n"
        "import scripts.dalton.conversation_archive as ca\n"
        "env_after_ca = dict(os.environ)\n"
        "import scripts.dalton.reextract_conversations as rx\n"
        "env_after_import = dict(os.environ)\n"
        "expected_dotenv_keys = set(dotenv_values(os.environ['DALTON_DOTENV_PATH']).keys())\n"
        "caller_preserved_on_src_config = all(env_after_src_config.get(k) == v for k, v in env_before_src_config.items())\n"
        "src_config_added_keys = set(env_after_src_config.keys()) - set(env_before_src_config.keys())\n"
        "src_config_only_explicit_dotenv = (src_config_added_keys <= expected_dotenv_keys and bool(src_config_added_keys))\n"
        "import_unchanged = (env_after_src_config == env_after_ca == env_after_import and caller_preserved_on_src_config and src_config_only_explicit_dotenv)\n"
        "sentinel_kept_on_import = (os.environ.get('CALLER_SENTINEL_VAR') == 'keep_on_import')\n"
        "cwd_poison_present = ('CWD_POISON_SENTINEL' in os.environ)\n"
        "dsn_before_filter = rx.resolve_dsn(argparse.Namespace(dsn=None))\n"
        "rx.bridge_subprocess_env.enforce_reextract_process_env_in_place(\n"
        "    os.environ, 'apply', explicit_dsn=dsn_before_filter, repo_root=rx.REPO_ROOT, hydrate_from_dotenv=True, validate_required=True\n"
        ")\n"
        "dsn_after_apply_filter = rx.resolve_dsn(argparse.Namespace(dsn=None))\n"
        "cli_dsn = 'postgresql://postgres:synth@127.0.0.1:5432/dalton_scratch_cli'\n"
        "rx.bridge_subprocess_env.enforce_reextract_process_env_in_place(\n"
        "    os.environ, 'rollback', explicit_dsn=cli_dsn, repo_root=rx.REPO_ROOT, hydrate_from_dotenv=True, validate_required=True\n"
        ")\n"
        "dsn_after_cli_override = rx.resolve_dsn(argparse.Namespace(dsn=cli_dsn))\n"
        "print('RESULT_JSON:' + json.dumps({\n"
        "    'import_unchanged': import_unchanged,\n"
        "    'sentinel_kept_on_import': sentinel_kept_on_import,\n"
        "    'cwd_poison_present': cwd_poison_present,\n"
        "    'dsn_before_filter': dsn_before_filter,\n"
        "    'dsn_after_apply_filter': dsn_after_apply_filter,\n"
        "    'dsn_after_cli_override': dsn_after_cli_override,\n"
        "    'feishu_present': 'FEISHU_APP_SECRET' in os.environ,\n"
        "    'openai_present': 'OPENAI_API_KEY' in os.environ,\n"
        "}))\n",
        encoding="utf-8",
    )
    res_imp = subprocess.run(
        [sys.executable, str(import_and_dsn_probe)],
        cwd=str(cwd_poison_dir),
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DALTON_DOTENV_PATH": str(synth_repo_env),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_imp.returncode == 0, res_imp.stderr
    imp_lines = [
        ln[len("RESULT_JSON:") :]
        for ln in res_imp.stdout.splitlines()
        if ln.startswith("RESULT_JSON:")
    ]
    assert len(imp_lines) == 1, res_imp.stdout
    imp_data = json.loads(imp_lines[0])
    assert imp_data["import_unchanged"] is True
    assert imp_data["sentinel_kept_on_import"] is True
    assert imp_data["cwd_poison_present"] is False
    assert imp_data["dsn_before_filter"] == "postgresql://postgres:synth@127.0.0.1:5432/dalton_scratch_dotenv"
    assert imp_data["dsn_after_apply_filter"] == "postgresql://postgres:synth@127.0.0.1:5432/dalton_scratch_dotenv"
    assert imp_data["dsn_after_cli_override"] == "postgresql://postgres:synth@127.0.0.1:5432/dalton_scratch_cli"
    assert imp_data["feishu_present"] is False
    assert imp_data["openai_present"] is False

    # Verify `environment > dotenv` precedence in `build_archive_subprocess_env`
    override_env = bse.build_archive_subprocess_env(
        repo_env_path=synth_repo_env,
        bridge_env_path=synth_bridge_env,
        base_env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DB_HOST": "10.99.0.1",
            "DEEPSEEK_API_KEY": "env_override_deepseek_key",
        },
        explicit_model="deepseek-flash",
        validate_required=True,
    )
    assert override_env["DB_HOST"] == "10.99.0.1"
    assert override_env["DEEPSEEK_API_KEY"] == "env_override_deepseek_key"

    # Verify `reextract_conversations.main` validation failure (including invalid DSN when DB_* is set)
    # cannot fall through to any DB connection
    with monkeypatch.context() as mp:
        empty_env_file = tmp_path / "empty.env"
        empty_env_file.write_text("", encoding="utf-8")
        for k in list(os.environ.keys()):
            if not k.startswith("PYTEST_"):
                mp.delenv(k, raising=False)
        mp.setenv("PATH", "/usr/bin:/bin")
        mp.setenv("HOME", str(tmp_path))
        mp.setenv("DALTON_DOTENV_PATH", str(empty_env_file))
        conn_spy = create_autospec(reextract_mod._open_autocommit_conn)
        mp.setattr(reextract_mod, "_open_autocommit_conn", conn_spy)
        rc = reextract_mod.main(["apply", "--run-id", "run-1"], validate_env=True)
        assert rc == archive_mod.EXIT_CODE_FATAL
        conn_spy.assert_not_called()

        # Also when valid DB_* vars exist in `.env` but `--dsn` or `DALTON_REEXTRACT_DSN` is invalid
        mp.setenv("DALTON_DOTENV_PATH", str(synth_repo_env))
        rc_bad_dsn = reextract_mod.main(
            ["apply", "--run-id", "run-1", "--dsn", "mysql://invalid/db"],
            validate_env=True,
        )
        assert rc_bad_dsn == archive_mod.EXIT_CODE_FATAL
        conn_spy.assert_not_called()

    # 2. Verify CLI `--model grok-4.7-oauth` via `archive_daily.sh` with `DALTON_XAI_OAUTH_CLIENT_ID`
    #    present ONLY in `synth_repo_env` (and `DALTON_ASSIST_FALLBACK_MODEL=grok-4.7-oauth`):
    #    retains `DALTON_XAI_OAUTH_CLIENT_ID` and `SANDBOX_PROVIDER`, drops `DEEPSEEK_API_KEY`,
    #    `ZAI_API_KEY`, `MOONSHOT_API_KEY`, `OPENAI_API_KEY`, `FEISHU_*`, etc.
    check_keys = list(FORBIDDEN_NONARCHIVE_CHECK_KEYS) + [
        "DB_HOST",
        "BYOK_ENCRYPTION_KEY",
        "DALTON_XAI_OAUTH_CLIENT_ID",
        "SANDBOX_PROVIDER",
        "DALTON_BRIDGE_EMBED_URL",
        "no_proxy",
    ]
    archive_probe = tmp_path / "archive_cli_model_probe.py"
    archive_probe.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import scripts.dalton.conversation_archive as ca\n"
        "ca.bridge_subprocess_env.enforce_archive_process_env_in_place(\n"
        "    os.environ, explicit_model='grok-4.7-oauth', repo_root=ca.REPO_ROOT, hydrate_from_dotenv=True, validate_required=True\n"
        ")\n"
        f"keys = {check_keys!r}\n"
        "presence = {k: (k in os.environ and bool(os.environ.get(k))) for k in keys}\n"
        "print(json.dumps({'presence': presence, 'embed': os.environ.get('DALTON_BRIDGE_EMBED_URL')}))\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "archive_state.json"
    state_file.write_text('{"enabled": true}\n', encoding="utf-8")
    status_file = tmp_path / "archive_status.json"
    pid_file = tmp_path / "archive.pid"

    res_grok = subprocess.run(
        ["/bin/bash", str(ARCHIVE_DAILY_SH), "--model", "grok-4.7-oauth"],
        cwd=str(dotenv_dir),
        env={
            **_PoisonEnvDict(),
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DALTON_ASSIST_FALLBACK_MODEL": "grok-4.7-oauth",
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
    assert res_grok.returncode == 0, res_grok.stdout + "\n" + res_grok.stderr
    grok_lines = [ln for ln in res_grok.stdout.splitlines() if ln.strip().startswith("{")]
    grok_report = json.loads(grok_lines[0])
    grok_pres = grok_report["presence"]
    assert grok_report["embed"] == "http://127.0.0.1:8960/embed"
    assert grok_pres["DALTON_XAI_OAUTH_CLIENT_ID"] is True
    assert grok_pres["BYOK_ENCRYPTION_KEY"] is True
    assert grok_pres["SANDBOX_PROVIDER"] is True
    assert grok_pres["DEEPSEEK_API_KEY"] is False
    assert grok_pres["ZAI_API_KEY"] is False
    assert grok_pres["MOONSHOT_API_KEY"] is False
    assert grok_pres["OPENAI_API_KEY"] is False
    assert grok_pres["FEISHU_APP_SECRET"] is False

    # 3. Verify `reextract_conversations.py` plan model (`glm-5-turbo` -> `ZAI_API_KEY` only in `.env`)
    #    AND late `load_from_files()` in `_build_shared_llm_service`:
    #    First confirm `load_dotenv_async()` actually loads `FEISHU_APP_SECRET` and `ZAI_API_KEY` from
    #    `DALTON_DOTENV_PATH`, then confirm `_build_shared_llm_service(model='glm-5-turbo', ...)`
    #    retains `ZAI_API_KEY` and strips `FEISHU_APP_SECRET`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`.
    with monkeypatch.context() as mp:
        for k in list(os.environ.keys()):
            if not k.startswith("PYTEST_"):
                mp.delenv(k, raising=False)
        mp.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
        mp.setenv("HOME", str(tmp_path))
        mp.setenv("DALTON_DOTENV_PATH", str(synth_repo_env))
        mp.setenv("DALTON_ASSIST_FALLBACK_MODEL", "glm-5-turbo")

        # Prove `load_dotenv_async()` loads the synthetic `.env` (including forbidden keys) before filtering
        from src.ptc_agent.config.utils import load_dotenv_async

        await load_dotenv_async()
        assert os.environ.get("FEISHU_APP_SECRET") == "sec_dotenv_poison"
        assert os.environ.get("ZAI_API_KEY") == "synth_zai_key_only_in_dotenv"
        assert os.environ.get("OPENAI_API_KEY") == "synth_openai_unselected_must_drop"

        # Now run `enforce_reextract_process_env_in_place` + `_build_shared_llm_service` for plan model `glm-5-turbo`
        bse.enforce_reextract_process_env_in_place(
            os.environ,
            "extract",
            explicit_model="glm-5-turbo",
            explicit_fallback_model="gemini-4-argon-medium-oauth",
            repo_root=REPO_ROOT,
            hydrate_from_dotenv=True,
            preserve_pytest=True,
            validate_required=True,
        )
        _ = await reextract_mod._build_shared_llm_service(
            model="glm-5-turbo",
            fallback_model="gemini-4-argon-medium-oauth",
        )
        assert os.environ.get("ZAI_API_KEY") == "synth_zai_key_only_in_dotenv"
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ
        assert "FEISHU_APP_SECRET" not in os.environ
        assert "OPENCLAW_GATEWAY_TOKEN" not in os.environ

    # 4. Verify `render.py:_resolve_workspace_backend` late `load_from_files(context=ConfigContext.SDK)`
    #    re-filters forbidden/unrelated keys injected by `load_dotenv_async()` while preserving workspace keys,
    #    even if `load_from_files()` raises an exception after calling `load_dotenv_async()`!
    with monkeypatch.context() as mp:
        for k in list(os.environ.keys()):
            if not k.startswith("PYTEST_"):
                mp.delenv(k, raising=False)
        mp.setenv("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
        mp.setenv("HOME", str(tmp_path))
        mp.setenv("DALTON_DOTENV_PATH", str(synth_repo_env))
        mp.chdir(REPO_ROOT)
        bse.enforce_reextract_process_env_in_place(
            os.environ,
            "render",
            repo_root=REPO_ROOT,
            hydrate_from_dotenv=True,
            preserve_pytest=True,
            validate_required=True,
        )
        assert "FEISHU_APP_SECRET" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ

        from src.server.services.workspace_manager import WorkspaceManager

        fake_wm = create_autospec(WorkspaceManager, instance=True)
        fake_session = type(
            "_FakeSession",
            (),
            {"sandbox": type("_FakeSandbox", (), {"working_dir": "/workspace"})()},
        )()
        fake_wm.get_session_for_workspace.return_value = fake_session
        calls = {"n": 0}

        def _fake_get_instance(cls_or_config: Any = None, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1 and cls_or_config is None and not kwargs:
                raise ValueError("WorkspaceManager not initialized")
            return fake_wm

        mp.setattr(WorkspaceManager, "get_instance", _fake_get_instance)
        import src.server.database.workspace as ws_db_mod

        mp.setattr(
            ws_db_mod,
            "get_workspace",
            create_autospec(ws_db_mod.get_workspace, return_value={"dir_name": "NVDA"}),
        )

        backend = await render_mod._resolve_workspace_backend("ws-123", user_id="u-1")
        assert backend is not None
        assert calls["n"] == 2
        assert "FEISHU_APP_SECRET" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert os.environ.get("SANDBOX_PROVIDER") == "docker"
        assert os.environ.get("DALTON_BRIDGE_EMBED_URL") == "http://127.0.0.1:8960/embed"

        # Also verify `try ... finally:` post-dotenv scrubbing AND caller-over-dotenv precedence
        # if `load_from_files` raises after `load_dotenv_async`
        import ptc_agent.config as ptc_config_direct_mod
        import src.ptc_agent.config as ptc_config_mod
        from src.ptc_agent.config.utils import load_dotenv_async as _real_load_dotenv_async

        async def _load_dotenv_then_fail(*_args: Any, **_kwargs: Any) -> Any:
            await _real_load_dotenv_async()
            os.environ["SANDBOX_PROVIDER"] = "overwritten_by_late_dotenv"
            raise RuntimeError("simulated config failure after load_dotenv_async")

        calls["n"] = 0
        with monkeypatch.context() as fail_mp:
            fail_mp.setenv("SANDBOX_PROVIDER", "caller_daytona_wins")
            fail_mp.setattr(ptc_config_mod, "load_from_files", _load_dotenv_then_fail)
            fail_mp.setattr(ptc_config_direct_mod, "load_from_files", _load_dotenv_then_fail)
            with pytest.raises(RuntimeError, match="simulated config failure after load_dotenv_async"):
                await render_mod._resolve_workspace_backend("ws-123", user_id="u-1")
            assert "FEISHU_APP_SECRET" not in os.environ
            assert "OPENAI_API_KEY" not in os.environ
            assert os.environ.get("SANDBOX_PROVIDER") == "caller_daytona_wins"


# ---------------------------------------------------------------------------
# 3. Controlled archive execution, preference model selection, BYOK/OAuth store, & `/feishu/*`
# ---------------------------------------------------------------------------


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
async def test_archive_pipeline_executes_preference_model_selection_late_load_and_host_embed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the filtered archive allowlist environment with synthetic `.env`:
    - When `resolve_dalton_extract_model` returns a user preference model (`kimi-k2.5`) distinct
      from `dalton.yaml` default (`deepseek-flash`), `run_archive_pipeline` hydrates `MOONSHOT_API_KEY`
      from `DALTON_DOTENV_PATH` (or preserves caller env `provider_candidates`) and strips
      `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `FEISHU_*`, etc., including after late `load_from_files()`
      in `LLMService` initialization.
    - `BYOK_ENCRYPTION_KEY` is retained for `get_encryption_key()` / `encryption_configured()` and
      autospecced `get_key_for_provider` / `get_oauth_tokens`.
    - `embed_texts_via_bridge` posts to `http://127.0.0.1:8960/embed` with `Bearer <DALTON_BRIDGE_ADMIN_TOKEN>`.
    """
    owner_uid = "11111111-1111-4111-8111-111111111111"
    synth_dotenv = tmp_path / "pref_model.env"
    synth_dotenv.write_text(
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=5432\n"
        "DB_NAME=dalton_test\n"
        "DB_USER=postgres\n"
        "DB_PASSWORD=synth_password\n"
        "BYOK_ENCRYPTION_KEY=synth-byok-key-for-archive-unit-test\n"
        f"AUTH_USER_ID={owner_uid}\n"
        f"DALTON_PM_USER_IDS={owner_uid}\n"
        "DALTON_ENABLED=true\n"
        "DEEPSEEK_API_KEY=synth_deepseek_yaml_default_must_drop_after_pref\n"
        "MOONSHOT_API_KEY=synth_moonshot_pref_model_key_only_in_dotenv\n"
        "OPENAI_API_KEY=synth_unselected_openai\n"
        "FEISHU_APP_SECRET=synth_feishu_secret_must_drop\n",
        encoding="utf-8",
    )
    synth_bridge_env = tmp_path / "pref_bridge.env"
    synth_bridge_env.write_text(
        "DALTON_BRIDGE_ADMIN_TOKEN=synth_archive_admin_token_777\n"
        "DALTON_BRIDGE_TOKEN=synth_mcp_token_must_drop\n",
        encoding="utf-8",
    )

    # Start from outer launcher env (before DB preference model is known -> carries YAML default `deepseek-flash`
    # and `DALTON_DOTENV_PATH=synth_dotenv`).
    # 1) When caller `base_env` supplies `BYOK_ENCRYPTION_KEY` (`byok_synth_unrelated` in `_PoisonEnvDict()`),
    #    caller environment takes precedence over `synth_dotenv`.
    caller_byok_env = bse.build_archive_subprocess_env(
        repo_env_path=synth_dotenv,
        bridge_env_path=synth_bridge_env,
        base_env={
            **_PoisonEnvDict(),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
        },
        validate_required=True,
    )
    assert caller_byok_env["BYOK_ENCRYPTION_KEY"] == "byok_synth_unrelated"

    # 2) When caller `base_env` omits `BYOK_ENCRYPTION_KEY` and `MOONSHOT_API_KEY`, `BYOK_ENCRYPTION_KEY`
    #    is hydrated from `synth_dotenv`, and `MOONSHOT_API_KEY` is absent before DB preference resolution
    #    and hydrated from `synth_dotenv` once `resolve_dalton_extract_model` returns `kimi-k2.5`.
    allowlist_env = bse.build_archive_subprocess_env(
        repo_env_path=synth_dotenv,
        bridge_env_path=synth_bridge_env,
        base_env={
            **{
                k: v
                for k, v in _PoisonEnvDict().items()
                if k not in ("BYOK_ENCRYPTION_KEY", "MOONSHOT_API_KEY")
            },
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
        },
        validate_required=True,
    )
    assert "MOONSHOT_API_KEY" not in allowlist_env
    for k in list(os.environ.keys()):
        if not k.startswith("PYTEST_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in caller_byok_env.items():
        monkeypatch.setenv(k, v)
    assert encryption_configured() is True
    assert get_encryption_key() == "byok_synth_unrelated"

    for k, v in allowlist_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(REPO_ROOT)
    assert encryption_configured() is True
    assert get_encryption_key() == "synth-byok-key-for-archive-unit-test"

    get_key_spy = create_autospec(
        db_api_keys.get_key_for_provider,
        return_value="sk-moonshot-byok-decrypted",
    )
    get_oauth_spy = create_autospec(
        db_oauth_tokens.get_oauth_tokens,
        return_value={"access_token": "oa-acc", "refresh_token": "oa-ref"},
    )
    assert await get_key_spy(owner_uid, "moonshot") == "sk-moonshot-byok-decrypted"
    assert (await get_oauth_spy(owner_uid, "antigravity-oauth"))["access_token"] == "oa-acc"

    embed_requests: list[tuple[str, str, dict[str, Any]]] = []

    def _embed_urlopen_handler(req: Any, timeout: Any = None) -> _FakeUrllibResponse:
        _ = timeout
        body_raw = req.data.decode("utf-8") if req.data else "{}"
        body = json.loads(body_raw)
        auth_hdr = (
            req.get_header("Authorization")
            or req.headers.get("Authorization")
            or req.headers.get("authorization")
            or ""
        )
        embed_requests.append((str(req.full_url), str(auth_hdr), body))
        texts = body.get("texts") or []
        resp_bytes = json.dumps(
            {
                "ok": True,
                "model": "gemini-embedding-001",
                "embeddings": [[0.25, 0.75] for _ in texts],
            }
        ).encode("utf-8")
        return _FakeUrllibResponse(resp_bytes, 200)

    monkeypatch.setattr(embedder_mod.urllib.request, "urlopen", _embed_urlopen_handler)

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

    # User preference in DB selects `kimi-k2.5` (provider `moonshot` -> `MOONSHOT_API_KEY`)
    resolve_model_spy = create_autospec(
        archive_mod.resolve_dalton_extract_model,
        return_value="kimi-k2.5",
    )
    monkeypatch.setattr(archive_mod, "resolve_dalton_extract_model", resolve_model_spy)

    async def _fake_extract(
        events: list[dict[str, Any]],
        *,
        extractor_model: str | None = None,
        prompt_version: str | None = None,
        user_id: str | None = None,
        llm_service: Any = None,
        resend_indeterminate: bool = False,
        persist_raw: bool = True,
        skip_journal: bool = False,
        deadline: float | None = None,
    ) -> SegmentExtractionOutcome:
        _ = (
            events,
            prompt_version,
            llm_service,
            resend_indeterminate,
            persist_raw,
            skip_journal,
            deadline,
        )
        assert extractor_model == "kimi-k2.5"
        assert user_id == owner_uid
        # Verify preference model key was hydrated from `.env` and YAML default `DEEPSEEK_API_KEY` was dropped!
        assert os.environ.get("MOONSHOT_API_KEY") == "synth_moonshot_pref_model_key_only_in_dotenv"
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert "OPENAI_API_KEY" not in os.environ
        assert "FEISHU_APP_SECRET" not in os.environ
        return SegmentExtractionOutcome(
            ok=True,
            model_used="kimi-k2.5",
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
    assert summary["extractor_model"] == "kimi-k2.5"
    resolve_model_spy.assert_awaited_once_with(owner_uid)
    extract_spy.assert_awaited_once()
    insert_cards_spy.assert_awaited_once()
    assert len(embed_requests) == 1
    req_url, req_auth, req_body = embed_requests[0]
    assert req_url == "http://127.0.0.1:8960/embed"
    assert req_auth == "Bearer synth_archive_admin_token_777"
    assert req_body["texts"] == ["NVDA 2026Q3 数据中心营收预计达 420 亿美元"]

    # Also verify env-only provider key (`MOONSHOT_API_KEY` only in process env, NOT in `.env`)
    # survives early CLI filtering via `capture_archive_provider_candidates` + `provider_candidates`,
    # that no module-global cache (`_LAST_PROCESS_PROVIDER_CANDIDATES`) exists, and that repeated
    # invocations in one process cannot recover a key from an earlier invocation!
    assert not hasattr(bse, "_LAST_PROCESS_PROVIDER_CANDIDATES")
    env_only_dotenv = tmp_path / "env_only_pref.env"
    env_only_dotenv.write_text(
        "DB_HOST=127.0.0.1\n"
        "DB_PORT=5432\n"
        "DB_NAME=dalton_test\n"
        "DB_USER=postgres\n"
        "DB_PASSWORD=synth_password\n"
        "BYOK_ENCRYPTION_KEY=synth-byok-key-for-archive-unit-test\n"
        f"AUTH_USER_ID={owner_uid}\n"
        f"DALTON_PM_USER_IDS={owner_uid}\n"
        "DALTON_ENABLED=true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_DOTENV_PATH", str(env_only_dotenv))
    monkeypatch.delenv("DALTON_ASSIST_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MOONSHOT_API_KEY", "synth_moonshot_only_in_process_env")
    candidates = bse.capture_archive_provider_candidates(os.environ, repo_root=REPO_ROOT)
    bse.enforce_archive_process_env_in_place(
        os.environ,
        explicit_model=None,
        repo_root=REPO_ROOT,
        hydrate_from_dotenv=True,
        preserve_pytest=True,
        validate_required=True,
        defer_primary_model_validation=True,
    )
    # Before DB preference resolution, deferred primary validation drops `MOONSHOT_API_KEY`
    # and does NOT require unused YAML default `DEEPSEEK_API_KEY`!
    assert "MOONSHOT_API_KEY" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ
    # Once DB preference `kimi-k2.5` is resolved, explicit `provider_candidates` restores `MOONSHOT_API_KEY`
    bse.enforce_archive_process_env_in_place(
        os.environ,
        explicit_model="kimi-k2.5",
        repo_root=REPO_ROOT,
        hydrate_from_dotenv=True,
        preserve_pytest=True,
        validate_required=True,
        provider_candidates=candidates,
    )
    assert os.environ.get("MOONSHOT_API_KEY") == "synth_moonshot_only_in_process_env"
    assert "DEEPSEEK_API_KEY" not in os.environ

    # Verify a subsequent invocation without `provider_candidates` after removing `MOONSHOT_API_KEY`
    # cannot recover `MOONSHOT_API_KEY` from any global cache!
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    bse.enforce_archive_process_env_in_place(
        os.environ,
        explicit_model="kimi-k2.5",
        repo_root=REPO_ROOT,
        hydrate_from_dotenv=True,
        preserve_pytest=True,
        validate_required=True,
    )
    assert "MOONSHOT_API_KEY" not in os.environ
    with pytest.raises(bse.ArchiveEnvValidationError):
        bse.enforce_archive_process_env_in_place(
            os.environ,
            explicit_model="grok-4.7-oauth",
            repo_root=REPO_ROOT,
            hydrate_from_dotenv=True,
            preserve_pytest=True,
            validate_required=True,
        )

    # Exercise full launchd boundary: `archive_daily.sh -> bridge_subprocess_env.py exec-archive ->
    # conversation_archive.py main -> run_archive_pipeline` with:
    # - `MOONSHOT_API_KEY` ONLY in caller environment (not in `.env`)
    # - `DEEPSEEK_API_KEY` (unused YAML default model's provider key) ABSENT everywhere
    # - Database user preference selecting `kimi-k2.5`
    launchd_chain_runner = tmp_path / "launchd_archive_main_runner.py"
    launchd_chain_out = tmp_path / "launchd_archive_main_out.json"
    launchd_chain_runner.write_text(
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import scripts.dalton.conversation_archive as ca\n"
        "pre_main_env_has_moonshot = ('MOONSHOT_API_KEY' in os.environ)\n"
        "pre_main_env_has_fd = ('_DALTON_ARCHIVE_CANDIDATES_FD' in os.environ)\n"
        "observed = {}\n"
        "async def _fake_resolve_model(uid):\n"
        "    observed['during_resolve_moonshot_in_env'] = ('MOONSHOT_API_KEY' in os.environ)\n"
        "    observed['during_resolve_fd_in_env'] = ('_DALTON_ARCHIVE_CANDIDATES_FD' in os.environ)\n"
        "    return 'kimi-k2.5'\n"
        "async def _fake_extract_cards(segment, *, extractor_model=None, **kwargs):\n"
        "    observed['extractor_model'] = extractor_model\n"
        "    return ca.SegmentExtractionOutcome(ok=True, model_used=str(extractor_model or ''), status='completed', cards=[])\n"
        "ca.resolve_dalton_extract_model = _fake_resolve_model\n"
        "ca.extract_segment_cards = _fake_extract_cards\n"
        "rc = ca.main(['run', '--dry-run', '--skip-host-export', '--inbox-file', "
        f"{str(inbox_file)!r}"
        "], validate_env=True)\n"
        "observed['rc'] = rc\n"
        "observed['pre_main_env_has_moonshot'] = pre_main_env_has_moonshot\n"
        "observed['pre_main_env_has_fd'] = pre_main_env_has_fd\n"
        "observed['post_pipeline_moonshot'] = os.environ.get('MOONSHOT_API_KEY')\n"
        "observed['post_pipeline_deepseek_present'] = ('DEEPSEEK_API_KEY' in os.environ)\n"
        "observed['post_pipeline_openai_present'] = ('OPENAI_API_KEY' in os.environ)\n"
        "observed['post_pipeline_feishu_present'] = ('FEISHU_APP_SECRET' in os.environ)\n"
        f"open({str(launchd_chain_out)!r}, 'w').write(json.dumps(observed))\n"
        "sys.exit(rc)\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "launchd_state.json"
    state_file.write_text('{"enabled": true}\n', encoding="utf-8")
    status_file = tmp_path / "launchd_status.json"
    pid_file = tmp_path / "launchd.pid"
    ocw_agents_dir = tmp_path / "launchd_ocw_agents"
    ocw_agents_dir.mkdir(parents=True, exist_ok=True)

    res_launchd = subprocess.run(
        ["/bin/bash", str(ARCHIVE_DAILY_SH)],
        cwd=str(tmp_path),
        env={
            **_PoisonEnvDict(),
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "MOONSHOT_API_KEY": "caller_only_moonshot_across_launchd",
            "DALTON_REPO_PYTHON": sys.executable,
            "DALTON_REPO_ENV_FILE": str(env_only_dotenv),
            "DALTON_BRIDGE_ENV_FILE": str(synth_bridge_env),
            "DALTON_ARCHIVE_STATE_FILE": str(state_file),
            "DALTON_ARCHIVE_STATUS_FILE": str(status_file),
            "DALTON_ARCHIVE_PID_FILE": str(pid_file),
            "DALTON_CONVERSATION_ARCHIVE_PY": str(launchd_chain_runner),
            "DALTON_OCW_AGENTS_ROOT": str(ocw_agents_dir),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res_launchd.returncode == 0, res_launchd.stdout + "\n" + res_launchd.stderr
    launchd_obs = json.loads(launchd_chain_out.read_text(encoding="utf-8"))
    assert launchd_obs["rc"] == 0
    assert launchd_obs["pre_main_env_has_moonshot"] is False
    assert launchd_obs["pre_main_env_has_fd"] is True
    assert launchd_obs["during_resolve_moonshot_in_env"] is False
    assert launchd_obs["during_resolve_fd_in_env"] is False
    assert launchd_obs["post_pipeline_moonshot"] == "caller_only_moonshot_across_launchd"
    assert launchd_obs["post_pipeline_deepseek_present"] is False
    assert launchd_obs["post_pipeline_openai_present"] is False
    assert launchd_obs["post_pipeline_feishu_present"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archive_allowlist_env_pgcrypto_api_keys_and_oauth_tokens_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real PostgreSQL `pgcrypto` encryption/decryption through `src.server.database.api_keys`
    and `src.server.database.oauth_tokens` under the filtered archive allowlist environment.

    Uses `DALTON_TEST_PGCRYPTO_DSN` or `TEST_DATABASE_URL` (or `DB_*` env vars) pointing to an
    isolated PostgreSQL database and rolls back its transaction on completion.
    """
    raw_dsn = (
        os.environ.get("DALTON_TEST_PGCRYPTO_DSN", "").strip()
        or os.environ.get("TEST_DATABASE_URL", "").strip()
    )
    if raw_dsn:
        parsed = urlparse(raw_dsn)
        db_host = parsed.hostname or "127.0.0.1"
        db_port = str(parsed.port or 5432)
        db_name = (parsed.path or "/postgres").lstrip("/") or "postgres"
        db_user = parsed.username or "postgres"
        db_password = parsed.password or "postgres"
    else:
        db_host = os.environ.get("DB_HOST", "127.0.0.1")
        db_port = os.environ.get("DB_PORT", "5432")
        db_name = os.environ.get("DB_NAME", "postgres")
        db_user = os.environ.get("DB_USER", "postgres")
        db_password = os.environ.get("DB_PASSWORD", "postgres")
        raw_dsn = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

    owner_uid = "11111111-1111-4111-8111-111111111111"
    synth_byok_key = "synth-pgcrypto-symmetric-key-2026-oc5"

    allowlist_env = bse.build_archive_subprocess_env(
        base_env={
            **_PoisonEnvDict(),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DB_HOST": db_host,
            "DB_PORT": db_port,
            "DB_NAME": db_name,
            "DB_USER": db_user,
            "DB_PASSWORD": db_password,
            "BYOK_ENCRYPTION_KEY": synth_byok_key,
            "AUTH_USER_ID": owner_uid,
            "DALTON_PM_USER_IDS": owner_uid,
            "DALTON_ENABLED": "true",
            "DEEPSEEK_API_KEY": "synth_deepseek_key",
            "DALTON_BRIDGE_ADMIN_TOKEN": "synth_admin_token",
        },
        explicit_model="deepseek-flash",
        validate_required=True,
    )
    for k in list(os.environ.keys()):
        if not k.startswith("PYTEST_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in allowlist_env.items():
        monkeypatch.setenv(k, v)

    conn = await psycopg.AsyncConnection.connect(raw_dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(
            """
            CREATE TEMP TABLE users (
                user_id VARCHAR(255) PRIMARY KEY,
                byok_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            CREATE TEMP TABLE user_api_keys (
                user_id VARCHAR(255) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                api_key BYTEA NOT NULL,
                base_url TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, provider)
            );
            CREATE TEMP TABLE user_oauth_tokens (
                user_id VARCHAR(255) NOT NULL,
                provider VARCHAR(64) NOT NULL,
                access_token BYTEA NOT NULL,
                refresh_token BYTEA NOT NULL,
                account_id VARCHAR(255) NOT NULL DEFAULT '',
                email VARCHAR(255),
                plan_type VARCHAR(64),
                expires_at TIMESTAMPTZ,
                needs_reauth_at TIMESTAMPTZ,
                last_error_code VARCHAR(128),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (user_id, provider)
            );
            """
        )
        await conn.execute(
            "INSERT INTO users (user_id, byok_enabled) VALUES (%s, TRUE)",
            (owner_uid,),
        )

        @asynccontextmanager
        async def _temp_db_conn(existing_conn: Any = None):
            yield existing_conn or conn

        async def _noop_invalidate(_uid: str) -> None:
            return None

        monkeypatch.setattr(db_api_keys, "get_db_connection", _temp_db_conn)
        monkeypatch.setattr(db_oauth_tokens, "get_db_connection", _temp_db_conn)
        monkeypatch.setattr(
            db_oauth_tokens, "invalidate_oauth_active_cache", _noop_invalidate
        )

        await db_api_keys.upsert_api_key(
            owner_uid,
            "deepseek",
            "sk-synth-pgcrypto-byok-secret",
            base_url="https://api.deepseek.com/v1",
            conn=conn,
        )
        decrypted_key = await db_api_keys.get_key_for_provider(owner_uid, "deepseek")
        assert decrypted_key == "sk-synth-pgcrypto-byok-secret"

        await db_oauth_tokens.upsert_oauth_tokens(
            user_id=owner_uid,
            provider="xai-oauth",
            access_token="xai-access-synth-pgcrypto",
            refresh_token="xai-refresh-synth-pgcrypto",
            account_id="acct-synth-1",
            email="pm@dalton.test",
            conn=conn,
        )
        oauth_row = await db_oauth_tokens.get_oauth_tokens(owner_uid, "xai-oauth")
        assert oauth_row is not None
        assert oauth_row["access_token"] == "xai-access-synth-pgcrypto"
        assert oauth_row["refresh_token"] == "xai-refresh-synth-pgcrypto"

        # Wrong BYOK_ENCRYPTION_KEY fails pgp_sym_decrypt
        monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "wrong-pgcrypto-key-must-fail")
        with pytest.raises(psycopg.Error):
            await db_api_keys.get_key_for_provider(owner_uid, "deepseek")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_feishu_resource_apis_with_dedicated_credentials_and_mock_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify existing `/feishu/*` resource APIs with dedicated Bridge credentials and mocked
    Feishu OpenAPI HTTP responses:
    - Valid `tenant_access_token` acquisition
    - Auth error (`99991663`), permission denied (`HTTP 403`), and non-zero business `code`
    - Doc read, doc create + markdown inline image upload + personal/group permission grant,
      doc grant (`handle_feishu_doc_grant`), doc append
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
    cfg.wiki_ingest_audit_file = tmp_path / "var" / "dalton" / "wiki_ingest_audit.jsonl"
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
        req_body = req.data.decode("utf-8", errors="ignore") if getattr(req, "data", None) else ""
        recorded_calls.append({"method": method, "url": url, "body": req_body})

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
            if "doccn_created_1" in url:
                return _FakeUrllibResponse(
                    json.dumps({"code": 131006, "msg": "not a wiki node"}).encode("utf-8"),
                    200,
                )
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
    code_bot, body_bot = await bridge.handle_feishu_bot_info({})
    assert code_bot == 200 and body_bot["ok"] is True
    assert body_bot["app_name"] == "Dalton Research Bot"

    code_chats, body_chats = await bridge.handle_feishu_chats({})
    assert code_chats == 200 and body_chats["ok"] is True
    assert body_chats["chats"][0]["chat_id"] == "oc_dedicated_team_group"

    # 2. Doc read & Wiki spaces/nodes/node read
    code_read, body_read = await bridge.handle_feishu_doc_read(
        {"token": "https://example.feishu.cn/docx/doccn_1"}
    )
    assert code_read == 200 and body_read["ok"] is True
    assert body_read["content"] == "Document body content"

    code_sp, body_sp = await bridge.handle_feishu_wiki_nodes({})
    assert code_sp == 200 and body_sp["spaces"][0]["space_id"] == "sp_res"

    code_nd, body_nd = await bridge.handle_feishu_wiki_nodes({"space_id": "sp_res"})
    assert code_nd == 200 and body_nd["nodes"][0]["node_token"] == "wikinode_1"

    code_wn, body_wn = await bridge.handle_feishu_wiki_node({"token": "wikinode_1"})
    assert code_wn == 200 and body_wn["ok"] is True
    assert body_wn["node"]["node_token"] == "wikinode_1"
    assert body_wn["node"]["obj_token"] == "doccn_1"
    assert body_wn["node"]["title"] == "Wiki Doc Title"

    code_wn_read, body_wn_read = await bridge.handle_feishu_doc_read(
        {"token": "wikinode_1", "document_id": body_wn["node"]["obj_token"]}
    )
    assert code_wn_read == 200 and body_wn_read["ok"] is True
    assert body_wn_read["content"] == "Document body content"

    # 3. Doc create with inline image upload + personal & group permission grant +
    #    explicit doc grant (`handle_feishu_doc_grant`) + Wiki create + Doc append
    png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nsynth").decode("ascii")
    code_create, body_create = await bridge.handle_feishu_doc_create(
        {
            "title": "Created Doc",
            "markdown": "![chart](chart.png)\n",
            "grant_email": "analyst@example.com",
            "image_files": {"chart.png": {"filename": "chart.png", "content_b64": png_b64}},
        }
    )
    assert code_create == 200 and body_create["ok"] is True
    assert body_create["document_id"] == "doccn_created_1"
    assert body_create["grant_member"] == "openid:ou_analyst_1"
    assert any(
        "/permissions/doccn_created_1/members" in c["url"]
        and "oc_dedicated_team_group" in c["body"]
        for c in recorded_calls
    )
    assert body_create["images"]["bound"] == 1

    code_grant, body_grant = await bridge.handle_feishu_doc_grant(
        {
            "token": "https://example.feishu.cn/docx/doccn_created_1",
            "email": "analyst@example.com",
        }
    )
    assert code_grant == 200 and body_grant["ok"] is True
    assert body_grant["document_id"] == "doccn_created_1"
    assert body_grant["grant_member"] == "openid:ou_analyst_1"

    code_wcreate, body_wcreate = await bridge.handle_feishu_doc_create(
        {
            "title": "Created Wiki Doc",
            "markdown": "![chart](chart.png)\n",
            "space_id": "sp_res",
            "image_files": {"chart.png": {"filename": "chart.png", "content_b64": png_b64}},
        }
    )
    assert code_wcreate == 200 and body_wcreate["ok"] is True
    assert body_wcreate["node_token"] == "wikinode_created"

    code_app, body_app = await bridge.handle_feishu_doc_append(
        {
            "token": "doccn_1",
            "markdown": "![chart](chart.png)",
            "image_files": {"chart.png": {"filename": "chart.png", "content_b64": png_b64}},
        }
    )
    assert code_app == 200 and body_app["ok"] is True

    # 4. Permission error (HTTP 403), business code != 0, and auth error
    mode_state["mode"] = "perm_403"
    code_403, body_403 = await bridge.handle_feishu_doc_read({"token": "doccn_1"})
    assert code_403 == 502 and body_403["ok"] is False
    assert body_403["error"] == "feishu_http_403"
    assert "1770032" in body_403["detail"]

    mode_state["mode"] = "biz_nonzero"
    code_biz, body_biz = await bridge.handle_feishu_doc_read({"token": "doccn_1"})
    assert code_biz == 502 and body_biz["ok"] is False
    assert body_biz["error"] == "feishu_api_error"
    assert "1770002" in body_biz["detail"]

    bridge._feishu_client_instance = None
    mode_state["mode"] = "auth_error"
    code_auth, body_auth = await bridge.handle_feishu_doc_read({"token": "doccn_1"})
    assert code_auth == 502 and body_auth["ok"] is False
    assert body_auth["error"] in ("feishu_api_error", "feishu_auth_failed")
    assert "99991663" in body_auth["detail"]
    assert "sec_dedicated_test_secret" not in json.dumps(body_auth)
    assert "openclaw.json" not in describe_feishu_error("feishu_not_configured")
