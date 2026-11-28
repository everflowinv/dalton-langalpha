"""Unit tests for OpenClaw decoupling batch 2a (research_runtime in Dalton)."""

from __future__ import annotations

import asyncio
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
RESEARCH_RUNTIME = SCRIPTS_DALTON / "research_runtime"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import bridge_subprocess_env as bse  # noqa: E402
import researchdata_bridge as rb  # noqa: E402


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bridge_config_resolves_research_code_and_home_and_ocw_compat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))

    # 1. Default decoupled mode (DALTON_RESEARCH_CODE / DALTON_RESEARCH_HOME)
    for k in ("DALTON_RESEARCH_CODE", "DALTON_RESEARCH_HOME", "DALTON_OCW_ROOT", "DALTON_EMBED_PYTHON"):
        monkeypatch.delenv(k, raising=False)
    cfg_default = rb.BridgeConfig()
    assert cfg_default.research_code == RESEARCH_RUNTIME.resolve()
    assert cfg_default.research_home == Path.home() / "Library" / "Application Support" / "Everflow" / "research"
    assert cfg_default.mcp_script == (RESEARCH_RUNTIME / "scripts" / "researchdata_mcp.py").resolve()
    assert cfg_default.xueqiu_script == (RESEARCH_RUNTIME / "scripts" / "xueqiu_posts.py").resolve()
    assert cfg_default.feishu_send_card_script == RESEARCH_RUNTIME / "scripts" / "feishu_send_card.py"
    assert cfg_default.data_sources_json == RESEARCH_RUNTIME / "config" / "data_sources.json"
    assert cfg_default.employee_python == (
        cfg_default.research_home / "venvs" / "employee-reviews" / "bin" / "python"
    )

    # 2. Explicit DALTON_RESEARCH_CODE / DALTON_RESEARCH_HOME
    custom_code = tmp_path / "code"
    custom_home = tmp_path / "home"
    custom_embed = tmp_path / "custom_embed_py"
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(custom_code))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(custom_home))
    monkeypatch.setenv("DALTON_EMBED_PYTHON", str(custom_embed))
    cfg_custom = rb.BridgeConfig()
    assert cfg_custom.research_code == custom_code.resolve()
    assert cfg_custom.research_home == custom_home.resolve()
    assert cfg_custom.embed_python == custom_embed
    assert cfg_custom.employee_python == custom_home.resolve() / "venvs" / "employee-reviews" / "bin" / "python"

    # 3. DALTON_OCW_ROOT alone is ignored now that legacy single-root mode is retired
    legacy_ws = tmp_path / "legacy_ocw"
    monkeypatch.delenv("DALTON_RESEARCH_CODE", raising=False)
    monkeypatch.delenv("DALTON_RESEARCH_HOME", raising=False)
    monkeypatch.delenv("DALTON_EMBED_PYTHON", raising=False)
    monkeypatch.setenv("DALTON_OCW_ROOT", str(legacy_ws))
    cfg_ignored = rb.BridgeConfig()
    assert cfg_ignored.research_code == RESEARCH_RUNTIME.resolve()
    assert cfg_ignored.research_home == Path.home() / "Library" / "Application Support" / "Everflow" / "research"


def test_resolve_company_wiki_python_supports_embed_override_and_research_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "bridge.env"
    env_file.write_text("DALTON_BRIDGE_TOKEN=tok\n", encoding="utf-8")
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))

    code_dir = tmp_path / "code"
    home_dir = tmp_path / "home"
    wiki_py = home_dir / "venvs" / "company-wiki" / "bin" / "python"
    wiki_py.parent.mkdir(parents=True, exist_ok=True)
    wiki_py.write_text("#!/bin/sh\n", encoding="utf-8")
    wiki_py.chmod(0o755)

    override_py = tmp_path / "override_python"
    override_py.write_text("#!/bin/sh\n", encoding="utf-8")
    override_py.chmod(0o755)

    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(code_dir))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(home_dir))
    monkeypatch.delenv("DALTON_EMBED_PYTHON", raising=False)

    bridge = rb.ResearchDataBridge(rb.BridgeConfig())
    resolved_py, resolved_cli = bridge._resolve_company_wiki_python_and_cli()
    assert resolved_py == str(wiki_py)
    assert resolved_cli == code_dir.resolve() / "skills" / "company-wiki" / "scripts" / "company_wiki_cli.py"

    monkeypatch.setenv("DALTON_EMBED_PYTHON", str(override_py))
    bridge_override = rb.ResearchDataBridge(rb.BridgeConfig())
    resolved_override_py, _ = bridge_override._resolve_company_wiki_python_and_cli()
    assert resolved_override_py == str(override_py)


