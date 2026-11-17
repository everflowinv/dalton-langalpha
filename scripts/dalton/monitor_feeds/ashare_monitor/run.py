#!/usr/bin/env python3
"""Daily data-job entrypoint for A-share intraday monitor (refreshes 30d history + HTML)."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.monitor_feeds.ashare_monitor.collect import AShareCollector  # noqa: E402
from scripts.dalton.monitor_feeds.ashare_monitor.render import write_ashare_panel  # noqa: E402
from scripts.dalton.monitor_feeds.common import (  # noqa: E402
    ASHARE_REQUIRED_PUBLISH_SOURCES,
    CN_TZ,
    SourceStatus,
    SourceStatusStore,
    make_source_result,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build A-share monitor static panel.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    collector = AShareCollector(state_dir=args.state_dir)
    status_store = SourceStatusStore(collector.state_dir)
    now_str = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    snapshot: dict[str, Any] = {}
    source_results: list[SourceStatus] = []
    required_sources: list[str] = list(ASHARE_REQUIRED_PUBLISH_SOURCES)
    collect_exc: Exception | None = None

    if args.skip_collect:
        snapshot = dict(collector._last_good or {})
        prior_sources = status_store.load().get("sources") or {}
        source_results = [
            SourceStatus.from_dict(v)
            for v in prior_sources.values()
            if isinstance(v, dict) and v.get("name")
        ]
        required_sources = list(prior_sources.keys()) or list(ASHARE_REQUIRED_PUBLISH_SOURCES)
    else:
        try:
            snapshot = collector.collect_snapshot(commit_status=False)
            now_str = str(snapshot.get("as_of") or now_str)
            source_results = [
                SourceStatus.from_dict(d)
                for d in (snapshot.get("_pending_source_results") or [])
                if isinstance(d, dict) and d.get("name")
            ]
            required_sources = list(snapshot.get("_required_sources") or ASHARE_REQUIRED_PUBLISH_SOURCES)
        except Exception as exc:
            collect_exc = exc
            source_results = [
                make_source_result(name, ok=False, stale=True, reason=f"采集异常: {exc}", label=name)
                for name in ASHARE_REQUIRED_PUBLISH_SOURCES
            ]

    required_all = list(dict.fromkeys(required_sources + ["html_render"]))
    out_path_str = str(Path(args.output_dir) / "index.html")
    render_exc: Exception | None = None

    if collect_exc is not None:
        render_st = make_source_result(
            "html_render", ok=False, stale=True, reason=f"采集异常跳过渲染: {collect_exc}", label="页面渲染"
        )
    else:
        render_ok = make_source_result("html_render", ok=True, stale=False, as_of=now_str, label="页面渲染")
        prospective = status_store.preview_sources(
            source_results + [render_ok], required=required_all, now_str=now_str
        )
        snapshot["sources"] = prospective["sources"]
        snapshot["overall"] = prospective["overall"]
        try:
            out_path_str = str(write_ashare_panel(output_dir=args.output_dir, snapshot=snapshot))
            render_st = render_ok
        except Exception as exc:
            render_exc = exc
            render_st = make_source_result(
                "html_render", ok=False, stale=True, reason=f"HTML渲染失败: {exc}", label="页面渲染"
            )

    final_doc = status_store.update_sources(
        source_results + [render_st], required=required_all, now_str=now_str
    )
    overall = final_doc["overall"]
    sources = final_doc["sources"]
    publish_ok = (
        collect_exc is None
        and render_exc is None
        and all(
            bool((sources.get(k) or {}).get("ok"))
            and (not bool((sources.get(k) or {}).get("stale")))
            for k in (list(ASHARE_REQUIRED_PUBLISH_SOURCES) + ["html_render"])
        )
    )
    summary = {
        "ok": bool(overall.get("ok")),
        "publish_ok": bool(publish_ok),
        "output": out_path_str,
        "as_of": snapshot.get("as_of") or now_str,
        "stale": bool(overall.get("stale")),
        "last_success_at": overall.get("last_success_at"),
        "sources": sources,
        "overall": overall,
        "history_days": len(snapshot.get("daily_history") or []),
    }
    if collect_exc is not None:
        summary["error"] = str(collect_exc)
    elif render_exc is not None:
        summary["error"] = str(render_exc)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if publish_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
