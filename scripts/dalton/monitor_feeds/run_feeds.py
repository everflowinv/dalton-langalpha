#!/usr/bin/env python3
"""Host daemon pushing 1-minute realtime monitor data to Dalton backend.

- Runs every 60 seconds during CN (09:30-11:30, 13:00-15:00) and HK
  (09:30-12:00, 13:00-16:15) exchange trading sessions (skipping lunch breaks
  and exchange holidays via ``exchange_calendars``).
- Outside trading windows: writes one post-close final value per trading day,
  then sleeps until the next trading session opens.
- On any collection failure: retains last good value with ``stale=True``,
  ``stale_reason``, and ``last_success_at``.
- Reads ``INTERNAL_SERVICE_TOKEN`` from env; never embeds or logs the token.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.monitor_feeds.ashare_monitor.collect import AShareCollector  # noqa: E402
from scripts.dalton.monitor_feeds.common import (  # noqa: E402
    CN_TZ,
    HK_TZ,
    MarketKind,
    SourceStatusStore,
    atomic_write_json,
    load_json_safe,
    make_source_result,
    market_session_state,
    next_session_open,
    now_cn,
    push_monitor_data,
    resolve_state_dir,
)
from scripts.dalton.monitor_feeds.hk_momentum.compute import (  # noqa: E402
    compute_intraday_snapshot,
)

logger = logging.getLogger("dalton.monitor_feeds")


class FeedDaemon:
    """Orchestrates 60s CN/HK realtime feeds with post-close final write & sleep."""

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        hk_daily_dir: str | Path | None = None,
        backend_url: str | None = None,
        interval_seconds: float = 60.0,
        max_idle_sleep_seconds: float = 300.0,
        feeds: tuple[MarketKind, ...] = ("cn", "hk"),
        cn_collect_fn: Callable[[], dict[str, Any]] | None = None,
        hk_collect_fn: Callable[[datetime], dict[str, Any]] | None = None,
        push_fn: Callable[..., dict[str, Any]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.daemon_state_dir = resolve_state_dir("_daemon", state_dir)
        self.state_file = self.daemon_state_dir / "feed_state.json"
        self.backend_url = (
            backend_url
            or os.environ.get("DALTON_BACKEND_URL")
            or "http://127.0.0.1:8000"
        ).rstrip("/")
        self.interval_seconds = float(interval_seconds)
        self.max_idle_sleep_seconds = float(max_idle_sleep_seconds)
        self.feeds = feeds
        self._now = now_fn or now_cn
        self._push = push_fn or push_monitor_data

        cn_state_dir = (
            Path(state_dir) / "ashare_monitor"
            if state_dir is not None
            else None
        )
        hk_state_dir = (
            Path(state_dir) / "hk_momentum"
            if state_dir is not None
            else None
        )
        self._hk_local_state_dir = hk_state_dir
        default_hk_daily = (
            hk_daily_dir
            or os.environ.get("DALTON_HK_DAILY_STATE_DIR", "").strip()
            or (REPO_ROOT / "var" / "dalton" / "openclaw-data" / "state" / "monitor-feeds" / "hk_momentum")
        )
        self.hk_daily_dir = Path(default_hk_daily)

        if cn_collect_fn is not None:
            self._cn_collect = cn_collect_fn
        else:
            collector = AShareCollector(state_dir=cn_state_dir, now_fn=self._now)
            self._cn_collect = collector.collect_snapshot

        if hk_collect_fn is not None:
            self._hk_collect = hk_collect_fn
        else:
            self._hk_collect = lambda cur: compute_intraday_snapshot(
                state_dir=hk_state_dir,
                daily_state_dir=self.hk_daily_dir,
                now=cur,
            )

        loaded = load_json_safe(self.state_file, {}) or {}
        self._state_lock = threading.Lock()
        self.state: dict[str, dict[str, Any]] = {
            "cn": dict(loaded.get("cn") or {}),
            "hk": dict(loaded.get("hk") or {}),
        }

    def _save_state(self) -> None:
        with self._state_lock:
            atomic_write_json(self.state_file, json.loads(json.dumps(self.state)))

    def _next_attempt_seq(self, market: MarketKind) -> int:
        """Return a strictly monotonic attempt sequence number persisted across restarts."""
        with self._state_lock:
            prev = int(self.state[market].get("last_attempt_seq") or 0)
            seq = max(prev + 1, time.time_ns() // 1_000_000)
            self.state[market]["last_attempt_seq"] = seq
            return seq

    def _hk_daily_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for candidate in (self.hk_daily_dir, self._hk_local_state_dir):
            if candidate is not None:
                p = Path(candidate)
                if p not in dirs:
                    dirs.append(p)
        return dirs

    def _hk_daily_has_trade_date(self, trade_date: str) -> bool:
        for p in self._hk_daily_dirs():
            series = load_json_safe(p / "index_series.json")
            if isinstance(series, dict) and series.get("dates") and str(series["dates"][-1]) >= trade_date:
                return True
        return False

    def _hk_daily_state_signature(self) -> str:
        """Return compact signature of daily status/meta/series so any container update triggers a host push."""
        parts: list[dict[str, Any]] = []
        for p in self._hk_daily_dirs():
            st_doc = load_json_safe(p / "status.json")
            meta = load_json_safe(p / "index_meta.json")
            series = load_json_safe(p / "index_series.json")
            if not (st_doc or meta or series):
                continue
            overall = (st_doc or {}).get("overall") if isinstance(st_doc, dict) else {}
            parts.append(
                {
                    "status_updated": (st_doc or {}).get("updated") if isinstance(st_doc, dict) else None,
                    "overall_ok": overall.get("ok") if isinstance(overall, dict) else None,
                    "overall_month_end_failed": overall.get("month_end_failed") if isinstance(overall, dict) else None,
                    "overall_reason": overall.get("reason") if isinstance(overall, dict) else None,
                    "meta_updated": (meta or {}).get("updated") if isinstance(meta, dict) else None,
                    "meta_month_end_failed": (meta or {}).get("month_end_failed") if isinstance(meta, dict) else None,
                    "meta_stale_reason": (meta or {}).get("stale_reason") if isinstance(meta, dict) else None,
                    "series_last": str(series["dates"][-1]) if isinstance(series, dict) and series.get("dates") else None,
                }
            )
        return json.dumps(parts, ensure_ascii=False, sort_keys=True) if parts else ""

    def _hk_daily_status_overlay(self) -> dict[str, Any]:
        """Read daily-only status/meta from disk (never overlay container intraday errors onto live recovery)."""
        intraday_only = {"hk_intraday_spot", "hsi_spot"}
        sources: dict[str, Any] = {}
        reasons: list[str] = []
        month_end_failed = False
        daily_last_ok: str | None = None
        for p in reversed(self._hk_daily_dirs()):
            st_doc = load_json_safe(p / "status.json")
            st_overall_ok: str | None = None
            if isinstance(st_doc, dict):
                if isinstance(st_doc.get("sources"), dict):
                    for sk, sv in st_doc["sources"].items():
                        if sk not in intraday_only:
                            sources[sk] = sv
                overall = st_doc.get("overall") or {}
                if isinstance(overall, dict):
                    if overall.get("month_end_failed"):
                        month_end_failed = True
                    if overall.get("last_success_at"):
                        st_overall_ok = str(overall["last_success_at"])
            meta = load_json_safe(p / "index_meta.json")
            meta_daily_ok: str | None = None
            meta_fallback_ok: str | None = None
            if isinstance(meta, dict):
                if isinstance(meta.get("source_statuses"), dict):
                    for sk, sv in meta["source_statuses"].items():
                        if sk not in intraday_only:
                            sources[sk] = sv
                if meta.get("month_end_failed"):
                    month_end_failed = True
                if (meta.get("month_end_failed") or meta.get("stale")) and meta.get("stale_reason"):
                    r = str(meta["stale_reason"])
                    if r not in reasons:
                        reasons.append(r)
                if meta.get("daily_last_success_at"):
                    meta_daily_ok = str(meta["daily_last_success_at"])
                if meta.get("last_success_at"):
                    meta_fallback_ok = str(meta["last_success_at"])
            picked_ok = meta_daily_ok or st_overall_ok or meta_fallback_ok
            if picked_ok:
                daily_last_ok = picked_ok
        return {
            "sources": sources,
            "reasons": reasons,
            "month_end_failed": month_end_failed,
            "daily_last_success_at": daily_last_ok,
        }

    def _hk_daily_is_stale_or_failed(self) -> bool:
        ov = self._hk_daily_status_overlay()
        if ov["month_end_failed"] or ov["reasons"]:
            return True
        for entry in ov["sources"].values():
            if isinstance(entry, dict) and (entry.get("stale") or not entry.get("ok", True)):
                return True
        return False

    def _collect_and_push_one(
        self,
        market: MarketKind,
        cur: datetime,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        sess = market_session_state(market, cur)
        slug, data_key = (
            ("cn-intraday", "snapshot") if market == "cn" else ("hk-momentum", "intraday")
        )
        tz = CN_TZ if market == "cn" else HK_TZ
        cur_local = cur.astimezone(tz)
        cur_str = cur_local.strftime("%Y-%m-%d %H:%M:%S")
        cur_iso = cur_local.isoformat(timespec="seconds")
        trade_date = str(sess["trade_date"])

        with self._state_lock:
            fstate_snap = dict(self.state[market])

        def _parse_ts(raw: Any) -> datetime | None:
            if not raw:
                return None
            s = str(raw).strip()
            if not s:
                return None
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=cur_local.tzinfo)
            return dt

        def _is_stale_conflict_error(exc: Exception) -> bool:
            status_code = getattr(exc, "status_code", None)
            if status_code == 409:
                return True
            resp_obj = getattr(exc, "response", None)
            if getattr(resp_obj, "status_code", None) == 409:
                return True
            msg = str(exc)
            return "(HTTP 409)" in msg or "HTTP 409" in msg

        def _has_newer_or_equal_pending(state_view: dict[str, Any]) -> dict[str, Any] | None:
            cur_pending = state_view.get("pending_push")
            if not (isinstance(cur_pending, dict) and isinstance(cur_pending.get("payload"), dict)):
                return None
            p_payload = cur_pending["payload"]
            p_ts = _parse_ts(
                cur_pending.get("as_of") or p_payload.get("as_of_iso") or p_payload.get("as_of")
            )
            lg_payload = state_view.get("last_good_payload") or {}
            lg_ts = (
                _parse_ts(lg_payload.get("as_of_iso") or lg_payload.get("as_of"))
                if isinstance(lg_payload, dict)
                else None
            )
            if lg_ts is None or p_ts is None or p_ts >= lg_ts:
                return cur_pending
            return None

        def _retry_pending(
            pending: dict[str, Any],
            *,
            action_on_ok: str = "retried_push",
            extra_result: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            p_slug = str(pending.get("slug") or slug)
            p_key = str(pending.get("data_key") or data_key)
            p_payload = pending.get("payload")
            p_stale = bool(
                pending.get("is_stale")
                if "is_stale" in pending
                else (isinstance(p_payload, dict) and p_payload.get("stale"))
            )
            if "as_of" in pending:
                p_as_of = pending.get("as_of")
            elif p_stale:
                p_as_of = None
            else:
                p_as_of = (
                    (p_payload.get("as_of_iso") if isinstance(p_payload, dict) else None)
                    or cur_iso
                )
            p_attempt_seq = (
                int(pending["attempt_seq"])
                if pending.get("attempt_seq") is not None
                else self._next_attempt_seq(market)
            )
            p_error = (
                pending.get("error")
                or (p_payload.get("stale_reason") if isinstance(p_payload, dict) else None)
            )
            p_last_attempt_at = pending.get("last_attempt_at") or cur_iso
            try:
                self._push(
                    p_slug,
                    p_key,
                    p_payload,
                    as_of=p_as_of,
                    attempt_seq=p_attempt_seq,
                    is_stale=p_stale,
                    error=p_error if p_stale else None,
                    last_attempt_at=p_last_attempt_at,
                    backend_url=self.backend_url,
                )
            except Exception as retry_exc:
                if _is_stale_conflict_error(retry_exc):
                    logger.info(
                        "Pending push for %s/%s superseded on server (HTTP 409); clearing stale pending_push: %s",
                        p_slug,
                        p_key,
                        retry_exc,
                    )
                    with self._state_lock:
                        self.state[market].pop("pending_push", None)
                    self._save_state()
                    stale_res: dict[str, Any] = {
                        "market": market,
                        "action": "stale_pending_cleared",
                        "stale": False,
                        "error": str(retry_exc),
                    }
                    if extra_result:
                        stale_res.update(extra_result)
                    return stale_res
                logger.warning("Pending push retry failed for %s/%s: %s", p_slug, p_key, retry_exc)
                with self._state_lock:
                    updated_pending = dict(pending)
                    updated_pending["failed_at"] = cur_iso
                    updated_pending["attempts"] = int(updated_pending.get("attempts") or 1) + 1
                    self.state[market]["pending_push"] = updated_pending
                self._save_state()
                res: dict[str, Any] = {
                    "market": market,
                    "action": "retry_push_failed",
                    "stale": p_stale,
                    "error": str(retry_exc),
                }
                if extra_result:
                    res.update(extra_result)
                return res
            p_trade_date = str(pending.get("trade_date") or trade_date)
            p_daily_sig = str(pending.get("current_daily_sig") or "")
            with self._state_lock:
                self.state[market].pop("pending_push", None)
                if market == "hk" and p_daily_sig:
                    self.state[market]["last_hk_daily_sig"] = p_daily_sig
                if pending.get("is_closed_td") and not pending.get("in_window") and not p_stale:
                    if pending.get("needs_post_close"):
                        self.state[market]["post_close_written_date"] = p_trade_date
                    sc = p_payload.get("smallcap") or {} if isinstance(p_payload, dict) else {}
                    ng = p_payload.get("nonggt") or {} if isinstance(p_payload, dict) else {}
                    pushed_daily_date = (
                        (p_payload.get("daily_close_date") or p_payload.get("trade_date"))
                        if isinstance(p_payload, dict)
                        else None
                    ) or p_trade_date
                    if (
                        market == "hk"
                        and isinstance(sc, dict)
                        and isinstance(ng, dict)
                        and sc.get("source") == "daily_close"
                        and ng.get("source") == "daily_close"
                        and pushed_daily_date == p_trade_date
                        and not self._hk_daily_is_stale_or_failed()
                    ):
                        self.state[market]["post_close_daily_synced_date"] = p_trade_date
            self._save_state()
            ok_res: dict[str, Any] = {
                "market": market,
                "action": action_on_ok,
                "stale": p_stale,
                "as_of": p_payload.get("as_of") if isinstance(p_payload, dict) else None,
            }
            if extra_result:
                ok_res.update(extra_result)
            return ok_res

        # Calendar error must be reported & pushed BEFORE skipping collection,
        # while preserving any newer unpushed pending snapshot instead of stamping last_good_payload.
        if sess.get("calendar_error"):
            cal_reason = f"交易日历不可用: {sess['calendar_error']}"
            cal_st = make_source_result(
                "calendar",
                ok=False,
                stale=True,
                reason=cal_reason,
                label="交易日历",
            )
            daemon_store = SourceStatusStore(self.daemon_state_dir, filename=f"{market}_daemon_status.json")
            status_doc = daemon_store.update_sources([cal_st], required=["calendar"], now_str=f"{cur_str} 北京时间")
            newer_pending = _has_newer_or_equal_pending(fstate_snap)
            if newer_pending is not None:
                return _retry_pending(
                    newer_pending,
                    action_on_ok="retried_push",
                    extra_result={"calendar_error": str(sess["calendar_error"])},
                )
            payload = dict(fstate_snap.get("last_good_payload") or {})
            merged_sources = dict(payload.get("sources") or {})
            merged_sources.update(status_doc["sources"])
            payload.update(
                {
                    "as_of": cur_str,
                    "as_of_iso": cur_iso,
                    "tz": "北京时间",
                    "trade_date": trade_date,
                    "trading": False,
                    "market_status": "calendar_error",
                    "market_status_label": "交易日历不可用",
                    "stale": True,
                    "stale_reason": cal_reason,
                    "last_success_at": fstate_snap.get("last_success_at"),
                    "sources": merged_sources,
                }
            )
            attempt_seq = self._next_attempt_seq(market)
            try:
                self._push(
                    slug,
                    data_key,
                    payload,
                    as_of=None,
                    attempt_seq=attempt_seq,
                    is_stale=True,
                    error=cal_reason,
                    last_attempt_at=cur_iso,
                    backend_url=self.backend_url,
                )
            except Exception as exc:
                logger.warning("Calendar error push failed for %s/%s: %s", slug, data_key, exc)
                with self._state_lock:
                    self.state[market]["pending_push"] = {
                        "slug": slug,
                        "data_key": data_key,
                        "payload": payload,
                        "as_of": None,
                        "attempt_seq": attempt_seq,
                        "is_stale": True,
                        "error": cal_reason,
                        "last_attempt_at": cur_iso,
                        "trade_date": trade_date,
                        "in_window": False,
                        "is_closed_td": False,
                        "needs_post_close": False,
                        "current_daily_sig": "",
                        "failed_at": cur_iso,
                        "attempts": 1,
                    }
                self._save_state()
                return {
                    "market": market,
                    "action": "push_failed",
                    "stale": True,
                    "error": str(exc),
                }
            with self._state_lock:
                self.state[market].pop("pending_push", None)
            self._save_state()
            return {
                "market": market,
                "action": "calendar_error",
                "stale": True,
                "as_of": payload.get("as_of"),
            }

        in_window = bool(sess["in_trading_window"])
        is_closed_td = bool(sess["is_trading_day"]) and sess["status"] == "closed"
        needs_post_close = is_closed_td and fstate_snap.get("post_close_written_date") != trade_date
        needs_hk_daily_sync = (
            market == "hk"
            and is_closed_td
            and fstate_snap.get("post_close_daily_synced_date") != trade_date
            and self._hk_daily_has_trade_date(trade_date)
        )
        current_daily_sig = self._hk_daily_state_signature() if market == "hk" else ""
        daily_state_changed = (
            market == "hk"
            and bool(current_daily_sig)
            and current_daily_sig != fstate_snap.get("last_hk_daily_sig")
        )
        should_collect = bool(
            force
            or in_window
            or needs_post_close
            or needs_hk_daily_sync
            or daily_state_changed
        )

        pending_push = fstate_snap.get("pending_push")
        if not should_collect:
            if isinstance(pending_push, dict) and isinstance(pending_push.get("payload"), dict):
                return _retry_pending(pending_push)
            return {"market": market, "action": "skip", "status": sess["status"]}

        daemon_store = SourceStatusStore(self.daemon_state_dir, filename=f"{market}_daemon_status.json")
        collect_failed = False
        try:
            payload = (
                self._cn_collect()
                if market == "cn"
                else self._hk_collect(cur_local)
            )
            if not isinstance(payload, dict):
                raise RuntimeError("采集结果格式无效")
        except Exception as exc:
            collect_failed = True
            with self._state_lock:
                fstate_cur = dict(self.state[market])
            daemon_err_st = make_source_result(
                f"{market}_collector",
                ok=False,
                stale=True,
                reason=f"采集异常: {exc}",
                label="实时采集",
            )
            status_doc = daemon_store.update_sources(
                [daemon_err_st], required=[f"{market}_collector"], now_str=f"{cur_str} 北京时间"
            )
            newer_pending = _has_newer_or_equal_pending(fstate_cur)
            if newer_pending is not None:
                # Never overwrite a newer unpushed pending snapshot with an older last_good_payload
                # stamped with a synthetic cur_iso. Retry the pending snapshot directly instead.
                return _retry_pending(
                    newer_pending,
                    action_on_ok="retried_push",
                    extra_result={"collect_error": str(exc)},
                )
            payload = dict(fstate_cur.get("last_good_payload") or {})
            merged_sources = dict(payload.get("sources") or {})
            merged_sources.update(status_doc["sources"])
            payload.update(
                {
                    "as_of": cur_str,
                    "as_of_iso": cur_iso,
                    "tz": "北京时间",
                    "trade_date": trade_date,
                    "trading": in_window,
                    "market_status": sess["status"],
                    "market_status_label": sess["label"],
                    "stale": True,
                    "stale_reason": f"采集异常: {exc}",
                    "last_success_at": fstate_cur.get("last_success_at"),
                    "sources": merged_sources,
                }
            )
        else:
            if market == "hk":
                # Ensure daily container state (especially month-end failure without host token) surfaces cleanly.
                ov = self._hk_daily_status_overlay()
                if ov["sources"]:
                    merged_sources = dict(ov["sources"])
                    if isinstance(payload.get("sources"), dict):
                        merged_sources.update(payload["sources"])
                    payload["sources"] = merged_sources
                if ov["month_end_failed"] or ov["reasons"]:
                    payload["stale"] = True
                    if ov["month_end_failed"]:
                        payload["month_end_failed"] = True
                    existing_parts = [p for p in str(payload.get("stale_reason") or "").split("；") if p]
                    for r in ov["reasons"]:
                        if r not in existing_parts:
                            existing_parts.append(r)
                    payload["stale_reason"] = "；".join(existing_parts) if existing_parts else None
            with self._state_lock:
                if not payload.get("stale"):
                    self.state[market]["last_good_payload"] = payload
                    self.state[market]["last_success_at"] = payload.get("last_success_at") or cur_str
                elif not payload.get("last_success_at") and self.state[market].get("last_success_at"):
                    payload["last_success_at"] = self.state[market]["last_success_at"]

        is_stale = bool(payload.get("stale"))
        if collect_failed:
            push_as_of = None
        elif is_stale:
            push_as_of = payload.get("as_of_iso")
        else:
            push_as_of = payload.get("as_of_iso") or cur_iso
        push_error = (
            str(payload.get("stale_reason") or "采集异常")
            if is_stale
            else None
        )
        attempt_seq = self._next_attempt_seq(market)
        try:
            self._push(
                slug,
                data_key,
                payload,
                as_of=push_as_of,
                attempt_seq=attempt_seq,
                is_stale=is_stale,
                error=push_error,
                last_attempt_at=cur_iso,
                backend_url=self.backend_url,
            )
        except Exception as exc:
            logger.warning("Push failed for %s/%s: %s", slug, data_key, exc)
            with self._state_lock:
                self.state[market]["pending_push"] = {
                    "slug": slug,
                    "data_key": data_key,
                    "payload": payload,
                    "as_of": push_as_of,
                    "attempt_seq": attempt_seq,
                    "is_stale": is_stale,
                    "error": push_error,
                    "last_attempt_at": cur_iso,
                    "trade_date": trade_date,
                    "in_window": in_window,
                    "is_closed_td": is_closed_td,
                    "needs_post_close": needs_post_close,
                    "current_daily_sig": current_daily_sig,
                    "failed_at": cur_iso,
                    "attempts": 1,
                }
            self._save_state()
            return {
                "market": market,
                "action": "push_failed",
                "stale": is_stale,
                "error": str(exc),
            }
        with self._state_lock:
            self.state[market].pop("pending_push", None)
            if market == "hk" and current_daily_sig:
                self.state[market]["last_hk_daily_sig"] = current_daily_sig
            if is_closed_td and not in_window and not is_stale:
                sc = payload.get("smallcap") or {}
                ng = payload.get("nonggt") or {}
                pushed_daily_date = payload.get("daily_close_date") or payload.get("trade_date") or trade_date
                both_daily_close = (
                    market == "hk"
                    and isinstance(sc, dict)
                    and isinstance(ng, dict)
                    and sc.get("source") == "daily_close"
                    and ng.get("source") == "daily_close"
                    and pushed_daily_date == trade_date
                    and not self._hk_daily_is_stale_or_failed()
                )
                if needs_post_close:
                    self.state[market]["post_close_written_date"] = trade_date
                if both_daily_close:
                    self.state[market]["post_close_daily_synced_date"] = trade_date
        self._save_state()
        if (needs_hk_daily_sync or daily_state_changed) and not needs_post_close and not in_window:
            action = "post_close_daily_sync"
        elif needs_post_close and not in_window:
            action = "post_close"
        else:
            action = "pushed"
        return {
            "market": market,
            "action": action,
            "stale": is_stale,
            "as_of": payload.get("as_of"),
        }

    def run_tick(self, *, now: datetime | None = None, force: bool = False) -> list[dict[str, Any]]:
        cur = now or self._now()
        if len(self.feeds) <= 1:
            return [self._collect_and_push_one(m, cur, force=force) for m in self.feeds]
        with ThreadPoolExecutor(max_workers=len(self.feeds)) as ex:
            futures = [ex.submit(self._collect_and_push_one, m, cur, force=force) for m in self.feeds]
            return [f.result() for f in futures]

    def _aligned_interval_wait(self, cur: datetime) -> float:
        ts = cur.timestamp()
        interval = max(1.0, self.interval_seconds)
        next_boundary = (math.floor(ts / interval) + 1.0) * interval
        return max(0.05, next_boundary - ts)

    def compute_sleep_seconds_for(self, market: MarketKind, *, now: datetime | None = None) -> float:
        cur = now or self._now()
        sess = market_session_state(market, cur)
        if sess.get("calendar_error") or sess["in_trading_window"]:
            return self._aligned_interval_wait(cur)
        trade_date = str(sess["trade_date"])
        with self._state_lock:
            fstate = dict(self.state[market])
        if isinstance(fstate.get("pending_push"), dict):
            return self._aligned_interval_wait(cur)
        if sess["is_trading_day"] and sess["status"] == "closed":
            if fstate.get("post_close_written_date") != trade_date:
                return self._aligned_interval_wait(cur)
            close_hm = int(sess.get("close_hm") or 960)
            if (
                market == "hk"
                and (
                    fstate.get("post_close_daily_synced_date") != trade_date
                    or self._hk_daily_is_stale_or_failed()
                )
                and sess["hm"] <= close_hm + 90
            ):
                return self._aligned_interval_wait(cur)
        nxt = next_session_open(market, cur)
        delta = max(1.0, nxt.timestamp() - cur.timestamp())
        return min(delta, self.max_idle_sleep_seconds)

    def compute_sleep_seconds(self, *, now: datetime | None = None) -> float:
        cur = now or self._now()
        if not self.feeds:
            return self._aligned_interval_wait(cur)
        return min(self.compute_sleep_seconds_for(m, now=cur) for m in self.feeds)

    def run_market_loop(
        self,
        market: MarketKind,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        stop = stop_event or threading.Event()
        iters = 0
        while not stop.is_set():
            self._collect_and_push_one(market, self._now())
            iters += 1
            if max_iterations is not None and iters >= max_iterations:
                break
            wait_sec = self.compute_sleep_seconds_for(market)
            if stop.wait(timeout=wait_sec):
                break

    def run_loop(
        self,
        *,
        stop_event: threading.Event | None = None,
        max_iterations: int | None = None,
    ) -> None:
        stop = stop_event or threading.Event()
        if len(self.feeds) <= 1:
            for market in self.feeds:
                self.run_market_loop(market, stop_event=stop, max_iterations=max_iterations)
            return
        threads = [
            threading.Thread(
                target=self.run_market_loop,
                args=(market,),
                kwargs={"stop_event": stop, "max_iterations": max_iterations},
                name=f"feed-{market}",
                daemon=True,
            )
            for market in self.feeds
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Run Dalton monitor realtime feeds daemon.")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--hk-daily-dir", default=None)
    parser.add_argument("--backend-url", default=None)
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--feeds", default="cn,hk", help="Comma-separated feeds: cn,hk")
    parser.add_argument("--once", action="store_true", help="Force one immediate collect+push cycle and exit")
    args = parser.parse_args()

    selected = tuple(
        f.strip() for f in args.feeds.split(",") if f.strip() in ("cn", "hk")
    )
    if not selected:
        raise SystemExit("No valid feeds selected (expected cn,hk)")

    daemon = FeedDaemon(
        state_dir=args.state_dir,
        hk_daily_dir=args.hk_daily_dir,
        backend_url=args.backend_url,
        interval_seconds=args.interval,
        feeds=selected,  # type: ignore[arg-type]
    )
    if args.once:
        res = daemon.run_tick(force=True)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    stop = threading.Event()

    def _handle_sig(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)
    logger.info("Starting monitor feeds daemon (feeds=%s, interval=%.0fs)", selected, args.interval)
    daemon.run_loop(stop_event=stop)
    logger.info("Monitor feeds daemon stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
