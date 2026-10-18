#!/usr/bin/env python3
"""Sync OpenClaw Broker v2 model catalog into `src/dalton/llm/openclaw_overlay.json` (P0.4 §7).

Usage:
  uv run python scripts/dalton/sync_openclaw_models.py \
      [--broker-url http://127.0.0.1:18789/dalton-broker/v1] \
      [--with-openclaw-config ~/.openclaw/openclaw.json] \
      [--dalton-price-cards PATH] [--dry-run] [--check]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

from src.dalton.llm.openclaw_catalog import (  # noqa: E402
    OVERLAY_PATH,
    UPSTREAM_MODELS_PATH,
    assert_no_credentials,
    build_overlay,
    diff_overlay,
    extract_v1_rate_cards_from_openclaw_config,
    load_overrides,
    public_extract_sha256,
    write_overlay_atomic,
)
from src.dalton.llm.overlay import load_overlay_data  # noqa: E402
from src.dalton.settings import get_dalton_broker_token, get_dalton_broker_url  # noqa: E402


def _fetch_broker_catalog(broker_url: str, token: str) -> dict[str, Any]:
    url = f"{broker_url.rstrip('/')}/models?detail=1"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Broker /models?detail=1 did not return a JSON object")
        return data


def _try_record_sync_run_in_db(
    *,
    dry_run: bool,
    status: str,
    counts: dict[str, Any],
    diff: dict[str, Any],
    warnings: list[str],
    overlay_stamp: str | None,
    sha256: str,
) -> None:
    """Best-effort DB recording when Postgres is reachable from host CLI."""

    async def _run() -> None:
        from src.dalton.database.openclaw_sync import create_sync_run, finish_sync_run

        run_id = await create_sync_run(
            started_by="cli",
            dry_run=dry_run,
            forced=False,
        )
        await finish_sync_run(
            run_id=run_id,
            status=status,
            counts=counts,
            diff=diff,
            warnings=warnings,
            error=None,
            overlay_stamp=overlay_stamp,
            public_extract_sha256=sha256,
        )

    try:
        asyncio.run(_run())
    except Exception:
        # Host CLI runs even when Postgres container is stopped
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync OpenClaw Broker v2 models into overlay.")
    parser.add_argument(
        "--broker-url",
        default=None,
        help="Broker base URL (default: DALTON_BROKER_URL or http://127.0.0.1:18789/dalton-broker/v1)",
    )
    parser.add_argument(
        "--with-openclaw-config",
        default=None,
        help="Path to ~/.openclaw/openclaw.json to read v1 rateCard whitelist fields (host only)",
    )
    parser.add_argument(
        "--dalton-price-cards",
        default=None,
        help="Optional JSON file with legacy Dalton price cards",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print diff without writing openclaw_overlay.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if openclaw_overlay.json is out of sync or collides with upstream models.json",
    )
    args = parser.parse_args(argv)

    broker_url = args.broker_url or get_dalton_broker_url("127.0.0.1")
    token = get_dalton_broker_token()

    v1_rate_cards: dict[str, dict[str, Any]] = {}
    if args.with_openclaw_config:
        cfg_path = Path(args.with_openclaw_config).expanduser().resolve()
        v1_rate_cards = extract_v1_rate_cards_from_openclaw_config(cfg_path)

    price_cards: dict[str, dict[str, Any]] = {}
    if args.dalton_price_cards:
        pc_path = Path(args.dalton_price_cards).expanduser().resolve()
        if pc_path.exists():
            price_cards = json.loads(pc_path.read_text(encoding="utf-8"))

    current = load_overlay_data(OVERLAY_PATH)

    # In --check mode, if broker is unreachable, still check existing overlay integrity + collision
    catalog: dict[str, Any] | None = None
    try:
        catalog = _fetch_broker_catalog(broker_url, token)
    except Exception as exc:
        if args.check and current and (current.get("models") or {}):
            print(
                f"[WARN] Broker unreachable ({exc.__class__.__name__}); checking committed overlay only.",
                file=sys.stderr,
            )
        else:
            print(f"[ERROR] Failed to fetch broker catalog from {broker_url}: {exc}", file=sys.stderr)
            return 1

    upstream_models: dict[str, Any] = {}
    if UPSTREAM_MODELS_PATH.exists():
        upstream_models = json.loads(UPSTREAM_MODELS_PATH.read_text(encoding="utf-8"))

    if catalog is not None:
        new_overlay = build_overlay(
            catalog=catalog,
            overrides=load_overrides(),
            v1_rate_cards=v1_rate_cards,
            price_cards=price_cards,
            current_overlay=current,
        )
    else:
        assert current is not None
        new_overlay = current

    assert_no_credentials(new_overlay)

    collisions = [k for k in (new_overlay.get("models") or {}) if k in upstream_models]
    if collisions:
        print(f"[ERROR] Overlay keys collide with upstream models.json: {collisions}", file=sys.stderr)
        return 1

    diff = diff_overlay(current, new_overlay)
    meta = new_overlay.get("meta") or {}
    counts = dict(meta.get("counts") or {})
    counts.update(
        {
            "added": len(diff["added"]),
            "removed": len(diff["removed"]),
            "changed": len(diff["changed"]),
        }
    )

    if args.check:
        if catalog is not None and (diff["added"] or diff["removed"] or diff["changed"]):
            print(
                json.dumps(
                    {
                        "status": "out_of_sync",
                        "diff": diff,
                        "counts": counts,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1
        print(
            json.dumps(
                {
                    "status": "ok",
                    "counts": meta.get("counts"),
                    "warnings": meta.get("warnings"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    sha256 = public_extract_sha256(new_overlay)
    stamp: str | None = None
    if not args.dry_run:
        stamp = write_overlay_atomic(new_overlay, OVERLAY_PATH)

    _try_record_sync_run_in_db(
        dry_run=bool(args.dry_run),
        status="succeeded",
        counts=counts,
        diff=diff,
        warnings=list(meta.get("warnings") or []),
        overlay_stamp=stamp,
        sha256=sha256,
    )

    summary = {
        "dry_run": bool(args.dry_run),
        "applied": not args.dry_run,
        "counts": counts,
        "added": diff["added"],
        "removed": diff["removed"],
        "changed": diff["changed"],
        "unchanged": diff["unchanged"],
        "price_discrepancies": meta.get("price_discrepancies") or [],
        "warnings": meta.get("warnings") or [],
        "overlay_stamp": stamp,
        "public_extract_sha256": sha256,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
