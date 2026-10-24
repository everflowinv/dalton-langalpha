"""Contract and behavior tests for researchdata bridge callers, watch probes, and nightly delta collect."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from scripts.dalton.researchdata_bridge import (
    PARAM_ALIAS_TABLE,
    ROUTE_CONTRACT_SPECS,
    _normalize_and_validate_call_args,
)
from src.dalton.autonomy.probes import COMPANY_PROBES
from src.dalton.autonomy.probes.cnhk import build_cnhk_filings_request
from src.dalton.data_map.health import build_route_probe_spec

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


# Every file under src/dalton, plugins/dalton/**/scripts, scripts/dalton that constructs
# a JSON-RPC `"method": "tools/call"` request must either be covered by contract assertions
# below or listed here with an explicit reason.
COVERED_BRIDGE_CALLER_FILES: set[str] = {
    "src/dalton/autonomy/probes/cnhk.py",
    "plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py",
    "src/dalton/data_map/health.py",
}

DOCUMENTED_TOOLS_CALL_EXCEPTIONS: dict[str, str] = {
    "scripts/dalton/researchdata_bridge.py": (
        "Bridge server internal stdio transport forwarding (_call_alphaengine_stdio_direct, "
        "_call_guidepoint_stdio_direct, _forward_stdio_rpc) to upstream MCP subprocesses, "
        "not an external caller of the bridge route contract."
    ),
    "scripts/dalton/realrun/check_env.py": (
        "Environment readiness check calling catalog(action='list'), which is the catalog "
        "listing tool rather than a route-dispatched research/finance tool."
    ),
    "scripts/dalton/realrun/g7_bridge.py": (
        "One-off G7 live acceptance evidence script exercising sec:financials and company-wiki:search."
    ),
    "scripts/dalton/realrun/g4_autonomy.py": (
        "Historical G4 real-run evidence script (_check_live_internal_literature_today), not production runtime."
    ),
}


def _accepted_param_and_alias_names(route: str, spec: dict[str, Any]) -> set[str]:
    accepted = {"route", "operation"} | set(spec.get("accepted_params") or [])
    nested_accepted = set(spec.get("accepted_nested_params") or [])
    for scope in ("*", route):
        alias_map = PARAM_ALIAS_TABLE.get(scope) or {}
        for alias_name, target in alias_map.items():
            if target.startswith("params."):
                if target.split(".", 1)[1] in nested_accepted:
                    accepted.add(alias_name)
            elif target in accepted:
                accepted.add(alias_name)
    return accepted


def assert_valid_bridge_call(tool_name: str, arguments: dict[str, Any]) -> None:
    """Assert that a bridge tool call conforms to ROUTE_CONTRACT_SPECS and passes bridge validation."""
    assert isinstance(arguments, dict), f"arguments must be a dict, got {type(arguments)}"
    route = arguments.get("route")
    assert isinstance(route, str) and route in ROUTE_CONTRACT_SPECS, (
        f"Unknown or missing route {route!r}; must exist in ROUTE_CONTRACT_SPECS"
    )
    spec = ROUTE_CONTRACT_SPECS[route]

    # 1. Tool name matches route spec
    assert tool_name == spec["tool"], (
        f"Route {route!r} expects tool {spec['tool']!r}, got {tool_name!r}"
    )

    # 2. Operation is supported by this route (canonical operations or operation_aliases)
    op = arguments.get("operation") or arguments.get("action") or spec.get("default_operation")
    supported_ops = set(spec.get("operations") or []) | set((spec.get("operation_aliases") or {}).keys())
    assert op in supported_ops, (
        f"Route {route!r} does not support operation {op!r}; supported={sorted(supported_ops)}"
    )

    # 3. Every top-level parameter is in accepted_params or a registered schema alias for this route
    allowed_top = _accepted_param_and_alias_names(route, spec)
    unknown_top = set(arguments.keys()) - allowed_top
    assert not unknown_top, (
        f"Route {route!r} received unsupported top-level params {sorted(unknown_top)}; "
        f"allowed={sorted(allowed_top)}"
    )

    # 4. Every nested `params` key is in accepted_nested_params
    nested = arguments.get("params")
    if nested is not None:
        assert isinstance(nested, dict), f"Route {route!r} nested 'params' must be a dict"
        allowed_nested = set(spec.get("accepted_nested_params") or [])
        unknown_nested = set(nested.keys()) - allowed_nested
        assert not unknown_nested, (
            f"Route {route!r} received unsupported nested params {sorted(unknown_nested)}; "
            f"allowed={sorted(allowed_nested)}"
        )

    # 5. Bridge's own _normalize_and_validate_call_args accepts the call without error
    _, _, validation_err = _normalize_and_validate_call_args(tool_name, dict(arguments))
    assert validation_err is None, (
        f"Bridge _normalize_and_validate_call_args rejected ({tool_name}, {arguments}): {validation_err}"
    )


def test_watch_probes_exclude_internal() -> None:
    """Verify watch COMPANY_PROBES does not contain 'internal' and internal.py is removed."""
    assert "internal" not in COMPANY_PROBES
    assert COMPANY_PROBES == ("sec", "cnhk", "agenda", "earnings")
    assert not (REPO_ROOT / "src" / "dalton" / "autonomy" / "probes" / "internal.py").exists()
    assert importlib.util.find_spec("src.dalton.autonomy.probes.internal") is None


def test_all_tools_call_sites_covered_or_explicitly_documented() -> None:
    """Ensure every Python file constructing `'method': 'tools/call'` in dalton directories is covered or documented."""
    scan_roots = [
        REPO_ROOT / "src" / "dalton",
        REPO_ROOT / "plugins" / "dalton",
        REPO_ROOT / "scripts" / "dalton",
    ]
    found_files: set[str] = set()
    for root in scan_roots:
        for py_file in root.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8", errors="replace")
            if '"method": "tools/call"' in text or "'method': 'tools/call'" in text:
                found_files.add(str(py_file.relative_to(REPO_ROOT)))

    accounted_for = COVERED_BRIDGE_CALLER_FILES | set(DOCUMENTED_TOOLS_CALL_EXCEPTIONS.keys())
    unaccounted = found_files - accounted_for
    assert not unaccounted, (
        f"Found uncovered 'tools/call' sites not in COVERED_BRIDGE_CALLER_FILES or "
        f"DOCUMENTED_TOOLS_CALL_EXCEPTIONS: {sorted(unaccounted)}"
    )
    for rel_path in COVERED_BRIDGE_CALLER_FILES:
        assert rel_path in found_files, f"Expected covered caller file {rel_path} to contain tools/call"
    for rel_path, reason in DOCUMENTED_TOOLS_CALL_EXCEPTIONS.items():
        assert reason.strip(), f"Exception {rel_path} must document a non-empty reason"


def test_cnhk_probe_request_matches_bridge_route_contract() -> None:
    """Verify probes/cnhk.py request construction conforms to ROUTE_CONTRACT_SPECS['cn-hk']."""
    tool_name, arguments = build_cnhk_filings_request(
        ticker="00700",
        start="2026-10-01",
        end="2026-10-24",
        limit=50,
    )
    assert_valid_bridge_call(tool_name, arguments)


def test_collect_six_internal_sources_and_sec_requests_match_bridge_route_contract() -> None:
    """Verify collect.py builds contract-compliant requests for all 6 internal sources and SEC filings."""
    collect_mod = _load_collect_module()

    expected_six = ["company-wiki", "guidepoint", "funda-ai", "sales-notes", "tmtb", "alphaengine"]
    assert list(collect_mod.ALL_INTERNAL_SOURCES) == expected_six
    assert set(collect_mod.INTERNAL_ROUTES) == {
        "company-wiki",
        "guidepoint",
        "funda-ai",
        "sales-notes",
        "tmtb",
    }

    meta = {
        "ticker": "META",
        "name": "Meta Platforms",
        "aliases": ["META", "Meta Platforms", "Facebook"],
        "sec_forms": list(collect_mod.DEFAULT_SEC_FORMS),
    }
    since = "2026-10-21"
    until = "2026-10-24"

    for route in collect_mod.ALL_INTERNAL_SOURCES:
        tool_name, req_args = collect_mod.build_internal_route_request(route, meta, since, until)
        assert req_args["route"] == route
        assert_valid_bridge_call(tool_name, req_args)

    # Guidepoint specific contract checks: company name + ticker in query, time window start/end
    gp_tool, gp_args = collect_mod.build_internal_route_request("guidepoint", meta, since, until)
    assert gp_tool == "research"
    assert gp_args["operation"] == "search_library"
    assert "META" in gp_args["query"] and "Meta Platforms" in gp_args["query"]
    assert gp_args["start"] == since
    assert gp_args["end"] == until
    assert "ticker" not in gp_args

    # SEC filings requests
    for form in collect_mod.DEFAULT_SEC_FORMS:
        sec_tool, sec_args = collect_mod.build_sec_filing_request("META", form, limit=5)
        assert_valid_bridge_call(sec_tool, sec_args)

    # Data map health probe specs for all routes in ROUTE_CONTRACT_SPECS
    for route_id, spec in ROUTE_CONTRACT_SPECS.items():
        probe_tool, probe_args = build_route_probe_spec(route_id, spec["tool"])
        assert_valid_bridge_call(probe_tool, probe_args)

    # Guard verification: invalid parameter names or nested params fail immediately
    with pytest.raises(AssertionError):
        assert_valid_bridge_call(
            "research",
            {
                "route": "alphaengine",
                "operation": "search_library",
                "query": "Meta Platforms",
                "params": {"sort": "relevance", "document_categories": ["sell_side_report"]},
            },
        )


def test_collect_scans_all_six_internal_sources_and_normalizes_guidepoint_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify collect_internal_and_ae queries all 6 internal sources and formats guidepoint:<id>."""
    collect_mod = _load_collect_module()

    called_routes: list[str] = []

    def _fake_bridge(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
        assert_valid_bridge_call(tool_name, arguments)
        route = arguments["route"]
        called_routes.append(route)
        if route == "company-wiki":
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "ok": True,
                    "status": "ok",
                    "results": [
                        {
                            "document_id": "wiki:meta-ai-2026",
                            "company": "META",
                            "title": "Meta AI Capex Notes",
                            "date": "2026-10-23",
                        }
                    ],
                },
            }
        if route == "guidepoint":
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "ok": True,
                    "status": "ok",
                    "data": {
                        "structuredContent": {
                            "data": [
                                {
                                    "id": "library_ts:gp-1001",
                                    "transcript_name": "Former Meta Reels Ads Director",
                                    "date": "2026-10-23",
                                    "snippet": "CPM grew 12% YoY.",
                                },
                                {
                                    "id": "gp-old-999",
                                    "transcript_name": "Out of window transcript",
                                    "date": "2026-09-01",
                                    "snippet": "Should be filtered out by date window.",
                                },
                            ]
                        }
                    },
                },
            }
        if route == "alphaengine":
            return {
                "ok": True,
                "status": "ok",
                "data": {
                    "ok": True,
                    "status": "ok",
                    "results": [
                        {
                            "doc_id": "ae-501",
                            "title": "Meta Q3 Preview",
                            "publish_time": "2026-10-22",
                            "snippet": "Raising FY26 EPS.",
                        }
                    ],
                },
            }
        return {
            "ok": True,
            "status": "ok",
            "data": {
                "ok": True,
                "status": "ok",
                "results": [
                    {
                        "document_id": f"{route}:doc-1",
                        "title": f"{route} note on META",
                        "date": "2026-10-22",
                        "snippet": "META ad demand check",
                    }
                ],
            },
        }

    mock_bridge = create_autospec(collect_mod.call_bridge_jsonrpc, side_effect=_fake_bridge)
    monkeypatch.setattr(collect_mod, "call_bridge_jsonrpc", mock_bridge)

    meta = {
        "ticker": "META",
        "name": "Meta Platforms",
        "aliases": ["META", "Meta Platforms"],
        "sec_forms": ["8-K"],
    }
    internal_data, ae_data = collect_mod.collect_internal_and_ae(
        meta=meta,
        since="2026-10-21",
        until="2026-10-24",
        seen=set(),
        skip_ae=False,
    )

    assert set(called_routes) == {
        "company-wiki",
        "guidepoint",
        "funda-ai",
        "sales-notes",
        "tmtb",
        "alphaengine",
    }
    assert set(internal_data["status"].keys()) == {
        "company-wiki",
        "guidepoint",
        "funda-ai",
        "sales-notes",
        "tmtb",
        "alphaengine",
    }
    assert all(st["ok"] is True for st in internal_data["status"].values())
    assert "guidepoint:gp-1001" in internal_data["new"]
    assert "guidepoint:gp-old-999" not in internal_data["new"]
    assert internal_data["new"]["guidepoint:gp-1001"]["route"] == "guidepoint"
    assert "alphaengine:ae-501" in ae_data["new"]


