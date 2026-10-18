#!/usr/bin/env python3
"""Host-side script to initialize Dalton company workspaces via LangAlpha REST API (or local directory).

Dynamically imports `plugins/dalton/skills/dalton-init-company/scripts/init_company.py`
via `importlib` so both host-side REST initialization and in-sandbox skill execution
share a single template renderer and idempotency engine.

Usage:
  uv run python scripts/dalton/init_company_workspace.py --tickers META,AMZN,GOOGL,MSFT [--api http://localhost:8000] [--apply]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT_COMPANY_SCRIPT = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-init-company" / "scripts" / "init_company.py"
)
DEFAULT_COVERAGE_YAML = REPO_ROOT / "config" / "dalton" / "coverage.yaml"


def _load_init_company_module():
    spec = importlib.util.spec_from_file_location("dalton_init_company", INIT_COMPANY_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {INIT_COMPANY_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_coverage_map(coverage_yaml: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    if not coverage_yaml.is_file():
        return "2026-10-09", {}
    doc = yaml.safe_load(coverage_yaml.read_text(encoding="utf-8")) or {}
    as_of = str(doc.get("as_of") or "2026-10-09")
    by_ticker: dict[str, dict[str, Any]] = {}
    for comp in doc.get("companies") or []:
        t = str(comp.get("ticker") or "").upper()
        if t:
            entry = dict(comp)
            entry["as_of"] = as_of
            by_ticker[t] = entry
    return as_of, by_ticker


def _api_request(
    api_base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    url = f"{api_base.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {"detail": raw}
        return e.code, parsed


def _read_remote_file(api_base: str, workspace_id: str, relpath: str) -> str | None:
    encoded = urllib.parse.quote(relpath, safe="")
    status, body = _api_request(
        api_base, "GET", f"/api/v1/workspaces/{workspace_id}/files/read?path={encoded}"
    )
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"Failed reading {relpath} in workspace {workspace_id}: HTTP {status} {body}")
    if isinstance(body, dict) and "content" in body:
        return str(body["content"])
    return None


def _write_remote_file(api_base: str, workspace_id: str, relpath: str, content: str) -> None:
    encoded = urllib.parse.quote(relpath, safe="")
    status, body = _api_request(
        api_base,
        "PUT",
        f"/api/v1/workspaces/{workspace_id}/files/write?path={encoded}",
        payload={"content": content},
    )
    if status not in (200, 201, 204):
        raise RuntimeError(f"Failed writing {relpath} in workspace {workspace_id}: HTTP {status} {body}")


def _init_via_rest(
    init_mod: Any,
    api_base: str,
    ticker: str,
    entry: dict[str, Any],
    *,
    apply_changes: bool,
) -> dict[str, Any]:
    tier_raw = str(entry.get("tier") or "watch").lower()
    tier_display = "Core" if tier_raw == "core" else "Watch"
    name = str(entry.get("name") or ticker)
    description = f"{name} · {tier_display}"

    if not apply_changes:
        files = init_mod.render(ticker, entry, init_mod.TEMPLATE_VERSION)
        return {
            "ticker": ticker,
            "mode": "rest_dry_run",
            "workspace_name": ticker,
            "workspace_description": description,
            "planned_files": list(files.keys()) + [".dalton/init.json"],
            "writes_count": 0,
        }

    # 1. List existing workspaces to deduplicate by name and find shared computer_id
    status, ws_list_resp = _api_request(api_base, "GET", "/api/v1/workspaces")
    if status != 200:
        raise RuntimeError(f"GET /api/v1/workspaces failed: HTTP {status} {ws_list_resp}")
    workspaces = (
        ws_list_resp.get("workspaces", ws_list_resp.get("items", []))
        if isinstance(ws_list_resp, dict)
        else ws_list_resp
    )

    existing_ws = None
    shared_computer_id = None
    for ws in workspaces or []:
        if not isinstance(ws, dict):
            continue
        if ws.get("computer_id") and not shared_computer_id:
            shared_computer_id = ws["computer_id"]
        if str(ws.get("name") or "").upper() == ticker.upper():
            existing_ws = ws

    if existing_ws is not None:
        workspace_id = existing_ws.get("workspace_id") or existing_ws.get("id")
        ws_created = False
    else:
        create_payload: dict[str, Any] = {"name": ticker, "description": description}
        if shared_computer_id:
            create_payload["computer_id"] = shared_computer_id
        c_status, created_ws = _api_request(api_base, "POST", "/api/v1/workspaces", payload=create_payload)
        if c_status not in (200, 201):
            raise RuntimeError(f"POST /api/v1/workspaces for {ticker} failed: HTTP {c_status} {created_ws}")
        workspace_id = created_ws.get("workspace_id") or created_ws.get("id")
        ws_created = True

    # 2. Ensure workspace sandbox is started and ready
    s_status, s_body = _api_request(
        api_base,
        "POST",
        f"/api/v1/workspaces/{workspace_id}/start?lazy=false",
        payload={},
        timeout=120,
    )
    if s_status not in (200, 201, 204, 409):
        raise RuntimeError(f"Failed starting workspace {ticker} ({workspace_id}): HTTP {s_status} {s_body}")

    # 3. Read existing .dalton/init.json if present
    manifest_text = _read_remote_file(api_base, workspace_id, ".dalton/init.json")
    existing_manifest: dict[str, Any] = {}
    init_date: str | None = None
    if manifest_text:
        try:
            existing_manifest = json.loads(manifest_text)
            created_str = str(existing_manifest.get("created_at") or "")
            if len(created_str) >= 10:
                init_date = created_str[:10]
        except Exception:
            existing_manifest = {}

    files = init_mod.render(ticker, entry, init_mod.TEMPLATE_VERSION, init_date=init_date)
    recorded_version = existing_manifest.get("template_version")
    recorded_files: dict[str, dict[str, str]] = dict(existing_manifest.get("files") or {})
    created_at = existing_manifest.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_manifest_files: dict[str, dict[str, str]] = dict(recorded_files)

    report: dict[str, Any] = {
        "ticker": ticker,
        "workspace_id": workspace_id,
        "workspace_created": ws_created,
        "written": [],
        "updated": [],
        "unchanged": [],
        "managed_block_appended": [],
        "skipped_modified": [],
        "writes_count": 0,
    }

    for relpath, target_content in files.items():
        current_content = _read_remote_file(api_base, workspace_id, relpath)
        rec_sha = (recorded_files.get(relpath) or {}).get("sha256")
        action_status, content_to_write, sha_for_manifest = init_mod.classify_file_action(
            relpath=relpath,
            target_content=target_content,
            current_content=current_content,
            recorded_sha=rec_sha,
            recorded_template_version=recorded_version,
            template_version=init_mod.TEMPLATE_VERSION,
        )
        report[action_status].append(relpath)
        new_manifest_files[relpath] = {"sha256": sha_for_manifest}
        if content_to_write is not None:
            _write_remote_file(api_base, workspace_id, relpath, content_to_write)
            report["writes_count"] += 1

    new_manifest = {
        "template_version": init_mod.TEMPLATE_VERSION,
        "created_at": created_at,
        "files": new_manifest_files,
    }
    serialized_manifest = json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n"
    if manifest_text != serialized_manifest:
        _write_remote_file(api_base, workspace_id, ".dalton/init.json", serialized_manifest)
        report["writes_count"] += 1
        report["manifest_written"] = True
    else:
        report["manifest_written"] = False

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize Dalton company workspaces via REST or local dir")
    parser.add_argument(
        "--tickers",
        required=True,
        help="Comma-separated list of canonical tickers (e.g. META,AMZN,GOOGL,MSFT)",
    )
    parser.add_argument("--api", default="http://localhost:8000", help="LangAlpha REST API base URL")
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE_YAML, help="Path to coverage.yaml")
    parser.add_argument(
        "--local-root",
        type=Path,
        default=None,
        help="Optional local parent directory (<local-root>/<TICKER>/) for offline testing without REST",
    )
    parser.add_argument("--allow-non-us", action="store_true", help="Allow HK/CN tickers (default: US only in P1)")
    parser.add_argument("--apply", action="store_true", help="Execute writes (default is dry-run)")
    args = parser.parse_args(argv)

    init_mod = _load_init_company_module()
    as_of, coverage_map = _load_coverage_map(args.coverage)

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    results: list[dict[str, Any]] = []

    for ticker in tickers:
        entry = coverage_map.get(ticker, {"ticker": ticker, "name": ticker, "tier": "watch", "market": "US", "as_of": as_of})
        market = str(entry.get("market") or "US")
        if market != "US" and not args.allow_non_us:
            print(
                f"ERROR: {ticker} has market={market}; P1 only initializes US workspaces (pass --allow-non-us to override).",
                file=sys.stderr,
            )
            return 1

        if args.local_root is not None:
            ws_dir = args.local_root / ticker
            init_date = None
            manifest_path = ws_dir / ".dalton" / "init.json"
            if manifest_path.is_file():
                try:
                    m_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    c_at = str(m_data.get("created_at") or "")
                    if len(c_at) >= 10:
                        init_date = c_at[:10]
                except Exception:
                    pass
            rendered = init_mod.render(ticker, entry, init_mod.TEMPLATE_VERSION, init_date=init_date)
            res = init_mod.apply(
                ws_dir, rendered, template_version=init_mod.TEMPLATE_VERSION, dry_run=not args.apply
            )
            res_clean = {k: v for k, v in res.items() if k != "manifest"}
            res_clean["ticker"] = ticker
            results.append(res_clean)
        else:
            res = _init_via_rest(init_mod, args.api, ticker, entry, apply_changes=args.apply)
            results.append(res)

    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
