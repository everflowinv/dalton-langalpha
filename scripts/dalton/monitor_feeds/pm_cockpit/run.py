#!/usr/bin/env python3
"""Unified CLI entrypoint for PM L1/L2 monitor data job (collect + compute + render)."""

from __future__ import annotations

from datetime import datetime
import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.monitor_feeds.common import (  # noqa: E402
    CN_TZ,
    PM_REQUIRED_L1_SOURCES,
    PM_REQUIRED_L2_MARKET_SOURCES,
    SourceStatus,
    SourceStatusStore,
    format_source_statuses,
    make_source_result,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.pm_cockpit.collect import collect_all  # noqa: E402
from scripts.dalton.monitor_feeds.pm_cockpit.compute import compute_panels  # noqa: E402
from scripts.dalton.monitor_feeds.pm_cockpit.render import render_pm_panels  # noqa: E402


def _pm_publish_ok(
    *,
    mode: str,
    sources: dict[str, dict[str, Any]],
    compute_ok: bool,
    render_ok: bool,
) -> bool:
    """Exit 0 iff render+compute succeed AND L1 + >=1 L2 market succeeded (for mode=='all')."""
    if not (compute_ok and render_ok):
        return False

    def _clean(name: str) -> bool:
        ent = sources.get(name) or {}
        return bool(ent.get("ok")) and (not bool(ent.get("stale")))

    if mode == "us":
        return all(_clean(k) for k in PM_REQUIRED_L1_SOURCES)
    if mode == "cn":
        return _clean("cn")
    if mode == "cnval":
        return _clean("cn_valuation")
    l1_ok = all(_clean(k) for k in PM_REQUIRED_L1_SOURCES)
    l2_ok = any(_clean(k) for k in PM_REQUIRED_L2_MARKET_SOURCES)
    return l1_ok and l2_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PM L1/L2 collect, compute, and HTML render.")
    parser.add_argument("--mode", default="all", choices=("all", "us", "cn", "cnval"))
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    st_dir = resolve_state_dir("pm_cockpit", args.state_dir)
    status_store = SourceStatusStore(st_dir)
    now_stamp = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S") + " 北京时间"

    collect_meta: dict[str, Any] | None = None
    collect_statuses: list[SourceStatus] = []
    req_collect: list[str] = (
        ["us", "cn", "cn_valuation"]
        if args.mode == "all"
        else (
            ["us"]
            if args.mode == "us"
            else (["cn", "cn_valuation"] if args.mode == "cn" else ["cn_valuation"])
        )
    )

    if not args.skip_collect:
        try:
            collect_meta = collect_all(
                which=args.mode,
                state_dir=st_dir,
                progress_fn=lambda msg: print(msg, file=sys.stderr, flush=True),
                commit_status=False,
            )
            pending_raw = collect_meta.get("_pending_statuses") or []
            collect_statuses = [
                SourceStatus.from_dict(item)
                for item in pending_raw
                if isinstance(item, dict) and item.get("name")
            ]
        except Exception as exc:
            err_msg = f"采集阶段异常: {exc}"
            collect_statuses = [
                make_source_result(name, ok=False, stale=True, reason=err_msg, label=name)
                for name in req_collect
            ]
            collect_meta = {"ok": False, "stale": True, "error": err_msg}
    else:
        prior_sources = status_store.load().get("sources") or {}
        collect_statuses = [
            SourceStatus.from_dict(prior_sources[name])
            for name in req_collect
            if isinstance(prior_sources.get(name), dict)
        ]

    required_all = list(dict.fromkeys(req_collect + ["pm_compute", "pm_render"]))
    preview_collect = status_store.preview_sources(
        collect_statuses, required=req_collect or None, now_str=now_stamp
    )

    print("[pm_cockpit] computing panels...", file=sys.stderr, flush=True)
    panels: dict[str, Any] | None = None
    try:
        panels = compute_panels(state_dir=st_dir, sources_override=preview_collect["sources"])
        comp_st = make_source_result("pm_compute", ok=True, stale=False, as_of=now_stamp, label="PM计算")
    except Exception as exc:
        comp_st = make_source_result("pm_compute", ok=False, stale=True, reason=str(exc), label="PM计算")
        rend_st = make_source_result(
            "pm_render", ok=False, stale=True, reason="前置计算失败，跳过渲染", label="PM渲染"
        )
        final_doc = status_store.update_sources(
            collect_statuses + [comp_st, rend_st], required=required_all, now_str=now_stamp
        )
        summary = {
            "ok": False,
            "publish_ok": False,
            "error": str(exc),
            "last_success_at": final_doc["overall"].get("last_success_at"),
            "sources": final_doc["sources"],
            "overall": final_doc["overall"],
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 1

    # Compute prospective overall status (including pm_compute + pm_render) in memory
    # BEFORE writing HTML, and ONLY commit status.json once render actually succeeds!
    render_ok_placeholder = make_source_result(
        "pm_render", ok=True, stale=False, as_of=now_stamp, label="PM渲染"
    )
    prospective_doc = status_store.preview_sources(
        collect_statuses + [comp_st, render_ok_placeholder],
        required=required_all,
        now_str=now_stamp,
    )
    panels["sources"] = prospective_doc["sources"]
    panels["overall"] = prospective_doc["overall"]
    if isinstance(panels.get("us_status"), dict):
        us_src = prospective_doc["sources"].get("us") or {}
        if us_src:
            panels["us_status"]["last_success_at"] = (
                us_src.get("last_success_at") or panels["us_status"].get("last_success_at")
            )
            us_banner = format_source_statuses(prospective_doc["sources"], only=("us",))
            if us_banner:
                panels["us_status"]["stale"] = True
                panels["us_status"]["reason"] = us_banner
    if isinstance(panels.get("cn_status"), dict):
        cn_src = prospective_doc["sources"].get("cn") or {}
        if cn_src:
            panels["cn_status"]["last_success_at"] = (
                cn_src.get("last_success_at") or panels["cn_status"].get("last_success_at")
            )
            cn_banner = format_source_statuses(
                prospective_doc["sources"], only=("cn", "cn_valuation")
            )
            if cn_banner:
                panels["cn_status"]["stale"] = True
                panels["cn_status"]["reason"] = cn_banner

    print("[pm_cockpit] rendering HTML...", file=sys.stderr, flush=True)
    outputs: dict[str, Path] = {}
    render_exc: Exception | None = None
    try:
        outputs = render_pm_panels(state_dir=st_dir, output_dir=args.output_dir, panels=panels)
        rend_st = render_ok_placeholder
    except Exception as exc:
        render_exc = exc
        rend_st = make_source_result("pm_render", ok=False, stale=True, reason=str(exc), label="PM渲染")

    # Single atomic status.json commit after collect + compute + render all finish
    final_doc = status_store.update_sources(
        collect_statuses + [comp_st, rend_st],
        required=required_all,
        now_str=now_stamp,
    )
    overall = final_doc["overall"]
    publish_ok = _pm_publish_ok(
        mode=args.mode,
        sources=final_doc["sources"],
        compute_ok=comp_st.is_clean,
        render_ok=rend_st.is_clean,
    )
    summary = {
        "ok": bool(overall.get("ok")),
        "publish_ok": bool(publish_ok),
        "us_data_as_of": panels.get("us_data_as_of"),
        "us_stale": (panels.get("us_status") or {}).get("stale", False),
        "cn_stale": (panels.get("cn_status") or {}).get("stale", False),
        "last_success_at": overall.get("last_success_at"),
        "sources": final_doc["sources"],
        "overall": overall,
        "outputs": {k: str(v) for k, v in outputs.items()},
        "collect_meta": collect_meta,
    }
    if render_exc is not None:
        summary["error"] = str(render_exc)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if publish_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
