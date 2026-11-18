"""Pytest unit tests for Guidepoint CLI, OAuth token storage, and proxy runtime."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCH_SCRIPTS = REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "scripts"
GP_CLI = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "research_runtime"
    / "skills"
    / "guidepoint-transcript-search"
    / "scripts"
    / "gp_search.py"
)


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gp = _load_module("test_gp_search_mod", GP_CLI)


def _run_search_result(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    args = argparse.Namespace(
        query="enterprise AI adoption",
        keywords=None,
        size=1,
        recency_bias=30,
        start_date=None,
        end_date=None,
        json=True,
    )
    out = io.StringIO()
    monkeypatch.setattr(gp, "mcp_session", lambda: ({}, None))
    monkeypatch.setattr(gp, "rpc", lambda *_a, **_kw: ({"result": result}, None))
    with contextlib.redirect_stdout(out):
        status = gp.cmd_search(args)
    return status, json.loads(out.getvalue())


def test_tool_error_over_http_success_fails_and_preserves_details(monkeypatch: pytest.MonkeyPatch) -> None:
    error = {"error": {"code": "MCP_VALIDATION_INVALID_FIELD", "retryable": False, "message": "size must be <=30"}}
    status, payload = _run_search_result(
        monkeypatch,
        {
            "isError": True,
            "structuredContent": error,
            "content": [{"type": "text", "text": "size must be <=30"}],
        },
    )
    assert status == 1
    assert payload["ok"] is False
    assert payload["error"] == error


def test_structured_only_preserves_excerpt_context_and_citation(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "data": [
            {
                "id": "a",
                "answer": "text",
                "context": "surrounding passage",
                "source_attribution": {"markdown": "citation"},
            }
        ]
    }
    status, payload = _run_search_result(monkeypatch, {"structuredContent": body})
    assert status == 0
    assert payload == body


def test_text_fallback_and_valid_empty_are_successful(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"data": []}
    status, payload = _run_search_result(
        monkeypatch,
        {"content": [{"type": "text", "text": json.dumps(body)}]},
    )
    assert status == 0
    assert payload == body


def test_unknown_shape_is_not_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    status, payload = _run_search_result(
        monkeypatch,
        {"content": [{"type": "text", "text": "unexpected upstream output"}]},
    )
    assert status == 1
    assert payload["error"] == "E_UPSTREAM_SHAPE"


@pytest.mark.parametrize(
    "flags",
    [
        ("--size", "40"),
        ("--recency-bias", "recent"),
        ("--recency-bias", "0"),
    ],
)
def test_invalid_cli_ranges_fail_before_network(flags: tuple[str, str]) -> None:
    r = subprocess.run(
        [sys.executable, str(GP_CLI), "search", "--query", "test", *flags],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2
    assert "error:" in r.stderr


def test_guidepoint_proxy_and_oauth_path_resolution_and_compat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_mod = _load_module("test_guidepoint_proxy_mod", RESEARCH_SCRIPTS / "guidepoint_mcp_proxy.py")
    oauth_mod = _load_module("test_guidepoint_oauth_mod", RESEARCH_SCRIPTS / "guidepoint_mcp_oauth.py")

    for mod in (proxy_mod, oauth_mod):
        monkeypatch.delenv("GUIDEPOINT_TOKENS_FILE", raising=False)
        monkeypatch.delenv("DALTON_RESEARCH_HOME", raising=False)
        monkeypatch.delenv("DALTON_OCW_ROOT", raising=False)
        default_path = mod.resolve_token_store()
        assert default_path == (
            Path.home() / "Library" / "Application Support" / "Everflow" / "research" / "config" / "guidepoint_mcp_tokens.json"
        ).resolve()

        ocw_dir = tmp_path / "ocw"
        monkeypatch.setenv("DALTON_OCW_ROOT", str(ocw_dir))
        assert mod.resolve_token_store() == (ocw_dir / "config" / "guidepoint_mcp_tokens.json").resolve()

        research_dir = tmp_path / "dalton_research"
        monkeypatch.setenv("DALTON_RESEARCH_HOME", str(research_dir))
        assert mod.resolve_token_store() == (research_dir / "config" / "guidepoint_mcp_tokens.json").resolve()

        override_file = tmp_path / "custom" / "tokens.json"
        monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(override_file))
        assert mod.resolve_token_store() == override_file.resolve()


def test_guidepoint_proxy_atomic_save_0600_and_refresh_with_autospec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_mod = _load_module("test_guidepoint_proxy_refresh", RESEARCH_SCRIPTS / "guidepoint_mcp_proxy.py")
    token_file = tmp_path / "cfg" / "guidepoint_mcp_tokens.json"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(
        json.dumps(
            {
                "access_token": "stale_tok",
                "refresh_token": "ref_tok_1",
                "obtained_at": 0,
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("GUIDEPOINT_TOKENS_FILE", str(token_file))
    monkeypatch.setenv("GUIDEPOINT_CLIENT_ID", "cid_synth")
    monkeypatch.setenv("GUIDEPOINT_CLIENT_SECRET", "csec_synth")

    def _fake_urlopen(_req: Any, timeout: int = 30) -> Any:
        assert timeout == 30
        resp = create_autospec(http.client.HTTPResponse, instance=True)
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = json.dumps(
            {
                "access_token": "fresh_tok_2",
                "expires_in": 600,
                "token_type": "Bearer",
            }
        ).encode("utf-8")
        return resp

    monkeypatch.setattr(proxy_mod.urllib.request, "urlopen", _fake_urlopen)

    proxy_mod.load_tokens()
    tok = proxy_mod.ensure_fresh_token()
    assert tok == "fresh_tok_2"

    mode = stat.S_IMODE(token_file.stat().st_mode)
    assert mode == 0o600
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["access_token"] == "fresh_tok_2"
    assert saved["refresh_token"] == "ref_tok_1"
    # No leftover temporary files
    assert [p.name for p in token_file.parent.iterdir()] == ["guidepoint_mcp_tokens.json"]


def test_guidepoint_proxy_run_sh_resolves_env_and_python_override(tmp_path: Path) -> None:
    run_sh = RESEARCH_SCRIPTS / "guidepoint_mcp_proxy_run.sh"
    research_home = tmp_path / "research_home"
    (research_home / "config").mkdir(parents=True)
    (research_home / "config" / "guidepoint_mcp.env").write_text(
        "GUIDEPOINT_CLIENT_ID=cid_from_env_file\nGUIDEPOINT_CLIENT_SECRET=csec_from_env_file\n",
        encoding="utf-8",
    )

    # Stub python prints argv[1] and confirms env vars were exported, without printing secrets.
    stub_py = tmp_path / "stub_python3.12"
    stub_py.write_text(
        "#!/bin/sh\n"
        'printf "script=%s has_id=%s has_sec=%s\\n" "$1" '
        '"$([ -n "$GUIDEPOINT_CLIENT_ID" ] && echo 1 || echo 0)" '
        '"$([ -n "$GUIDEPOINT_CLIENT_SECRET" ] && echo 1 || echo 0)"\n',
        encoding="utf-8",
    )
    stub_py.chmod(0o755)

    res = subprocess.run(
        ["/bin/bash", str(run_sh)],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DALTON_RESEARCH_HOME": str(research_home),
            "GUIDEPOINT_PYTHON": str(stub_py),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert f"script={RESEARCH_SCRIPTS / 'guidepoint_mcp_proxy.py'}" in res.stdout
    assert "has_id=1 has_sec=1" in res.stdout

    # Legacy compat: DALTON_OCW_ROOT without DALTON_RESEARCH_HOME
    ocw_root = tmp_path / "ocw_root"
    (ocw_root / "config").mkdir(parents=True)
    (ocw_root / "config" / "guidepoint_mcp.env").write_text(
        "GUIDEPOINT_CLIENT_ID=cid_ocw\nGUIDEPOINT_CLIENT_SECRET=csec_ocw\n",
        encoding="utf-8",
    )
    res_ocw = subprocess.run(
        ["/bin/bash", str(run_sh)],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "DALTON_OCW_ROOT": str(ocw_root),
            "GUIDEPOINT_PYTHON": str(stub_py),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ocw.returncode == 0, res_ocw.stderr
    assert "has_id=1 has_sec=1" in res_ocw.stdout
