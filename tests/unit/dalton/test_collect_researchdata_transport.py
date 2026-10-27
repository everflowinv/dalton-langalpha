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


def test_wrapper_rejects_mcp_error_flag_through_the_real_settle_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """isError=true 的应答走运行时真实解包链（_settle_reply → _unwrap_mcp_content
    → _is_error_result）：解包值里没有任何错误标志，is_error 是唯一判据，
    collect 判失败并保留值里的错误文本。"""
    from src.ptc_agent.core.sandbox import mcp_client_runtime

    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    _install_wrapper_modules(monkeypatch, tmp_path)
    mcp_client = sys.modules["mcp_client"]

    # 1. 线上事故形态：isError=true, content=[], structuredContent={"message": ...}
    envelope = {
        "isError": True,
        "content": [],
        "structuredContent": {"message": "unauthorized"},
    }
    mcp_client.call_mcp_tool_with_error_flag = create_autospec(
        mcp_client_runtime.call_mcp_tool_with_error_flag,
        side_effect=lambda s, t, a: mcp_client_runtime._settle_reply(
            {"result": envelope}, s, t, a, with_flag=True
        ),
    )
    res = collect_mod.call_bridge_jsonrpc("research", {"route": "company-wiki"})
    assert res["ok"] is False
    assert res["error"] == "unauthorized"
    assert res["data"] == {"message": "unauthorized"}

    # 2. 成功形态同链路：content 文本块解包为 {"ok": true, ...}，flag 为 False
    ok_envelope = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"ok": True, "status": "ok", "results": [{"document_id": "wiki:x"}]}
                ),
            }
        ]
    }
    mcp_client.call_mcp_tool_with_error_flag.side_effect = (
        lambda s, t, a: mcp_client_runtime._settle_reply(
            {"result": ok_envelope}, s, t, a, with_flag=True
        )
    )
    res_ok = collect_mod.call_bridge_jsonrpc("research", {"route": "company-wiki"})
    assert res_ok["ok"] is True
    assert res_ok["data"]["results"][0]["document_id"] == "wiki:x"


def test_transport_prefers_the_flag_preserving_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """运行时带 call_mcp_tool_with_error_flag 时，传输路选它而不是值通道。"""
    from src.ptc_agent.core.sandbox import mcp_client_runtime

    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    _install_wrapper_modules(monkeypatch, tmp_path)
    mcp_client = sys.modules["mcp_client"]
    mcp_client.call_mcp_tool_with_error_flag = create_autospec(
        mcp_client_runtime.call_mcp_tool_with_error_flag,
        return_value=(False, {"ok": True, "status": "ok", "results": []}),
    )

    transport = collect_mod.resolve_researchdata_transport()
    assert transport["mode"] == "wrapper"
    assert transport["call"] is mcp_client.call_mcp_tool_with_error_flag


def test_wrapper_fallback_still_fails_on_value_level_error_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """旧运行时（无带标志入口）退回 _call_mcp_tool 的值：值里的非空 error /
    ok=False / error 状态仍判失败。"""
    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    _install_wrapper_modules(monkeypatch, tmp_path)
    wrapper = sys.modules["tools.researchdata"]
    wrapper._call_mcp_tool.side_effect = None

    # 解包后的 {"error": ...}（无 ok/status 字段）
    wrapper._call_mcp_tool.return_value = {"error": "rate limit exceeded: 429"}
    res_unwrapped = collect_mod.call_bridge_jsonrpc("research", {"route": "company-wiki"})
    assert res_unwrapped["ok"] is False
    assert res_unwrapped["status"] == "error"
    assert res_unwrapped["error"] == "rate limit exceeded: 429"


def test_missing_or_corrupt_workspace_config_fails_fast(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PTC_WORKSPACE_CONFIG 指向的文件缺失或 JSON 损坏：启动探测即报
    researchdata_transport_unconfigured（含原因），不再认为包装模块可用。"""
    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    _install_wrapper_modules(monkeypatch, tmp_path)

    # 1. 配置文件缺失
    missing = tmp_path / "nope" / "mcp_client_config.json"
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(missing))
    transport = collect_mod.resolve_researchdata_transport()
    assert transport["mode"] is None
    assert "无法读取工作区 MCP 配置" in transport["error"]
    assert str(missing) in transport["error"]
    assert collect_mod.call_bridge_jsonrpc("research", {"route": "tmtb"})["status"] == (
        "researchdata_transport_unconfigured"
    )

    # 2. JSON 损坏
    corrupt = tmp_path / "corrupt_config.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("PTC_WORKSPACE_CONFIG", str(corrupt))
    transport_corrupt = collect_mod.resolve_researchdata_transport()
    assert transport_corrupt["mode"] is None
    assert "工作区 MCP 配置不可解析" in transport_corrupt["error"]

    # 3. cmd_collect 启动一次性报配置错误，不进入逐来源采集
    monkeypatch.setattr(
        collect_mod, "collect_internal_and_ae", create_autospec(collect_mod.collect_internal_and_ae)
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
    assert "不可解析" in out["error"]
    collect_mod.collect_internal_and_ae.assert_not_called()


def test_wrapper_rejects_non_dict_result_as_format_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """非字典结果不默认 ok=True，按格式异常判失败。"""
    collect_mod = _load_collect_module()
    monkeypatch.delenv("DALTON_RESEARCHDATA_URL", raising=False)
    _install_wrapper_modules(monkeypatch, tmp_path)
    wrapper = sys.modules["tools.researchdata"]
    wrapper._call_mcp_tool.side_effect = None

    for bad_value in (["unexpected", "list"], 42, None, True):
        wrapper._call_mcp_tool.return_value = bad_value
        res = collect_mod.call_bridge_jsonrpc("research", {"route": "company-wiki"})
        assert res["ok"] is False
        assert res["status"] == "invalid_format"
        assert res["error"]
