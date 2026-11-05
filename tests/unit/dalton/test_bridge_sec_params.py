"""桥对 SEC 路由参数的规范化：params.document 提到顶层、显式 form 原样透传。

线上线程 d9750d0e（sec attachments 取正文时 document 放顶层参数、不能放进
params）与 1f9c1246（Gateway form defaults to 10-Q，找 8-K / Form 4 时被默认值
吞掉）。桥把嵌套的 document/form 提到顶层并把调用方显式选择的表单类型原样
传给网关；网关缺省 form 时的 10-Q 默认值在 OpenClaw 侧（researchdata_mcp /
data_gateway），桥侧的职责是让显式选择不被吞。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

_COLLECT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
_spec = importlib.util.spec_from_file_location(
    "researchdata_bridge_sec_params", _COLLECT_DIR / "researchdata_bridge.py"
)
assert _spec is not None and _spec.loader is not None
rb = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("researchdata_bridge_sec_params", rb)
_spec.loader.exec_module(rb)


def _norm(args: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    normalized, _view, err = rb._normalize_and_validate_call_args("finance", args)
    return normalized, err


def test_params_document_is_hoisted_to_top_level_for_attachments() -> None:
    """The d9750d0e workaround becomes unnecessary: params.document reaches
    the gateway as the top-level --document flag."""
    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "NVDA",
            "params": {
                "accession": "0001045810-26-000024",
                "document": "q1fy27pr.htm",
                "max_chars": 100000,
            },
        }
    )
    assert err is None, err
    assert normalized["document"] == "q1fy27pr.htm"
    assert "document" not in normalized["params"]
    assert normalized["params"]["accession"] == "0001045810-26-000024"


def test_params_doc_id_alias_is_hoisted_too() -> None:
    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "NVDA",
            "params": {"doc_id": "q1fy27pr.htm"},
        }
    )
    assert err is None, err
    assert normalized["document"] == "q1fy27pr.htm"
    assert not normalized["params"]


def test_conflicting_top_and_nested_document_is_refused() -> None:
    """A top-level document that disagrees with params.document stays a
    parameter conflict, not a silent pick."""
    _n, err = _norm(
        {
            "route": "sec",
            "operation": "attachments",
            "ticker": "NVDA",
            "document": "a.htm",
            "params": {"document": "b.htm"},
        }
    )
    assert err is not None
    assert err["error_code"] == "parameter_conflict"


def test_explicit_form_passes_through_verbatim() -> None:
    """An explicit non-10-Q form is forwarded unchanged: no case-folding, no
    rewriting, so the gateway's default never gets to answer for it."""
    for form in ("8-K", "424B5", "4", "10-K/A"):
        normalized, err = _norm(
            {
                "route": "sec",
                "operation": "list-filings",
                "ticker": "DASH",
                "form": form,
            }
        )
        assert err is None, err
        assert normalized["form"] == form


def test_form_alias_and_nested_form_hoist_to_top_level() -> None:
    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "DASH",
            "form_type": "4",
        }
    )
    assert err is None, err
    assert normalized["form"] == "4"

    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "DASH",
            "params": {"form": "4"},
        }
    )
    assert err is None, err
    assert normalized["form"] == "4"
    assert "form" not in normalized["params"]


def test_conflicting_form_values_are_refused() -> None:
    _n, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "DASH",
            "form": "8-K",
            "params": {"form_type": "4"},
        }
    )
    assert err is not None
    assert err["error_code"] == "parameter_conflict"
    assert "form" in err["error"]


def test_single_element_form_list_is_unwrapped() -> None:
    """A one-element list is a caller's slip the gateway cannot parse (it
    would answer its default form); the bridge unwraps it to the scalar."""
    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "META",
            "form": ["8-K"],
        }
    )
    assert err is None, err
    assert normalized["form"] == "8-K"


def test_multi_element_form_list_is_refused() -> None:
    _n, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "META",
            "form": ["8-K", "4"],
        }
    )
    assert err is not None
    assert err["error_code"] == "parameter_conflict"


def test_nested_single_element_form_list_is_hoisted_and_unwrapped() -> None:
    """Hoisting precedes list unwrapping: params.form=['8-K'] reaches top-level
    form as '8-K', never stringified to \"['8-K']\"."""
    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "META",
            "params": {"form": ["8-K"]},
        }
    )
    assert err is None, err
    assert normalized["form"] == "8-K"
    assert "form" not in normalized["params"]


def test_nested_multi_element_form_list_is_refused() -> None:
    """A multi-element nested form list is refused with parameter_conflict
    after hoisting, never stringified and forwarded."""
    _n, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "META",
            "params": {"form_type": ["8-K", "4"]},
        }
    )
    assert err is not None
    assert err["error_code"] == "parameter_conflict"
    assert "params.form_type" in err["error"]


def test_matching_top_scalar_and_nested_single_element_form_list_agree() -> None:
    """Top-level form='8-K' and nested params.form=['8-K'] name the same form
    and do not report a conflict."""
    normalized, err = _norm(
        {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "META",
            "form": "8-K",
            "params": {"form": ["8-K"]},
        }
    )
    assert err is None, err
    assert normalized["form"] == "8-K"
    assert "form" not in normalized["params"]


def _make_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("DALTON_ALPHAENGINE_USAGE_FILE", str(tmp_path / "alphaengine_usage.jsonl"))
    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "0")
    empty_env = tmp_path / "bridge.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(empty_env))
    dummy_py = tmp_path / "dummy_python"
    dummy_py.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("DALTON_OCW_PYTHON", str(dummy_py))
    return rb.ResearchDataBridge(rb.BridgeConfig())


@pytest.mark.asyncio
async def test_handle_tools_call_forwards_document_top_level_and_form_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the JSON-RPC the gateway receives carries document at the
    top level (not inside params) and the explicit form byte-for-byte."""
    bridge = _make_bridge(tmp_path, monkeypatch)
    forwarded: list[dict[str, Any]] = []

    async def _rpc(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        req = requests[0]
        forwarded.append(req["params"]["arguments"])
        return [
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"ok": True, "status": "ok", "rows": []}
                            ),
                        }
                    ],
                },
            }
        ]

    rpc_double = create_autospec(bridge._run_stdio_rpc, side_effect=_rpc)
    monkeypatch.setattr(bridge, "_run_stdio_rpc", rpc_double)

    resp = await bridge.handle_tools_call(
        "sec-fwd-1",
        {
            "name": "finance",
            "arguments": {
                "route": "sec",
                "operation": "attachments",
                "ticker": "NVDA",
                "form": "8-K",
                "params": {
                    "accession": "0001045810-26-000024",
                    "document": "q1fy27pr.htm",
                    "max_chars": 100000,
                },
            },
        },
    )
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is True, body
    args = forwarded[0]
    assert args["document"] == "q1fy27pr.htm"
    assert "document" not in args["params"]
    assert args["form"] == "8-K"
    assert args["params"]["max_chars"] == 100000