def test_feishu_send_card_prefers_env_and_falls_back_to_openclaw_json_without_printing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feishu_mod = _load_module(
        "test_feishu_send_card_mod",
        RESEARCH_RUNTIME / "scripts" / "feishu_send_card.py",
    )

    legacy_json = tmp_path / "openclaw.json"
    legacy_json.write_text(
        json.dumps(
            {
                "channels": {
                    "feishu": {
                        "appId": "legacy_app_id",
                        "appSecret": "legacy_secret_never_print",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_OPENCLAW_CONFIG", str(legacy_json))

    seen_payloads: list[dict[str, Any]] = []

    def _fake_urlopen(req: Any) -> Any:
        seen_payloads.append(json.loads(req.data.decode("utf-8")))
        resp = create_autospec(http.client.HTTPResponse, instance=True)
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = json.dumps({"code": 0, "tenant_access_token": "t-fake-1"}).encode("utf-8")
        return resp

    monkeypatch.setattr(feishu_mod.urllib.request, "urlopen", _fake_urlopen)

    # 1. Env vars win over legacy openclaw.json
    monkeypatch.setenv("FEISHU_APP_ID", "env_app_id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret_never_print")
    tok1 = feishu_mod.get_tenant_token()
    assert tok1 == "t-fake-1"
    assert seen_payloads[-1] == {"app_id": "env_app_id", "app_secret": "env_secret_never_print"}

    # 2. Fallback to openclaw.json when env vars unset
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    tok2 = feishu_mod.get_tenant_token()
    assert tok2 == "t-fake-1"
    assert seen_payloads[-1] == {"app_id": "legacy_app_id", "app_secret": "legacy_secret_never_print"}


def test_plist_template_and_render_plist_use_research_code_and_home(
    tmp_path: Path,
) -> None:
    tpl = REPO_ROOT / "scripts" / "dalton" / "launchd" / "com.dalton.researchdata-bridge.plist.template"
    bridge_env = tmp_path / "researchdata-bridge.env"
    repo_env = tmp_path / ".env"
    dest_plist = tmp_path / "com.dalton.researchdata-bridge.plist"
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    research_code = REPO_ROOT / "scripts" / "dalton" / "research_runtime"
    research_home = home_dir / "Library" / "Application Support" / "Everflow" / "research"

    meta = bse.merge_bridge_and_repo_env_files(
        bridge_env_path=bridge_env,
        repo_env_path=repo_env,
        default_port="8960",
        default_python_bin=sys.executable,
        default_research_code=str(research_code),
        default_research_home=str(research_home),
    )
    assert meta["research_code"] == str(research_code)
    assert meta["research_home"] == str(research_home)

    bse.render_plist_atomic(
        template_path=tpl,
        dest_path=dest_plist,
        bridge_env_path=bridge_env,
        repo_root=REPO_ROOT,
        home_dir=home_dir,
        logs_dir=tmp_path / "logs",
    )
    plist_text = dest_plist.read_text(encoding="utf-8")
    assert f"<key>WorkingDirectory</key>\n    <string>{research_code}</string>" in plist_text
    assert f"<key>DALTON_RESEARCH_CODE</key>\n        <string>{research_code}</string>" in plist_text
    assert f"<key>DALTON_RESEARCH_HOME</key>\n        <string>{research_home}</string>" in plist_text
    assert ".openclaw" not in plist_text


def test_check_runtime_status_and_cli_json_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "bridge.env"
    env_file.write_text(
        "DALTON_BRIDGE_TOKEN=tok_synth\nGEMINI_API_KEY=gem_synth\nEDGAR_IDENTITY=Test <t@example.com>\n",
        encoding="utf-8",
    )
    research_home = tmp_path / "research_home"

    # Populate synthetic venvs & local stores
    for skill, expected_ver in (
        ("findata-analyst", "3.12.9"),
        ("cn-hk-findata", "3.12.9"),
        ("company-wiki", "3.14.6"),
        ("market-digest", "3.13.5"),
        ("employee-reviews", "3.14.6"),
        ("consensus-estimates", "3.14.6"),
    ):
        venv_dir = research_home / "venvs" / skill
        py_bin = venv_dir / "bin" / "python"
        py_bin.parent.mkdir(parents=True, exist_ok=True)
        py_bin.symlink_to(sys.executable)
        (venv_dir / "pyvenv.cfg").write_text(f"version = {expected_ver}\n", encoding="utf-8")

    for rel_file in (
        "wiki/vectors.db",
        "skills/market-digest/vectors.db",
        "state/sales-notes/manifest.jsonl",
        "state/funda-ai/index.sqlite",
        "state/research-images/index.sqlite",
    ):
        p = research_home / rel_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub")

    for rel_dir in (
        "wiki/companies",
        "wiki/sectors",
        "skills/market-digest/output",
        "state/tmtb-alphaengine",
        "market-memory",
    ):
        (research_home / rel_dir).mkdir(parents=True, exist_ok=True)

    (research_home / "config").mkdir(parents=True, exist_ok=True)
    (research_home / "config" / "config.json").write_text('{"newsletter_email": "n@example.invalid"}\n', encoding="utf-8")
    (research_home / "config" / "eve_tracking_mirror.json").write_text(
        '{"enabled": false, "source_chat_id": "oc_test_chat", "target": "user:123"}\n',
        encoding="utf-8",
    )

    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(RESEARCH_RUNTIME))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(research_home))

    status = rb.check_runtime_status(rb.BridgeConfig())
    assert status["ok"] is True
    assert status["code_ok"] is True
    assert status["configs_ok"] is True
    assert status["venvs_ok"] is True
    assert status["stores_ok"] is True
    assert status["configs"]["bridge_env_file"]["present_keys"]["GEMINI_API_KEY"] is True
    assert "gem_synth" not in json.dumps(status)
    assert "tok_synth" not in json.dumps(status)

    # Also invoke CLI --check-runtime in subprocess (offline)
    cli_res = subprocess.run(
        [sys.executable, str(SCRIPTS_DALTON / "researchdata_bridge.py"), "--check-runtime"],
        env={
            **os.environ,
            "DALTON_BRIDGE_ENV_FILE": str(env_file),
            "DALTON_RESEARCH_CODE": str(RESEARCH_RUNTIME),
            "DALTON_RESEARCH_HOME": str(research_home),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    parsed = json.loads(cli_res.stdout)
    assert parsed["code_ok"] is True
    assert parsed["configs_ok"] is True
    assert parsed["stores_ok"] is True


def test_run_sh_scripts_do_not_source_shell_profiles_and_pin_python_versions() -> None:
    expected_pins = {
        RESEARCH_RUNTIME / "skills" / "findata-analyst" / "run.sh": ("3.12", "venvs/findata-analyst"),
        RESEARCH_RUNTIME / "skills" / "cn-hk-findata" / "run.sh": ("3.12", "venvs/cn-hk-findata"),
        RESEARCH_RUNTIME / "skills" / "company-wiki" / "run.sh": ("3.14", "venvs/company-wiki"),
        RESEARCH_RUNTIME / "skills" / "market-digest" / "run.sh": ("3.13", "venvs/market-digest"),
        RESEARCH_RUNTIME / "skills" / "employee-reviews" / "scripts" / "run.sh": ("3.14", "venvs/employee-reviews"),
    }
    for path, (ver, venv_sub) in expected_pins.items():
        text = path.read_text(encoding="utf-8")
        assert ".zshrc" not in text
        assert ".bash_profile" not in text
        assert ".zprofile" not in text
        assert "DALTON_SKILL_SHELL_RC" not in text
        assert "sort -V" not in text
        assert ver in text
        assert venv_sub in text

    ce_text = (RESEARCH_RUNTIME / "scripts" / "consensus_estimates.sh").read_text(encoding="utf-8")
    assert "pyvenv.cfg" in ce_text
    assert "python3" in ce_text
    assert "python3.14 is required" not in ce_text


@pytest.mark.asyncio
async def test_stdio_rpc_passes_gemini_only_for_library_routes_and_preserves_edgar_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / "bridge.env"
    env_file.write_text(
        "DALTON_BRIDGE_TOKEN=tok\n"
        "GEMINI_API_KEY=gem_test_key\n"
        "EDGAR_IDENTITY=Dalton Test <edgar@example.com>\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(RESEARCH_RUNTIME))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(tmp_path / "home"))

    bridge = rb.ResearchDataBridge(rb.BridgeConfig())
    captured_envs: list[dict[str, str]] = []
    kill_pg_mock = create_autospec(rb._kill_process_group)
    monkeypatch.setattr(rb, "_kill_process_group", kill_pg_mock)

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> Any:
        captured_envs.append(dict(kwargs.get("env") or {}))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.pid = 424250
        proc.returncode = 0
        proc.communicate.return_value = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode("utf-8") + b"\n",
            b"",
        )
        return proc

    monkeypatch.setattr(rb.asyncio, "create_subprocess_exec", _fake_exec)

    await bridge._run_stdio_rpc([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], 5.0)
    assert "GEMINI_API_KEY" not in captured_envs[-1]
    assert captured_envs[-1].get("EDGAR_IDENTITY") == "Dalton Test <edgar@example.com>"
    assert captured_envs[-1].get("DALTON_RESEARCH_CODE") == str(RESEARCH_RUNTIME.resolve())

    await bridge._run_stdio_rpc(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "research", "arguments": {"route": "company-wiki", "query": "NVDA"}},
            }
        ],
        5.0,
    )
    assert captured_envs[-1].get("GEMINI_API_KEY") == "gem_test_key"


def _make_installer_stubs(tmp_path: Path) -> tuple[Path, Path]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir(parents=True, exist_ok=True)
    launchctl_stub = stub_dir / "launchctl"
    launchctl_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl_stub.chmod(0o755)
    curl_stub = stub_dir / "curl"
    curl_stub.write_text("#!/bin/sh\necho '{\"ok\": true}'\nexit 0\n", encoding="utf-8")
    curl_stub.chmod(0o755)
    return launchctl_stub, curl_stub


def test_install_bridge_and_render_plist_preserve_legacy_until_explicit_switch(
    tmp_path: Path,
) -> None:
    install_sh = SCRIPTS_DALTON / "install_bridge.sh"
    launchctl_stub, curl_stub = _make_installer_stubs(tmp_path)

    home_dir = tmp_path / "home"
    config_dir = home_dir / ".config" / "dalton"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    bridge_env = config_dir / "researchdata-bridge.env"
    repo_env = tmp_path / "repo.env"
    launch_agents = home_dir / "Library" / "LaunchAgents"
    plist_dest = launch_agents / "com.dalton.researchdata-bridge.plist"
    logs_dir = home_dir / "Library" / "Logs"
    ocw_root = tmp_path / "legacy_ocw_ws"
    ocw_root.mkdir(parents=True, exist_ok=True)

    bridge_env.write_text(
        "DALTON_BRIDGE_PORT=8960\n"
        "DALTON_BRIDGE_TOKEN=tok_keep\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=adm_keep\n"
        f"DALTON_OCW_ROOT={ocw_root}\n"
        f"DALTON_OCW_PYTHON={sys.executable}\n",
        encoding="utf-8",
    )
    bridge_env.chmod(0o600)

    base_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
        "DALTON_REPO_ENV_FILE": str(repo_env),
        "DALTON_LAUNCH_AGENTS_DIR": str(launch_agents),
        "DALTON_BRIDGE_LOGS_DIR": str(logs_dir),
        "DALTON_OCW_PYTHON": sys.executable,
        "LAUNCHCTL_BIN": str(launchctl_stub),
        "CURL_BIN": str(curl_stub),
        "DALTON_BRIDGE_HEALTH_RETRIES": "2",
        "DALTON_BRIDGE_HEALTH_SLEEP_S": "0",
    }

    # Running install_bridge.sh always converges to DALTON_RESEARCH_CODE / DALTON_RESEARCH_HOME and strips DALTON_OCW_ROOT
    res_auto = subprocess.run(
        ["/bin/bash", str(install_sh)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_auto.returncode == 0, res_auto.stderr
    _, vals_auto = bse.parse_env_file_strict(bridge_env)
    assert "DALTON_OCW_ROOT" not in vals_auto
    assert vals_auto["DALTON_RESEARCH_CODE"] == str(RESEARCH_RUNTIME)
    assert vals_auto["DALTON_RESEARCH_HOME"] == str(
        home_dir / "Library" / "Application Support" / "Everflow" / "research"
    )
    plist_auto_text = plist_dest.read_text(encoding="utf-8")
    assert "DALTON_OCW_ROOT" not in plist_auto_text
    assert f"<string>{RESEARCH_RUNTIME}</string>" in plist_auto_text

    # --research-runtime=legacy is rejected cleanly
    res_leg = subprocess.run(
        ["/bin/bash", str(install_sh), "--research-runtime=legacy"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_leg.returncode != 0

    # Explicit --research-runtime=new succeeds identically
    res_new = subprocess.run(
        ["/bin/bash", str(install_sh), "--research-runtime=new"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_new.returncode == 0, res_new.stderr
    _, vals_new = bse.parse_env_file_strict(bridge_env)
    assert vals_new["DALTON_RESEARCH_CODE"] == str(RESEARCH_RUNTIME)
    assert vals_new["DALTON_RESEARCH_HOME"] == str(
        home_dir / "Library" / "Application Support" / "Everflow" / "research"
    )
    plist_new_text = plist_dest.read_text(encoding="utf-8")
    assert f"<key>DALTON_RESEARCH_CODE</key>\n        <string>{RESEARCH_RUNTIME}</string>" in plist_new_text
    assert "<key>DALTON_RESEARCH_HOME</key>" in plist_new_text


def test_research_image_ingest_tmtb_no_unbound_local_and_download_dir_compat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts_dir = str(RESEARCH_RUNTIME / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    img_mod = _load_module("test_research_images_mod", RESEARCH_RUNTIME / "scripts" / "research_images.py")
    ingest_mod = _load_module(
        "test_research_image_ingest_mod",
        RESEARCH_RUNTIME / "scripts" / "research_image_ingest.py",
    )

    research_home = tmp_path / "research_home"
    dl_dir = tmp_path / "legacy_alphaengine_downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    src_img = dl_dir / "chart.png"
    src_img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    monkeypatch.setenv("DALTON_ALPHAENGINE_DOWNLOAD_DIR", str(dl_dir))
    monkeypatch.setenv("DALTON_NODE_BIN", "/usr/bin/env")

    seen_probe_envs: list[dict[str, str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
        seen_probe_envs.append(dict(kwargs.get("env") or {}))
        assert cmd[1].endswith("scripts/alphaengine-mcp/export-image-source.mjs")
        res = create_autospec(subprocess.CompletedProcess, instance=True)
        res.returncode = 0
        res.stdout = json.dumps({"ok": True, "status": "no_inline_images", "attachment_available": True})
        res.stderr = ""
        return res

    monkeypatch.setattr(ingest_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        ingest_mod,
        "mcp",
        lambda name, args: {"local_path": str(src_img)},
    )

    store = img_mod.ImageStore(research_home, code_root=RESEARCH_RUNTIME)
    job = {"key": "k_tmtb_1", "kind": "tmtb", "doc_key": "alphaengine:doc123"}
    items = ingest_mod.discover(job, store)
    assert len(items) == 1
    assert items[0]["path"] == str(research_home / "state" / "research-images" / "originals" / "k_tmtb_1" / "chart.png")
    assert Path(items[0]["path"]).is_file()
    assert seen_probe_envs[-1]["DALTON_RESEARCH_HOME"] == str(research_home)

    # Disallowed download path outside allowed download dirs must still fail closed
    outside_img = tmp_path / "outside.png"
    outside_img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(ingest_mod, "mcp", lambda name, args: {"local_path": str(outside_img)})
    with pytest.raises(ValueError, match="unexpected_download_path"):
        ingest_mod.discover({"key": "k_tmtb_2", "kind": "tmtb", "doc_key": "alphaengine:doc456"}, store)


def test_eve_tracking_mirror_and_feishu_import_error_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("DALTON_OCW_ROOT", "DALTON_RESEARCH_CODE", "DALTON_RESEARCH_HOME"):
        monkeypatch.delenv(k, raising=False)
    mirror_home = tmp_path / "mirror_home"
    (mirror_home / "config").mkdir(parents=True, exist_ok=True)
    mirror_cfg_path = mirror_home / "config" / "eve_tracking_mirror.json"
    mirror_cfg_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "source_chat_id": "oc_test_chat_01",
                "target": "user:000111222",
                "discord_account": "default",
                "source_label": "test-mirror",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(mirror_home))
    # 1. Runtime eve_tracking_mirror reads $DALTON_RESEARCH_HOME/config/eve_tracking_mirror.json & passes offline self-test
    mirror_mod = _load_module(
        "test_eve_mirror_mod",
        RESEARCH_RUNTIME / "scripts" / "eve_tracking_mirror.py",
    )
    assert mirror_mod.CONFIG == mirror_cfg_path
    cfg = mirror_mod.load_config()
    assert cfg.get("enabled") is True
    assert mirror_mod.should_mirror(cfg["source_chat_id"], "chat_id", cfg) is True
    assert mirror_mod.self_test() == 0

    # 2. When eve_tracking_mirror fails to import, feishu_send_card & bridge report error honestly
    broken_dir = tmp_path / "broken_scripts"
    broken_dir.mkdir(parents=True, exist_ok=True)
    card_copy = broken_dir / "feishu_send_card.py"
    card_copy.write_bytes((RESEARCH_RUNTIME / "scripts" / "feishu_send_card.py").read_bytes())
    (broken_dir / "eve_tracking_mirror.py").write_text("raise ImportError('broken mirror')\n", encoding="utf-8")

    # Test feishu_send_card._mirror_to_discord directly + full subprocess via _run_feishu_send_card
    broken_card_mod = _load_module("test_broken_feishu_card_mod", card_copy)
    mirror_res = broken_card_mod._mirror_to_discord("oc_x", "chat_id", {}, "lbl")
    assert isinstance(mirror_res, dict)
    assert mirror_res["status"] == "import_error"

    # Simulate _run_feishu_send_card receiving feishu_send_card JSON with import_error
    def _fake_subproc_run(*args: Any, **kwargs: Any) -> Any:
        proc = create_autospec(subprocess.CompletedProcess, instance=True)
        proc.returncode = 1
        proc.stdout = json.dumps(
            {
                "status": "error",
                "feishu_sent": True,
                "error": "eve_tracking_mirror_import_failed",
                "discord_mirror": {"status": "import_error", "error": "ImportError: broken mirror"},
            }
        )
        proc.stderr = ""
        return proc

    monkeypatch.setattr(rb.subprocess, "run", _fake_subproc_run)
    bridge_res = rb._run_feishu_send_card(
        script_path=card_copy,
        receive_id="oc_x",
        card={"schema": "2.0", "header": {"title": {"tag": "plain_text", "content": "t"}}, "body": {"elements": []}},
        receive_id_type="chat_id",
        feishu_app_id="app",
        feishu_app_secret="sec",
    )
    assert bridge_res["ok"] is False
    assert bridge_res["feishu_sent"] is True
    assert bridge_res["error"] == "discord_mirror_import_failed"


def test_consensus_estimates_reads_legacy_pyvenv_cfg_and_falls_back_to_python3(
    tmp_path: Path,
) -> None:
    code_dir = tmp_path / "code"
    (code_dir / "scripts").mkdir(parents=True, exist_ok=True)
    ce_sh = code_dir / "scripts" / "consensus_estimates.sh"
    ce_sh.write_bytes((RESEARCH_RUNTIME / "scripts" / "consensus_estimates.sh").read_bytes())
    ce_sh.chmod(0o755)
    (code_dir / "scripts" / "consensus_estimates_requirements.txt").write_text("", encoding="utf-8")
    (code_dir / "scripts" / "consensus_estimates.py").write_text(
        "import json, sys; print(json.dumps({'ok': True, 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )

    research_home = tmp_path / "research_home"
    venv_dir = research_home / "venvs" / "consensus-estimates"
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    fake_py = venv_dir / "bin" / "python"
    fake_py.symlink_to(sys.executable)
    (venv_dir / ".deps_ok").write_text("", encoding="utf-8")

    # Case 1: legacy pyvenv.cfg present -> check_runtime_status reads it dynamically
    legacy_cfg = tmp_path / "legacy_pyvenv.cfg"
    legacy_cfg.write_text(
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    (research_home / "state" / "venvs").mkdir(parents=True, exist_ok=True)
    (research_home / "state" / "venvs" / "consensus-estimates.python_version").write_text(
        f"{sys.version_info.major}.{sys.version_info.minor}\n",
        encoding="utf-8",
    )
    (venv_dir / "pyvenv.cfg").write_text(
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    assert rb._resolve_consensus_expected_mm(research_home) == (
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )

    # Case 2: missing legacy pyvenv.cfg -> consensus_estimates.sh runs via python3 fallback cleanly
    res = subprocess.run(
        ["/bin/bash", str(ce_sh), "--ticker", "NVDA"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DALTON_RESEARCH_CODE": str(code_dir),
            "DALTON_RESEARCH_HOME": str(research_home),
            "DALTON_CONSENSUS_LEGACY_PYVENV_CFG": str(tmp_path / "nonexistent_pyvenv.cfg"),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["argv"] == ["--ticker", "NVDA"]


def test_data_sources_agent_reach_removed_and_dead_openclaw_tools_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("DALTON_OCW_ROOT", "DALTON_RESEARCH_CODE", "DALTON_RESEARCH_HOME"):
        monkeypatch.delenv(k, raising=False)
    ds = json.loads((RESEARCH_RUNTIME / "config" / "data_sources.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in ds["routes"]}

    # agent-reach route & retired skill removed
    assert "agent-reach" not in by_id
    assert not (RESEARCH_RUNTIME / "skills" / "agent-reach").exists()

    # Dead openclaw__* routes are marked disabled & hidden with tool=None
    for rid in ("web-news", "web-page", "x-search"):
        assert by_id[rid]["tool"] is None
        assert by_id[rid]["disabled"] is True
        assert by_id[rid]["hidden"] is True

    gw_mod = _load_module("test_data_gateway_mod", RESEARCH_RUNTIME / "scripts" / "data_gateway.py")
    reg_self = gw_mod.catalog(include_hidden=True)
    assert all(r["id"] != "agent-reach" for r in reg_self["routes"])


def test_build_research_runtime_subprocess_env_enforces_allowlists() -> None:
    env = bse.build_research_runtime_subprocess_env(
        {
            "PATH": "/usr/bin:/bin",
            "FEISHU_APP_SECRET": "secret_feishu",
            "OPENAI_API_KEY": "secret_openai",
            "GEMINI_API_KEY": "gem_from_base",
            "BASH_ENV": "/tmp/evil.sh",
        },
        runtime_env={
            "DALTON_RESEARCH_CODE": "/tmp/code",
            "DALTON_RESEARCH_HOME": "/tmp/home",
            "EDGAR_IDENTITY": "Dalton <a@b.com>",
            "GEMINI_API_KEY": "gem_allowed",
            "OPENAI_API_KEY": "must_not_pass",
            "DALTON_BRIDGE_TOKEN": "must_not_pass",
        },
        allow_secret_keys=("GEMINI_API_KEY", "OPENAI_API_KEY"),
    )
    assert env["DALTON_RESEARCH_CODE"] == "/tmp/code"
    assert env["DALTON_RESEARCH_HOME"] == "/tmp/home"
    assert env["EDGAR_IDENTITY"] == "Dalton <a@b.com>"
    assert env["GEMINI_API_KEY"] == "gem_allowed"
    assert "OPENAI_API_KEY" not in env
    assert "FEISHU_APP_SECRET" not in env
    assert "DALTON_BRIDGE_TOKEN" not in env
    assert "BASH_ENV" not in env


def test_legacy_mode_is_rejected_and_strips_ocw_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge_env = tmp_path / "bridge.env"
    repo_env = tmp_path / "repo.env"
    plist_path = tmp_path / "bridge.plist"
    logs_dir = tmp_path / "logs"
    home_dir = tmp_path / "home"
    tpl = REPO_ROOT / "scripts" / "dalton" / "launchd" / "com.dalton.researchdata-bridge.plist.template"

    bridge_env.write_text(
        "DALTON_BRIDGE_TOKEN=tok\n"
        "DALTON_OCW_ROOT=/tmp/old_ocw\n"
        f"DALTON_RESEARCH_CODE={RESEARCH_RUNTIME}\n"
        f"DALTON_RESEARCH_HOME={tmp_path / 'new_home'}\n",
        encoding="utf-8",
    )
    bridge_env.chmod(0o600)
    with pytest.raises(ValueError, match="unsupported research_runtime mode"):
        bse.merge_bridge_and_repo_env_files(
            bridge_env_path=bridge_env,
            repo_env_path=repo_env,
            default_python_bin=sys.executable,
            research_runtime="legacy",
        )
    with pytest.raises(ValueError, match="unsupported research_runtime mode"):
        bse.render_plist_atomic(
            template_path=tpl,
            dest_path=plist_path,
            bridge_env_path=bridge_env,
            repo_root=REPO_ROOT,
            home_dir=home_dir,
            logs_dir=logs_dir,
            research_runtime="legacy",
        )

    meta_new = bse.merge_bridge_and_repo_env_files(
        bridge_env_path=bridge_env,
        repo_env_path=repo_env,
        default_python_bin=sys.executable,
        default_research_code=str(RESEARCH_RUNTIME),
        default_research_home=str(tmp_path / "new_home"),
    )
    assert meta_new["research_runtime_mode"] == "new"
    _, merged = bse.parse_env_file_strict(bridge_env)
    assert "DALTON_OCW_ROOT" not in merged
    assert merged["DALTON_RESEARCH_CODE"] == str(RESEARCH_RUNTIME)

    bse.render_plist_atomic(
        template_path=tpl,
        dest_path=plist_path,
        bridge_env_path=bridge_env,
        repo_root=REPO_ROOT,
        home_dir=home_dir,
        logs_dir=logs_dir,
    )
    plist_raw = plist_path.read_text(encoding="utf-8")
    assert "DALTON_OCW_ROOT" not in plist_raw
    assert "DALTON_RESEARCH_CODE" in plist_raw
    assert "DALTON_RESEARCH_HOME" in plist_raw


def test_company_filings_alert_guidepoint_and_catalog_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ds = json.loads((RESEARCH_RUNTIME / "config" / "data_sources.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in ds["routes"]}
    assert (RESEARCH_RUNTIME / by_id["filings"]["skill"]).is_file()
    assert (RESEARCH_RUNTIME / by_id["guidepoint"]["skill"]).is_file()
    assert by_id["transcript"]["skill"] is None
    assert by_id["last30days"]["disabled"] is True
    assert by_id["last30days"]["hidden"] is True
    assert by_id["last30days"]["tool"] is None
    assert by_id["last30days"]["skill"] is None

    filings_md = (RESEARCH_RUNTIME / by_id["filings"]["skill"]).read_text(encoding="utf-8")
    assert "dalton-daily-filing-alert" in filings_md
    assert "openclaw cron" not in filings_md.lower()
    gp_md = (RESEARCH_RUNTIME / by_id["guidepoint"]["skill"]).read_text(encoding="utf-8")
    assert "代理服务将在下一批迁到 Dalton" in gp_md

    # edgar_enrich must error cleanly without issuing HTTP requests if EDGAR_IDENTITY is unset
    edgar_mod = _load_module(
        "test_edgar_enrich_mod",
        RESEARCH_RUNTIME / "skills" / "company-filings-alert" / "scripts" / "edgar_enrich.py",
    )
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    monkeypatch.setattr(edgar_mod, "USER_AGENT", "")
    mock_urlopen = create_autospec(edgar_mod.urllib.request.urlopen)
    monkeypatch.setattr(edgar_mod.urllib.request, "urlopen", mock_urlopen)
    with pytest.raises(edgar_mod.EnrichError, match="EDGAR_IDENTITY"):
        edgar_mod._sec_get("https://data.sec.gov/submissions/CIK0001045810.json")
    mock_urlopen.assert_not_called()

    # filings run.sh checks $DALTON_RESEARCH_HOME/venvs/company-filings-alert in new mode and legacy path in legacy mode
    import hashlib

    run_sh = RESEARCH_RUNTIME / "skills" / "company-filings-alert" / "run.sh"
    req_sha = hashlib.sha256((RESEARCH_RUNTIME / "skills" / "company-filings-alert" / "requirements.txt").read_bytes()).hexdigest()
    new_home = tmp_path / "new_home"
    new_py = new_home / "venvs" / "company-filings-alert" / "bin" / "python"
    new_py.parent.mkdir(parents=True)
    new_py.write_text("#!/bin/sh\necho new_mode_ok\n", encoding="utf-8")
    new_py.chmod(0o755)
    (new_home / "venvs" / "company-filings-alert" / ".requirements.sha256").write_text(req_sha + "\n", encoding="utf-8")
    res_new = subprocess.run(
        ["/bin/bash", str(run_sh), "--help"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DALTON_RESEARCH_CODE": str(RESEARCH_RUNTIME),
            "DALTON_RESEARCH_HOME": str(new_home),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_new.returncode == 0
    assert "new_mode_ok" in res_new.stdout

    gw_res = subprocess.run(
        [sys.executable, str(RESEARCH_RUNTIME / "scripts" / "data_gateway.py"), "self-test"],
        env={**os.environ, "DALTON_RESEARCH_CODE": str(RESEARCH_RUNTIME)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert gw_res.returncode == 0, gw_res.stderr
    st = json.loads(gw_res.stdout)
    assert st["ok"] is True


def test_personal_configs_runtime_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_root = tmp_path / "new_home"
    (home_root / "config").mkdir(parents=True)
    (home_root / "config" / "config.json").write_text(
        json.dumps({"newsletter_email": "new@example.invalid", "senders": []}),
        encoding="utf-8",
    )
    (home_root / "config" / "eve_tracking_mirror.json").write_text(
        json.dumps({"enabled": True, "source_chat_id": "oc_new", "target": "user:2"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("DALTON_RESEARCH_CODE", str(RESEARCH_RUNTIME))
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(home_root))
    md_new = _load_module("test_md_cfg_new", RESEARCH_RUNTIME / "skills" / "market-digest" / "scripts" / "market_digest_cli.py")
    mirror_new = _load_module("test_mirror_cfg_new", RESEARCH_RUNTIME / "scripts" / "eve_tracking_mirror.py")
    assert md_new.load_config()["newsletter_email"] == "new@example.invalid"
    assert mirror_new.load_config()["source_chat_id"] == "oc_new"