def test_collect_source_failure_never_short_circuits_and_surfaces_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify that when any source returns ok:false, short-circuit is blocked and failure is surfaced in outputs."""
    collect_mod = _load_collect_module()

    def _fake_bridge_with_gp_failure(
        tool_name: str,
        arguments: dict[str, Any],
        timeout: int = 120,
    ) -> dict[str, Any]:
        route = arguments.get("route")
        if route == "guidepoint":
            return {
                "ok": False,
                "status": "error",
                "error": "[参数错误] guidepoint 不支持 --ticker",
                "data": {"ok": False, "status": "error", "error": "[参数错误] guidepoint 不支持 --ticker"},
            }
        if tool_name == "finance":
            return {"ok": True, "status": "ok", "data": {"ok": True, "rows": []}}
        return {"ok": True, "status": "ok", "data": {"ok": True, "status": "ok", "results": []}}

    mock_bridge = create_autospec(
        collect_mod.call_bridge_jsonrpc,
        side_effect=_fake_bridge_with_gp_failure,
    )
    mock_consensus = create_autospec(
        collect_mod.fetch_consensus_snapshot,
        return_value={
            "ticker": "META",
            "fetched_at": "2026-10-24T21:30:00-04:00",
            "earnings_estimate": {},
            "revenue_estimate": {},
            "eps_trend": {},
            "eps_revisions": {},
            "price_targets": {},
            "price": {},
        },
    )
    monkeypatch.setattr(collect_mod, "call_bridge_jsonrpc", mock_bridge)
    monkeypatch.setattr(collect_mod, "fetch_consensus_snapshot", mock_consensus)

    rc = collect_mod.cmd_collect(
        argparse.Namespace(
            ticker="META",
            workspace_dir=str(tmp_path),
            since="2026-10-21",
            run_id="20261024-2130",
            no_ae=False,
            no_web=True,
            json=True,
        )
    )
    assert rc == 0
    stdout_summary = json.loads(capsys.readouterr().out)

    # All new document counts are 0 and auto_flags is empty, yet guidepoint failed:
    assert stdout_summary["counts"] == {"internal": 0, "alphaengine": 0, "sec": 0, "x": 0}
    assert stdout_summary["auto_flags"] == []
    assert stdout_summary["internal_status"]["guidepoint"]["ok"] is False
    assert stdout_summary["all_sources_ok"] is False
    assert stdout_summary["short_circuit_eligible"] is False
    assert any(
        f["source"] == "guidepoint" and "guidepoint" in f["error"]
        for f in stdout_summary["failed_sources"]
    )

    pkt_md = (tmp_path / stdout_summary["packet_md"]).read_text(encoding="utf-8")
    assert "guidepoint 本轮取数失败" in pkt_md
    assert "（`short_circuit_eligible`）**：`false`" in pkt_md
