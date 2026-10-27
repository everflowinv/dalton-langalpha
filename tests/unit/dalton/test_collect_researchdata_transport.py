"""collect.py 的 researchdata 调用路解析：默认走沙箱 PTC 包装模块，两条路都不可用时启动即一次性报配置错误。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COLLECT_SCRIPT_PATH = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-thesis-delta" / "scripts" / "collect.py"
)


def _load_collect_module():
    spec = importlib.util.spec_from_file_location("dalton_thesis_delta_collect", COLLECT_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_mcp_call(server_name: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    """真实 _call_mcp_tool 的同签名替身：返回 researchdata research 工具的解包结果。"""
    assert server_name == "researchdata"
    assert tool_name == "research"
    return {"ok": True, "status": "ok", "results": [{"document_id": "wiki:x", "title": "t"}]}


def _install_wrapper_modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tools_pkg = types.ModuleType("tools")
    wrapper = types.ModuleType("tools.researchdata")
    wrapper._call_mcp_tool = create_autospec(_fake_mcp_call, side_effect=_fake_mcp_call)
    tools_pkg.researchdata = wrapper
    mcp_client = types.ModuleType("mcp_client")
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.researchdata", wrapper)
    monkeypatch.setitem(sys.modules, "mcp_client", mcp_client)
    config = tmp_path / "mcp_client_config.json"
    config.write_text(
        json.dumps({"workspace_id": "ws", "servers": {"researchdata": {}}}), encoding="utf-8"
    )
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(config))


def test_no_env_var_routes_through_sandbox_wrapper_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """无 DALTON_RESEARCHDATA_URL 时默认走包装模块 tools.researchdata._call_mcp_tool。"""
    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    _install_wrapper_modules(monkeypatch, tmp_path)

    transport = collect_mod.resolve_researchdata_transport()
    assert transport["mode"] == "wrapper"
    assert callable(transport["call"])

    res = collect_mod.call_bridge_jsonrpc(
        "research", {"route": "company-wiki", "operation": "list", "limit": 1}
    )
    assert res["ok"] is True
    assert res["status"] == "ok"
    assert res["data"]["results"][0]["document_id"] == "wiki:x"
    # 包装模块按 (server, tool, arguments) 被调用，与 ExecuteCode 的调用约定一致
    wrapper = sys.modules["tools.researchdata"]
    wrapper._call_mcp_tool.assert_called_once_with(
        "researchdata", "research", {"route": "company-wiki", "operation": "list", "limit": 1}
    )


def test_both_transports_unavailable_fails_fast_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """包装模块与 HTTP 桥都不可用时，collect 启动即返回一次配置错误，不逐来源失败。"""
    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    monkeypatch.delenv("PTC_WORKSPACE_CONFIG", raising=False)
    for name in ("tools", "tools.researchdata", "mcp_client"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    # 让 from tools import researchdata 必然失败：把 import 限定在真实 sys.modules
    monkeypatch.setattr(collect_mod, "collect_internal_and_ae", create_autospec(collect_mod.collect_internal_and_ae))
    monkeypatch.setattr(collect_mod, "collect_sec_filings", create_autospec(collect_mod.collect_sec_filings))
    monkeypatch.setattr(
        collect_mod, "fetch_consensus_snapshot", create_autospec(collect_mod.fetch_consensus_snapshot)
    )

    rc = collect_mod.cmd_collect(
        argparse.Namespace(
            ticker="META",
            workspace_dir=str(tmp_path),
            since="2026-10-21",
            run_id="20261027-0310",
            no_ae=False,
            no_web=True,
            json=True,
        )
    )
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "researchdata_transport_unconfigured"
    assert out["ok"] is False
    # 未进入任何来源采集：一次性配置错误，而不是 7 个来源各失败一次
    collect_mod.collect_internal_and_ae.assert_not_called()
    collect_mod.collect_sec_filings.assert_not_called()

    per_call = collect_mod.call_bridge_jsonrpc("research", {"route": "tmtb", "operation": "search"})
    assert per_call["ok"] is False
    assert per_call["status"] == "researchdata_transport_unconfigured"


def test_env_var_still_overrides_with_http_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """设了 DALTON_RESEARCHDATA_URL 时保持走 HTTP 桥（显式覆盖）。"""
    collect_mod = _load_collect_module()
    monkeypatch.setenv("DALTON_RESEARCHDATA_URL", "http://127.0.0.1:8961/rpc")

    transport = collect_mod.resolve_researchdata_transport()
    assert transport == {"mode": "bridge", "call": "http://127.0.0.1:8961/rpc", "error": ""}
