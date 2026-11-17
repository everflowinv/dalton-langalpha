#!/usr/bin/env python3
"""Daily data-job entrypoint for HK Momentum Indices (fetch + compute + render HTML)."""

from __future__ import annotations

import argparse
from datetime import date
import json
import logging
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.monitor_feeds.common import (  # noqa: E402
    HK_MONTH_END_REQUIRED_SOURCES,
    HK_REQUIRED_PUBLISH_SOURCES,
    SourceStatus,
    SourceStatusStore,
    atomic_write_json,
    format_source_statuses,
    is_trading_day,
    last_trading_day_of_month,
    load_json_safe,
    make_source_result,
    now_hk,
    resolve_market_trade_date,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.hk_momentum.compute import (  # noqa: E402
    compute_all,
    compute_intraday_snapshot,
    persist_compute_result,
)
from scripts.dalton.monitor_feeds.hk_momentum.fetch_data import (  # noqa: E402
    build_universe,
    download_history,
)
from scripts.dalton.monitor_feeds.hk_momentum.render import write_hkmom_panel  # noqa: E402

logger = logging.getLogger("dalton.monitor_feeds.hk_momentum.run")


def _extract_universe_statuses(uni_meta: dict[str, Any]) -> list[SourceStatus]:
    raw_map = uni_meta.get("source_statuses")
    if isinstance(raw_map, dict) and raw_map:
        return [SourceStatus.from_dict(v) for v in raw_map.values() if isinstance(v, dict)]
    stale = bool(uni_meta.get("stale"))
    errs = [str(e) for e in (uni_meta.get("errors") or [])]
    reason = "；".join(errs[:3]) if stale else None
    if stale and not reason:
        reason = "港股标的或现货抓取失败"
    return [
        make_source_result("hkex_universe", ok=not stale, stale=stale, reason=reason, label="港交所证券列表"),
        make_source_result("ggt_list", ok=True, stale=False, label="港股通名单"),
        make_source_result("hk_spot", ok=not stale, stale=stale, reason=reason, label="港股现货行情"),
    ]


def _extract_history_status(hist_prog: dict[str, Any]) -> SourceStatus:
    raw = hist_prog.get("source_status")
    if isinstance(raw, dict) and raw:
        return SourceStatus.from_dict(raw)
    n_err = int(hist_prog.get("n_errors", 0) or 0)
    stale = bool(hist_prog.get("stale")) or n_err > 0
    total = int(hist_prog.get("total", 0) or 0)
    reason = f"历史日线下载失败 {n_err}/{total} 只" if stale else None
    return make_source_result("hk_history", ok=not stale, stale=stale, reason=reason, label="港股历史日线")


def _extract_compute_statuses(meta: dict[str, Any], target_str: str) -> list[SourceStatus]:
    raw_map = meta.get("source_statuses")
    if isinstance(raw_map, dict) and raw_map:
        return [SourceStatus.from_dict(v) for v in raw_map.values() if isinstance(v, dict)]
    series_stale = bool(meta.get("series_stale")) or not bool(meta.get("completeness_ok", True))
    reason = str(meta.get("series_stale_reason") or "").strip() or None
    if series_stale and not reason:
        reason = "计算结果不完整"
    hsi_bad = series_stale and ("恒生" in (reason or ""))
    comp_bad = series_stale and not hsi_bad
    return [
        make_source_result(
            "hsi_history",
            ok=not hsi_bad,
            stale=hsi_bad,
            reason=reason if hsi_bad else None,
            as_of=target_str,
            label="恒生指数历史",
        ),
        make_source_result(
            "index_compute",
            ok=not comp_bad,
            stale=comp_bad,
            reason=reason if comp_bad else None,
            as_of=target_str,
            label="动量日度计算",
        ),
    ]


def _extract_intraday_statuses(snap: dict[str, Any]) -> list[SourceStatus]:
    raw_map = snap.get("intraday_source_statuses")
    if isinstance(raw_map, dict) and raw_map:
        return [SourceStatus.from_dict(v) for v in raw_map.values() if isinstance(v, dict)]
    stale = bool(snap.get("stale"))
    reason = str(snap.get("stale_reason") or "").strip() or ("盘中快照失效" if stale else None)
    return [
        make_source_result("hk_intraday_spot", ok=not stale, stale=stale, reason=reason if stale else None, label="港股盘中现货"),
        make_source_result("hsi_spot", ok=not stale, stale=stale, reason=reason if stale else None, label="恒生指数现货"),
    ]


def _commit_failure_artifacts(
    *,
    st_dir: Path,
    output_dir: str | Path,
    status_store: SourceStatusStore,
    results: list[SourceStatus],
    required_names: list[str],
    now_str: str,
    prev_index_meta: dict[str, Any],
    is_month_end: bool,
    headline_reason: str | None = None,
) -> dict[str, Any]:
    """Commit degraded SourceStatuses without touching frozen history or advancing last_success_at."""
    status_doc = status_store.update_sources(
        results,
        required=required_names,
        now_str=now_str,
        extra_overall={"month_end_failed": is_month_end},
    )
    overall = status_doc["overall"]
    base_reason = headline_reason or overall.get("reason") or format_source_statuses(status_doc["sources"]) or "采集或计算失败"
    fail_msg = (
        base_reason
        if (not is_month_end or base_reason.startswith("本月调仓未完成"))
        else f"本月调仓未完成：{base_reason}"
    )

    fail_meta = dict(prev_index_meta)
    fail_meta.update(
        {
            "updated": now_hk().isoformat(timespec="seconds"),
            "stale": True,
            "month_end_failed": is_month_end,
            "stale_reason": fail_msg,
            "last_success_at": overall.get("last_success_at"),
            "sources": status_doc["sources"],
            "source_statuses": status_doc["sources"],
        }
    )
    atomic_write_json(st_dir / "index_meta.json", fail_meta)

    prev_live = load_json_safe(st_dir / "live.json", {}) or {}
    now_local = now_hk()
    live_err = dict(prev_live)
    live_err.update(
        {
            "as_of": now_local.strftime("%Y-%m-%d %H:%M:%S"),
            "as_of_iso": now_local.isoformat(timespec="seconds"),
            "tz": "北京时间",
            "stale": True,
            "month_end_failed": is_month_end,
            "stale_reason": fail_msg,
            "last_success_at": overall.get("last_success_at"),
            "sources": status_doc["sources"],
        }
    )
    atomic_write_json(st_dir / "live.json", live_err)

    out_path_str = str(Path(output_dir) / "index.html")
    try:
        out_path_str = str(write_hkmom_panel(state_dir=st_dir, output_dir=output_dir))
    except Exception as exc:
        logger.exception("HK momentum HTML render failed during error recovery: %s", exc)
        render_err = make_source_result(
            "html_render", ok=False, stale=True, reason=f"HTML渲染失败: {exc}", label="页面渲染"
        )
        status_doc = status_store.update_sources(
            [render_err],
            required=required_names + ["html_render"],
            now_str=now_str,
            extra_overall={"month_end_failed": is_month_end},
        )
        overall = status_doc["overall"]
        fail_meta["sources"] = status_doc["sources"]
        fail_meta["source_statuses"] = status_doc["sources"]
        fail_meta["last_success_at"] = overall.get("last_success_at")
        fail_meta["stale_reason"] = f"{fail_msg}；HTML渲染失败: {exc}"
        atomic_write_json(st_dir / "index_meta.json", fail_meta)
        live_err["sources"] = status_doc["sources"]
        live_err["last_success_at"] = overall.get("last_success_at")
        live_err["stale_reason"] = fail_meta["stale_reason"]
        atomic_write_json(st_dir / "live.json", live_err)

    return {
        "ok": False,
        "publish_ok": False,
        "froze": False,
        "output": out_path_str,
        "last_date": fail_meta.get("last_date"),
        "launch_date": fail_meta.get("launch_date"),
        "stale": True,
        "month_end_failed": is_month_end,
        "stale_reason": fail_meta["stale_reason"],
        "last_success_at": overall.get("last_success_at"),
        "sources": status_doc["sources"],
    }


def _hk_publish_ok(
    *,
    sources: dict[str, dict[str, Any]],
    is_month_end: bool,
    month_end_failed: bool,
    skip_fetch: bool,
    render_ok: bool,
) -> bool:
    """Exit 0 iff page rendered & required daily close series succeeded (all month-end sources on rebalance day)."""
    if not render_ok or month_end_failed:
        return False

    def _clean(name: str) -> bool:
        ent = sources.get(name) or {}
        return bool(ent.get("ok")) and (not bool(ent.get("stale")))

    req = (
        HK_REQUIRED_PUBLISH_SOURCES
        if (not is_month_end or skip_fetch)
        else HK_MONTH_END_REQUIRED_SOURCES
    )
    return all(_clean(k) for k in req)


def run_daily_job(
    *,
    state_dir: str | Path | None = None,
    output_dir: str | Path,
    proxy: str | None = None,
    skip_fetch: bool = False,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """Run HK momentum daily pipeline in container (writes state/HTML only; host daemon pushes)."""
    st_dir = resolve_state_dir("hk_momentum", state_dir)
    status_store = SourceStatusStore(st_dir)
    now_local = now_hk()
    ref_day = resolve_market_trade_date("hk", now=now_local, as_of_date=as_of_date)
    ref_str = ref_day.isoformat()
    now_str = f"{now_local.strftime('%Y-%m-%d %H:%M:%S')} 北京时间"
    prev_index_meta = load_json_safe(st_dir / "index_meta.json", {}) or {}
    is_month_end_rebal = (
        is_trading_day("hk", ref_day)
        and ref_day == last_trading_day_of_month("hk", ref_day.year, ref_day.month)
    )

    if skip_fetch:
        required_names = ["hsi_history", "index_compute", "html_render"]
    else:
        required_names = [
            "hkex_universe",
            "ggt_list",
            "hk_spot",
            "hk_history",
            "hsi_history",
            "index_compute",
            "hk_intraday_spot",
            "hsi_spot",
            "html_render",
        ]

    all_results: list[SourceStatus] = []
    codes: list[str] = []
    fetched_uni: Any = None

    if not skip_fetch:
        try:
            uni, uni_meta = build_universe(state_dir=st_dir, proxy=proxy, as_of_date=ref_day)
            fetched_uni = uni
            all_results.extend(_extract_universe_statuses(uni_meta))
            codes = list(uni.loc[uni["price"].notna(), "code"]) if "price" in uni else list(uni["code"])
        except Exception as exc:
            logger.exception("HK momentum build_universe raised: %s", exc)
            err_msg = f"成分池抓取异常: {exc}"
            all_results.extend(
                [
                    make_source_result("hkex_universe", ok=False, stale=True, reason=err_msg, label="港交所证券列表"),
                    make_source_result("ggt_list", ok=False, stale=True, reason=err_msg, label="港股通名单"),
                    make_source_result("hk_spot", ok=False, stale=True, reason=err_msg, label="港股现货行情"),
                ]
            )

        if codes:
            try:
                hist_prog = download_history(codes, state_dir=st_dir, target_date=ref_str)
                all_results.append(_extract_history_status(hist_prog))
            except Exception as exc:
                logger.exception("HK momentum download_history raised: %s", exc)
                all_results.append(
                    make_source_result("hk_history", ok=False, stale=True, reason=f"历史日线下载异常: {exc}", label="港股历史日线")
                )
        else:
            all_results.append(
                make_source_result("hk_history", ok=False, stale=True, reason="无可用成分代码，跳过历史日线下载", label="港股历史日线")
            )

    fetch_failed = any(not st.is_clean for st in all_results)
    if is_month_end_rebal and fetch_failed:
        reasons = [st.reason for st in all_results if st.reason]
        detail = f"日频采集退化 ({ref_str})：{'；'.join(reasons)}"
        logger.error("本月调仓未完成：%s", detail)
        return _commit_failure_artifacts(
            st_dir=st_dir,
            output_dir=output_dir,
            status_store=status_store,
            results=all_results,
            required_names=required_names,
            now_str=now_str,
            prev_index_meta=prev_index_meta,
            is_month_end=True,
            headline_reason=detail,
        )

    # Separate calculation from persistence: compute_all runs with persist=False & freeze_on_stale=True,
    # and run_daily_job commits series/frozen/constituents ONLY after all checks and render pass.
    uni_override = (
        fetched_uni
        if (not (st_dir / "universe.csv").is_file() and fetched_uni is not None)
        else None
    )
    try:
        computed = compute_all(
            state_dir=st_dir,
            as_of_date=ref_day,
            freeze_on_stale=True,
            update_last_success=False,
            persist=False,
            universe_df=uni_override,
        )
    except Exception as exc:
        logger.exception("HK momentum compute_all raised: %s", exc)
        all_results.append(
            make_source_result("index_compute", ok=False, stale=True, reason=f"日度计算异常: {exc}", label="动量日度计算")
        )
        return _commit_failure_artifacts(
            st_dir=st_dir,
            output_dir=output_dir,
            status_store=status_store,
            results=all_results,
            required_names=required_names,
            now_str=now_str,
            prev_index_meta=prev_index_meta,
            is_month_end=is_month_end_rebal,
            headline_reason=f"日度计算异常 ({ref_str})：{exc}",
        )

    meta = dict(computed["meta"])
    comp_statuses = _extract_compute_statuses(meta, ref_str)
    all_results.extend(comp_statuses)

    compute_failed = any(not st.is_clean for st in comp_statuses) or not bool(meta.get("completeness_ok", True))
    if is_month_end_rebal and compute_failed:
        reasons = [st.reason for st in all_results if st.reason] or ["计算结果不完整"]
        detail = f"计算完整性校验未通过 ({ref_str})：{'；'.join(reasons)}"
        logger.error("本月调仓未完成：%s", detail)
        return _commit_failure_artifacts(
            st_dir=st_dir,
            output_dir=output_dir,
            status_store=status_store,
            results=all_results,
            required_names=required_names,
            now_str=now_str,
            prev_index_meta=prev_index_meta,
            is_month_end=True,
            headline_reason=detail,
        )

    snap: dict[str, Any] | None = None
    if not skip_fetch:
        meta["month_end_failed"] = False
        daily_override_map = {st.name: st.to_dict() for st in all_results}
        try:
            snap = compute_intraday_snapshot(
                state_dir=st_dir,
                persist_status=False,
                series_override=computed["series"],
                constituents_override=computed["constituents"],
                meta_override=meta,
                daily_sources_override=daily_override_map,
            )
            all_results.extend(_extract_intraday_statuses(snap))
        except Exception as exc:
            logger.exception("HK momentum compute_intraday_snapshot raised: %s", exc)
            err_msg = f"盘中快照异常: {exc}"
            all_results.extend(
                [
                    make_source_result("hk_intraday_spot", ok=False, stale=True, reason=err_msg, label="港股盘中现货"),
                    make_source_result("hsi_spot", ok=False, stale=True, reason=err_msg, label="恒生指数现货"),
                ]
            )

    # Separate daily status (saved to index_meta.json) from combined daily+intraday status (status.json / HTML).
    render_ok = make_source_result("html_render", ok=True, stale=False, as_of=now_str, label="页面渲染")
    daily_source_names = (
        ["hsi_history", "index_compute"]
        if skip_fetch
        else ["hkex_universe", "ggt_list", "hk_spot", "hk_history", "hsi_history", "index_compute"]
    )
    daily_results = [s for s in all_results if s.name in daily_source_names]
    daily_doc = status_store.preview_sources(
        daily_results + [render_ok],
        required=daily_source_names + ["html_render"],
        now_str=now_str,
        extra_overall={"month_end_failed": False},
    )
    daily_overall = daily_doc["overall"]
    daily_clean = bool(daily_overall.get("ok")) and bool(meta.get("completeness_ok", True))

    provisional_doc = status_store.preview_sources(
        all_results + [render_ok],
        required=required_names,
        now_str=now_str,
        extra_overall={"month_end_failed": False},
    )
    prov_overall = provisional_doc["overall"]
    meta["month_end_failed"] = False
    meta["last_success_at"] = prov_overall.get("last_success_at")
    meta["daily_last_success_at"] = daily_overall.get("last_success_at")
    meta["stale"] = not daily_clean
    meta["stale_reason"] = daily_overall.get("reason")
    meta["sources"] = daily_doc["sources"]
    meta["source_statuses"] = daily_doc["sources"]

    merged_snap: dict[str, Any] | None = None
    if snap is not None:
        merged_snap = dict(snap)
        merged_snap["month_end_failed"] = False
        merged_snap["last_success_at"] = prov_overall.get("last_success_at")
        merged_snap["stale"] = bool(prov_overall.get("stale"))
        merged_snap["stale_reason"] = prov_overall.get("reason")
        merged_snap["sources"] = provisional_doc["sources"]

    # Freeze/persist daily series, constituents & frozen_history whenever daily calculation
    # passed cleanly — independent of realtime snapshot spot status.
    will_freeze = (
        daily_clean
        and bool(computed.get("can_freeze", bool(meta.get("completeness_ok", True))))
        and (computed.get("frozen_payload") is not None)
    )
    meta["froze"] = will_freeze

    # Step A: Render HTML first from in-memory data (status.json & frozen_history.json still untouched!)
    try:
        out_path = write_hkmom_panel(
            state_dir=st_dir,
            output_dir=output_dir,
            series=computed["series"],
            meta=meta,
            constituents=computed["constituents"],
            intraday=merged_snap,
            status_doc=provisional_doc,
        )
    except Exception as exc:
        logger.exception("HK momentum write_hkmom_panel raised: %s", exc)
        render_err = make_source_result(
            "html_render", ok=False, stale=True, reason=f"HTML渲染失败: {exc}", label="页面渲染"
        )
        final_doc = status_store.update_sources(
            all_results + [render_err],
            required=required_names,
            now_str=now_str,
            extra_overall={"month_end_failed": False},
        )
        final_overall = final_doc["overall"]
        meta["froze"] = False
        meta["last_success_at"] = final_overall.get("last_success_at")
        meta["stale"] = True
        meta["stale_reason"] = final_overall.get("reason")
        meta["sources"] = {
            k: v for k, v in final_doc["sources"].items() if k in (daily_source_names + ["html_render"])
        }
        meta["source_statuses"] = meta["sources"]
        atomic_write_json(st_dir / "index_meta.json", meta)
        return {
            "ok": False,
            "publish_ok": False,
            "froze": False,
            "output": str(Path(output_dir) / "index.html"),
            "last_date": meta.get("last_date"),
            "launch_date": meta.get("launch_date"),
            "stale": True,
            "month_end_failed": False,
            "stale_reason": final_overall.get("reason"),
            "last_success_at": final_overall.get("last_success_at"),
            "sources": final_doc["sources"],
        }

    # Step B: After render succeeds, persist series/constituents/frozen when daily is clean,
    # write live.json with intraday state, and perform the single final status.json commit.
    if will_freeze:
        persist_compute_result(st_dir, computed, meta_override=meta)
    elif daily_clean:
        # Mocked/synthetic test path with no frozen_payload: still update series/constituents if present
        atomic_write_json(st_dir / "index_series.json", computed["series"])
        atomic_write_json(st_dir / "constituents.json", computed["constituents"])
        atomic_write_json(st_dir / "index_meta.json", meta)
    else:
        atomic_write_json(st_dir / "index_meta.json", meta)
    if merged_snap is not None:
        atomic_write_json(st_dir / "live.json", merged_snap)

    final_doc = status_store.update_sources(
        all_results + [render_ok],
        required=required_names,
        now_str=now_str,
        extra_overall={"month_end_failed": False},
    )
    overall = final_doc["overall"]
    publish_ok = _hk_publish_ok(
        sources=final_doc["sources"],
        is_month_end=is_month_end_rebal,
        month_end_failed=False,
        skip_fetch=skip_fetch,
        render_ok=True,
    )
    return {
        "ok": bool(overall.get("ok")),
        "publish_ok": bool(publish_ok),
        "froze": will_freeze,
        "output": str(out_path),
        "last_date": meta.get("last_date"),
        "launch_date": meta.get("launch_date"),
        "stale": bool(overall.get("stale")),
        "month_end_failed": False,
        "stale_reason": overall.get("reason"),
        "last_success_at": overall.get("last_success_at"),
        "sources": final_doc["sources"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run HK Momentum daily fetch, compute, and HTML render.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    res = run_daily_job(
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        proxy=args.proxy,
        skip_fetch=args.skip_fetch,
    )
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("publish_ok", res.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
