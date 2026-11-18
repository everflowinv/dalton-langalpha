#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import threading
import time
from pathlib import Path
from openclaw_workspace import workspace_root as resolve_workspace


import requests


SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = resolve_workspace(__file__)
RANKING = WORKSPACE / "research-outputs" / "index-crowding" / "official-all.json"
OUT_DIR = SKILL_ROOT / "state" / "cache" / "history"
URL = "https://fund.wacaijijin.com/extension/api/index/zone/queryCrowdedList"
LOCAL = threading.local()


def session() -> requests.Session:
    value = getattr(LOCAL, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://fund.wacaijijin.com/extension/indexZoneDetail",
                "User-Agent": "Mozilla/5.0 (compatible; EveCrowdingResearch/1.0; research)",
            }
        )
        LOCAL.session = value
    return value


def ranking_values() -> dict[str, tuple[str, float, int | None]]:
    """Today's crowding per index from the public ranking: (YYYYMMDD, percentile, level)."""
    try:
        source = json.loads(RANKING.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    trade_dt = str(source.get("trade_date") or "").replace("-", "")
    out = {}
    for row in source.get("rows") or []:
        value = row.get("crowding_percentile")
        if trade_dt and value is not None:
            out[row["index_code"]] = (trade_dt, float(value), row.get("crowding_level_code"))
    return out


_RANKING: dict[str, tuple[str, float, int | None]] | None = None


def keep_known_crowding(code: str, payload: dict, path: Path) -> dict:
    """Since 2026-11 the vendor hides the recent crowding history (value/crowded are null
    without a VIP account) while closes stay public. Keep every crowding value already
    cached for a date, and add today's value from the public ranking, so a refresh never
    erases history it can no longer download."""
    global _RANKING
    rows = payload.get("data")
    if not isinstance(rows, list):
        return payload
    known: dict[str, dict] = {}
    if path.exists():
        try:
            old = json.loads(path.read_text())
            old_rows = old.get("data")
            # A cache written straight from the vendor's newer response nests one level deeper.
            if isinstance(old_rows, dict):
                old_rows = old_rows.get("data")
            for item in old_rows if isinstance(old_rows, list) else []:
                if isinstance(item, dict) and item.get("value") is not None:
                    known[str(item.get("tradeDt"))] = item
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    if _RANKING is None:
        _RANKING = ranking_values()
    today = _RANKING.get(code)
    merged = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        dt = str(item.get("tradeDt"))
        seen.add(dt)
        if item.get("value") is None:
            if dt in known:
                item = {**item, "value": known[dt].get("value"), "crowded": known[dt].get("crowded")}
            elif today and dt == today[0]:
                item = {**item, "value": today[1], "crowded": today[2]}
        merged.append(item)
    for dt, item in known.items():
        if dt not in seen:
            merged.append(item)
    merged.sort(key=lambda item: str(item.get("tradeDt")))
    return {**payload, "data": merged}


def fetch(code: str, refresh: bool) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{code.replace('.', '_')}.json"
    if path.exists() and not refresh:
        payload = json.loads(path.read_text())
        return {"code": code, "cached": True, "rows": len(payload.get("data") or [])}
    last_error = None
    for attempt in range(4):
        try:
            response = session().get(URL, params={"indexCode": code, "timeInterval": 3}, timeout=40)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(payload.get("error") or f"vendor code={payload.get('code')}")
            inner = payload.get("data")  # 2026-11-13: vendor now nests {"data": {"data": [...]}}
            if isinstance(inner, dict) and isinstance(inner.get("data"), list):
                payload = {**payload, "data": inner["data"]}
            if not isinstance(payload.get("data"), list):
                # Never overwrite the cache with a malformed body: the history it holds
                # can no longer be downloaded (VIP-only since 2026-11).
                raise RuntimeError(f"vendor returned no data list ({type(payload.get('data')).__name__})")
            payload = keep_known_crowding(code, payload, path)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
            return {"code": code, "cached": False, "rows": len(payload.get("data") or [])}
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{code}: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    source = json.loads(RANKING.read_text())
    codes = [row["index_code"] for row in source["rows"]]
    results = []
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 10))) as pool:
        futures = {pool.submit(fetch, code, args.refresh): code for code in codes}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(str(exc))
            if i % 25 == 0 or i == len(codes):
                print(f"progress={i}/{len(codes)} errors={len(errors)}", flush=True)
    summary = {
        "ok": not errors,
        "requested": len(codes),
        "downloaded": len(results),
        "with_history": sum(item["rows"] > 0 for item in results),
        "errors": errors,
    }
    (OUT_DIR.parent / "fetch-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
