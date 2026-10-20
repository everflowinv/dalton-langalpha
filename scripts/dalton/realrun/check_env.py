#!/usr/bin/env python3
"""Pre-flight environment verification for Dalton real-run evidence (P0J §1.4, L40, L41).

Checks in order:
1. `identity`:
   - `INTERNAL_SERVICE_TOKEN` is set in environment / `.env`.
   - `GET /api/v1/dalton/openclaw/health` with `X-User-Id: realrun-pm-a` returns `user_id == "realrun-pm-a"`, `is_pm == True`.
   - `GET /api/v1/dalton/openclaw/health` with `X-User-Id: realrun-user-b` returns `user_id == "realrun-user-b"`, `is_pm == False`.
   - PM-only `POST /api/v1/dalton/openclaw/models/sync` (`{"dry_run": true}`) returns 200 for `realrun-pm-a` and 403 for `realrun-user-b`.
2. `version`:
   - Local working tree is clean (`git_dirty == False`).
   - Backend `git_sha` equals local `HEAD` (`git_sha`), and `backend_git_dirty == False`.
3. `conversation_transport`:
   - Sends a minimal `POST /api/v1/threads/messages` turn as `realrun-pm-a` using a subscription model (`gemini-3.8-flash-low-oauth`),
     verifying HTTP 200 (covering the Redis wake listener pool) and a `conversation_usages` row recorded under `user_id == "realrun-pm-a"` with `cash_usd == 0.0`.
4. `bridge`:
   - `http://127.0.0.1:8960/healthz` returns 200 and one real MCP `tools/call` read succeeds.
5. `broker`:
   - OpenClaw broker `/health` returns 200 (`ok: true`) and `/models` returns a non-empty model list.
6. `deployment`:
   - Reports whether the backend is running in `docker` or `host` mode.
7. `static_script_scan`:
   - Scans `scripts/dalton/realrun/g*.py` for forbidden `from src` imports, direct `INSERT INTO` SQL statements, `_for_check_env_probe`, and `*_fn=` preflight override arguments.
8. `oauth_token_provenance`:
   - Verifies that test user (`realrun-pm-a`) OAuth token in `user_oauth_tokens` has a prior `oauth_login_start` event in `dalton_provider_events` (`e.created_at <= t.created_at`), and that no `realrun-%` row in `user_oauth_tokens` lacks a prior `oauth_login_start` event (L39, P0J §1.5).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.realrun._common import (  # noqa: E402
    KNOWN_MIGRATED_WORKSPACES,
    RealRunHttpClient,
    assert_no_credentials_in_obj,
    build_realrun_workspace_config,
    get_git_sha,
    is_git_dirty,
    is_valid_realrun_workspace_row,
    load_repo_dotenv,
    now_utc_iso,
    resolve_local_dev_user_id,
)
from scripts.dalton.realrun.journal_dump import (  # noqa: E402
    build_dsn_from_env,
    dump_journal,
    format_iso_utc,
)

BRIDGE_ENV_FILE = Path.home() / ".config" / "dalton" / "researchdata-bridge.env"

_FROM_SRC_RE = re.compile(r"\b(?:from|import)\s+src\b")
_INSERT_INTO_RE = re.compile(r"\bINSERT\s+INTO\s+([a-zA-Z0-9_]+)", re.IGNORECASE)
_FOR_CHECK_ENV_PROBE_RE = re.compile(r"\b_for_check_env_probe\b")
_FN_OVERRIDE_RE = re.compile(r"\b[a-zA-Z0-9_]*_fn\s*=")


def _read_bridge_token() -> str:
    if BRIDGE_ENV_FILE.is_file():
        for line in BRIDGE_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DALTON_BRIDGE_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'").strip('"')
    return os.environ.get("DALTON_BRIDGE_TOKEN", "").strip()


def scan_realrun_scripts(
    script_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Scan `scripts/dalton/realrun/g*.py` for `from src`, `INSERT INTO`, `_for_check_env_probe`, and `*_fn=` (P0J §1.5)."""
    if script_paths is None:
        script_paths = sorted((REPO_ROOT / "scripts" / "dalton" / "realrun").glob("g*.py"))

    violations: list[dict[str, Any]] = []

    for path in script_paths:
        if not path.is_file():
            continue
        try:
            rel_path = path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            rel_path = path.as_posix()

        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _FROM_SRC_RE.search(line):
                violations.append(
                    {
                        "file": rel_path,
                        "line": lineno,
                        "kind": "from_src_import",
                        "snippet": stripped[:120],
                    }
                )
            m_ins = _INSERT_INTO_RE.search(line)
            if m_ins:
                table = m_ins.group(1)
                violations.append(
                    {
                        "file": rel_path,
                        "line": lineno,
                        "kind": f"insert_into:{table}",
                        "snippet": stripped[:120],
                    }
                )
            if _FOR_CHECK_ENV_PROBE_RE.search(line):
                violations.append(
                    {
                        "file": rel_path,
                        "line": lineno,
                        "kind": "forbidden_check_env_probe_call",
                        "snippet": stripped[:120],
                    }
                )
            if _FN_OVERRIDE_RE.search(line):
                violations.append(
                    {
                        "file": rel_path,
                        "line": lineno,
                        "kind": "forbidden_preflight_fn_override",
                        "snippet": stripped[:120],
                    }
                )

    return {
        "status": "PASS" if len(violations) == 0 else "FAIL",
        "scanned_files": len(script_paths),
        "violations": violations,
    }


def check_identity_and_version(base_url: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run checks #1 (identity), #2 (version), and #6 (deployment)."""
    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if not svc_token:
        raise RuntimeError("INTERNAL_SERVICE_TOKEN is not set in environment or .env (L40).")

    local_sha = get_git_sha(short=True)
    local_dirty = is_git_dirty()

    with RealRunHttpClient._for_check_env_probe(
        base_url=base_url, user_id="realrun-pm-a", timeout_s=30.0
    ) as pm_client, RealRunHttpClient._for_check_env_probe(
        base_url=base_url, user_id="realrun-user-b", timeout_s=30.0
    ) as user_b_client:
        pm_client.ensure_user()
        user_b_client.ensure_user()

        st_pm_h, body_pm_h = pm_client.get("/api/v1/dalton/openclaw/health")
        st_b_h, body_b_h = user_b_client.get("/api/v1/dalton/openclaw/health")

        st_pm_sync, body_pm_sync = pm_client.post(
            "/api/v1/dalton/openclaw/models/sync", json_body={"dry_run": True}
        )
        st_b_sync, body_b_sync = user_b_client.post(
            "/api/v1/dalton/openclaw/models/sync", json_body={"dry_run": True}
        )

    pm_uid = body_pm_h.get("user_id") if isinstance(body_pm_h, dict) else None
    pm_is_pm = body_pm_h.get("is_pm") if isinstance(body_pm_h, dict) else None
    b_uid = body_b_h.get("user_id") if isinstance(body_b_h, dict) else None
    b_is_pm = body_b_h.get("is_pm") if isinstance(body_b_h, dict) else None
    pm_source = (
        body_pm_h.get("pm_user_ids_source")
        if isinstance(body_pm_h, dict)
        else ("env" if os.getenv("DALTON_PM_USER_IDS", "").strip() else "default")
    )

    identity_ok = (
        st_pm_h == 200
        and st_b_h == 200
        and pm_uid == "realrun-pm-a"
        and pm_is_pm is True
        and b_uid == "realrun-user-b"
        and b_is_pm is False
        and st_pm_sync == 200
        and st_b_sync == 403
    )
    identity_result = {
        "status": "PASS" if identity_ok else "FAIL",
        "identity_mode": "service_token",
        "pm_user_ids_source": pm_source,
        "pm_user": {
            "requested_user_id": "realrun-pm-a",
            "returned_user_id": pm_uid,
            "is_pm": pm_is_pm,
            "pm_endpoint_status": st_pm_sync,
        },
        "non_pm_user": {
            "requested_user_id": "realrun-user-b",
            "returned_user_id": b_uid,
            "is_pm": b_is_pm,
            "pm_endpoint_status": st_b_sync,
        },
    }

    backend_sha = body_pm_h.get("git_sha") if isinstance(body_pm_h, dict) else "unknown"
    backend_dirty = bool(body_pm_h.get("git_dirty", True)) if isinstance(body_pm_h, dict) else True
    version_ok = (
        not local_dirty
        and not backend_dirty
        and bool(backend_sha)
        and backend_sha != "unknown"
        and backend_sha == local_sha
    )
    version_result = {
        "status": "PASS" if version_ok else "FAIL",
        "git_sha": local_sha,
        "git_dirty": local_dirty,
        "backend_git_sha": backend_sha,
        "backend_git_dirty": backend_dirty,
    }

    deployment_mode = (
        str(body_pm_h.get("deployment") or "")
        if isinstance(body_pm_h, dict)
        else ""
    )
    deployment_ok = deployment_mode in ("docker", "host")
    deployment_result = {
        "status": "PASS" if deployment_ok else "FAIL",
        "deployment": deployment_mode or "unknown",
        "g5_2_docker_ready": deployment_mode == "docker",
    }
    return identity_result, version_result, deployment_result


def check_oauth_token_provenance(
    user_id: str = "realrun-pm-a",
    provider: str = "antigravity-oauth",
    dsn: str | None = None,
) -> dict[str, Any]:
    """Verify test user OAuth token has a prior `oauth_login_start` event in `dalton_provider_events` (L39, P0J §1.5).

    Also checks that no `realrun-%` row in `user_oauth_tokens` lacks a prior `oauth_login_start` event.
    """
    try:
        conn_str = build_dsn_from_env(dsn)
        with psycopg.connect(conn_str, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.user_id,
                           t.provider,
                           t.created_at,
                           t.updated_at,
                           (
                               SELECT MIN(e.created_at)
                               FROM dalton_provider_events e
                               WHERE e.user_id = t.user_id
                                 AND e.provider = t.provider
                                 AND e.kind = 'oauth_login_start'
                                 AND e.created_at <= t.created_at
                           ) AS earliest_prior_login_start
                    FROM user_oauth_tokens t
                    WHERE t.user_id = %s AND t.provider = %s
                    """,
                    (user_id, provider),
                )
                row = cur.fetchone()

                cur.execute(
                    """
                    SELECT t.user_id,
                           t.provider,
                           t.created_at
                    FROM user_oauth_tokens t
                    WHERE t.user_id LIKE 'realrun-%%'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM dalton_provider_events e
                          WHERE e.user_id = t.user_id
                            AND e.provider = t.provider
                            AND e.kind = 'oauth_login_start'
                            AND e.created_at <= t.created_at
                      )
                    ORDER BY t.created_at, t.user_id, t.provider
                    """
                )
                unverified_rows = list(cur.fetchall())
    except Exception as exc:
        return {
            "status": "FAIL",
            "user_id": user_id,
            "provider": provider,
            "reason": "db_query_failed",
            "error": _safe_exc_summary(exc, "oauth_token_provenance database query failed"),
        }

    token_exists = row is not None
    prior_login_start = row.get("earliest_prior_login_start") if row else None
    has_prior_login_start = prior_login_start is not None
    ok = token_exists and has_prior_login_start and len(unverified_rows) == 0

    reason: str | None = None
    if not token_exists:
        reason = "missing_oauth_token_row"
    elif not has_prior_login_start:
        reason = "missing_prior_oauth_login_start_event"
    elif len(unverified_rows) > 0:
        reason = "unverified_realrun_oauth_token_rows_present"

    return {
        "status": "PASS" if ok else "FAIL",
        "user_id": user_id,
        "provider": provider,
        "token_exists": token_exists,
        "token_created_at": format_iso_utc(row["created_at"]) if row else None,
        "earliest_prior_login_start": format_iso_utc(prior_login_start) if prior_login_start is not None else None,
        "has_prior_oauth_login_start": has_prior_login_start,
        "unverified_test_token_rows": [
            {
                "user_id": str(r["user_id"]),
                "provider": str(r["provider"]),
                "created_at": format_iso_utc(r["created_at"]),
            }
            for r in unverified_rows
        ],
        "reason": reason,
    }


def _safe_exc_summary(exc: BaseException, context: str) -> str:
    """Return a credential-free exception summary containing only the exception type and a static description."""
    return f"{type(exc).__name__}: {context}"


def check_migrated_workspace_ownership(
    *,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Verify the 4 migrated workspaces (`META`, `AMZN`, `GOOGL`, `MSFT`) belong to `LOCAL_DEV_USER_ID` (P0J §8.1 #20)."""
    expected_owner = resolve_local_dev_user_id()
    migrated_map = dict(KNOWN_MIGRATED_WORKSPACES)
    expected_ids = list(migrated_map.values())
    try:
        conn_str = build_dsn_from_env(dsn)
        with psycopg.connect(conn_str, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workspace_id::text AS workspace_id,
                           user_id,
                           name,
                           status
                    FROM workspaces
                    WHERE workspace_id = ANY(%s::uuid[])
                      AND status <> 'deleted'
                    """,
                    (expected_ids,),
                )
                rows = list(cur.fetchall())
    except Exception as exc:
        return {
            "status": "FAIL",
            "expected_owner": expected_owner,
            "expected_workspaces": migrated_map,
            "reason": "db_query_failed",
            "error": _safe_exc_summary(exc, "migrated_workspace_ownership database query failed"),
        }

    by_id = {str(r.get("workspace_id") or "").strip().lower(): dict(r) for r in rows}
    missing_tickers: list[str] = []
    wrong_owner_workspaces: list[dict[str, Any]] = []
    verified_workspaces: dict[str, dict[str, Any]] = {}

    for ticker, ws_id in migrated_map.items():
        row = by_id.get(ws_id.lower())
        if row is None:
            missing_tickers.append(ticker)
            continue
        actual_owner = str(row.get("user_id") or "").strip()
        verified_workspaces[ticker] = {
            "workspace_id": ws_id,
            "user_id": actual_owner,
            "name": row.get("name"),
            "status": row.get("status"),
        }
        if actual_owner != expected_owner:
            wrong_owner_workspaces.append(
                {
                    "ticker": ticker,
                    "workspace_id": ws_id,
                    "expected_owner": expected_owner,
                    "actual_owner": actual_owner,
                }
            )

    ok = len(missing_tickers) == 0 and len(wrong_owner_workspaces) == 0
    reason: str | None = None
    if missing_tickers:
        reason = f"missing_migrated_workspaces:{','.join(missing_tickers)}"
    elif wrong_owner_workspaces:
        reason = "migrated_workspace_wrong_owner"

    return {
        "status": "PASS" if ok else "FAIL",
        "expected_owner": expected_owner,
        "expected_workspaces": migrated_map,
        "verified_workspaces": verified_workspaces,
        "missing_tickers": missing_tickers,
        "wrong_owner_workspaces": wrong_owner_workspaces,
        "reason": reason,
    }


def check_realrun_workspace_provenance(
    *,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Verify every workspace owned by `realrun-*` carries the evidence-script marker in `config` (P0J §8.1 #20)."""
    migrated_ids = {ws_id.lower() for ws_id in KNOWN_MIGRATED_WORKSPACES.values()}
    try:
        conn_str = build_dsn_from_env(dsn)
        with psycopg.connect(conn_str, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workspace_id::text AS workspace_id,
                           user_id,
                           name,
                           status,
                           config
                    FROM workspaces
                    WHERE user_id LIKE 'realrun-%%'
                      AND status <> 'deleted'
                    ORDER BY user_id, name, workspace_id
                    """
                )
                rows = list(cur.fetchall())
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": "db_query_failed",
            "error": _safe_exc_summary(exc, "realrun_workspace_provenance database query failed"),
        }

    valid_workspaces: list[dict[str, Any]] = []
    invalid_workspaces: list[dict[str, Any]] = []
    for row in rows:
        is_valid, marker_reason = is_valid_realrun_workspace_row(dict(row), migrated_ids=migrated_ids)
        entry = {
            "workspace_id": str(row.get("workspace_id") or ""),
            "user_id": str(row.get("user_id") or ""),
            "name": str(row.get("name") or ""),
            "status": str(row.get("status") or ""),
            "marker_check": marker_reason,
        }
        if is_valid:
            valid_workspaces.append(entry)
        else:
            invalid_workspaces.append(entry)

    ok = len(invalid_workspaces) == 0
    return {
        "status": "PASS" if ok else "FAIL",
        "realrun_workspaces_checked": len(rows),
        "valid_workspaces": valid_workspaces,
        "invalid_workspaces": invalid_workspaces,
        "reason": None if ok else str(invalid_workspaces[0]["marker_check"]),
    }


def _restore_budget_state(
    cleanup_client: RealRunHttpClient,
    *,
    user_id: str,
    was_configured: bool,
    prior_budget: dict[str, Any] | None,
    dsn: str | None = None,
) -> str | None:
    """Restore `user_id`'s budget state after `check_conversation_transport`; return error string on failure."""
    try:
        if was_configured:
            if not isinstance(prior_budget, dict) or prior_budget.get("daily_cap_usd") is None:
                return "budget_restore_missing_prior_daily_cap_usd"
            st_put, _ = cleanup_client.put(
                "/api/v1/dalton/budget",
                json_body={
                    "daily_cap_usd": float(prior_budget["daily_cap_usd"]),
                    "default_task_cap_usd": prior_budget.get("default_task_cap_usd"),
                    "timezone": str(prior_budget.get("timezone") or "Asia/Shanghai"),
                },
            )
            if st_put != 200:
                return f"budget_restore_put_http_{st_put}"
            return None

        conn_str = build_dsn_from_env(dsn)
        with psycopg.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM dalton_budget_config WHERE user_id = %s", (user_id,))
            conn.commit()
        st_get, body_get = cleanup_client.get("/api/v1/dalton/budget")
        if st_get != 200 or not isinstance(body_get, dict) or bool(body_get.get("configured")) is not False:
            return f"budget_restore_unconfigured_verify_failed:{st_get}"
        return None
    except Exception as exc:
        return f"budget_restore_exception:{type(exc).__name__}"


def _snapshot_api_keys_state(client: RealRunHttpClient) -> dict[str, Any]:
    """Snapshot `GET /api/v1/users/me/api-keys` state (without logging or storing raw keys)."""
    st, body = client.get("/api/v1/users/me/api-keys")
    if st != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET /api/v1/users/me/api-keys failed with HTTP {st}")
    providers = body.get("providers") or []
    ds_entry = next(
        (p for p in providers if isinstance(p, dict) and p.get("provider") == "deepseek"),
        {},
    )
    masked_key = ds_entry.get("masked_key")
    had_user_deepseek_key = bool(isinstance(masked_key, str) and masked_key and masked_key != "(server)")
    return {
        "byok_enabled": bool(body.get("byok_enabled")),
        "had_user_deepseek_key": had_user_deepseek_key,
    }


def _restore_api_keys_state(
    cleanup_client: RealRunHttpClient,
    *,
    prior_state: dict[str, Any] | None,
) -> str | None:
    """Restore user's BYOK `api-keys` state to `prior_state`; return error string on failure.

    When `prior_state["had_user_deepseek_key"]` is True, callers must not overwrite the
    user's pre-existing DeepSeek key at setup time (since `GET /api/v1/users/me/api-keys`
    only returns a masked key), so restoration is a no-op.
    """
    if not isinstance(prior_state, dict):
        return "api_keys_restore_missing_prior_state"
    try:
        prior_byok_enabled = bool(prior_state.get("byok_enabled"))
        had_user_ds_key = bool(prior_state.get("had_user_deepseek_key"))
        if had_user_ds_key:
            return None
        st_del, _ = cleanup_client.delete("/api/v1/users/me/api-keys/deepseek")
        st_put, body_put = cleanup_client.put(
            "/api/v1/users/me/api-keys",
            json_body={"byok_enabled": prior_byok_enabled, "api_keys": {"deepseek": None}},
        )
        if st_del != 200 or st_put != 200 or not isinstance(body_put, dict):
            return f"api_keys_restore_http_failed:del={st_del},put={st_put}"
        if bool(body_put.get("byok_enabled")) != prior_byok_enabled:
            return "api_keys_restore_byok_enabled_mismatch"
        ds_after = next(
            (
                p
                for p in (body_put.get("providers") or [])
                if isinstance(p, dict) and p.get("provider") == "deepseek"
            ),
            {},
        )
        if ds_after.get("masked_key") not in (None, "(server)"):
            return "api_keys_restore_deepseek_key_still_present"
        return None
    except Exception as exc:
        return f"api_keys_restore_exception:{type(exc).__name__}"


def _snapshot_subagent_state(
    client: RealRunHttpClient,
    name: str = "general-purpose",
) -> dict[str, Any]:
    """Snapshot a subagent's configuration from `GET /api/v1/subagents`."""
    st, body = client.get("/api/v1/subagents")
    if st != 200 or not isinstance(body, dict):
        raise RuntimeError(f"GET /api/v1/subagents failed with HTTP {st}")
    item = next(
        (s for s in (body.get("subagents") or []) if isinstance(s, dict) and s.get("name") == name),
        None,
    )
    if not isinstance(item, dict) or not item.get("version"):
        raise RuntimeError(f"Subagent {name!r} not found in GET /api/v1/subagents")
    return {
        "name": name,
        "version": str(item["version"]),
        "edited": bool(item.get("edited")),
        "user_fields": list(item.get("user_fields") or []),
        "model": item.get("model"),
    }


def _restore_subagent_state(
    cleanup_client: RealRunHttpClient,
    *,
    prior_state: dict[str, Any] | None,
) -> str | None:
    """Restore a subagent's `model` field to `prior_state`; return error string on failure."""
    if not isinstance(prior_state, dict):
        return "subagent_restore_missing_prior_state"
    name = str(prior_state.get("name") or "general-purpose")
    try:
        st_get, body_get = cleanup_client.get("/api/v1/subagents")
        if st_get != 200 or not isinstance(body_get, dict):
            return f"subagent_restore_get_http_{st_get}"
        cur_item = next(
            (s for s in (body_get.get("subagents") or []) if isinstance(s, dict) and s.get("name") == name),
            None,
        )
        if not isinstance(cur_item, dict) or not cur_item.get("version"):
            return f"subagent_restore_missing_{name}"
        cur_version = str(cur_item["version"])
        prior_user_fields = list(prior_state.get("user_fields") or [])
        prior_model = prior_state.get("model")
        target_model_patch = (prior_model or "inherit") if "model" in prior_user_fields else None
        st_put, body_put = cleanup_client.put(
            f"/api/v1/subagents/{name}",
            json_body={"version": cur_version, "fields": {"model": target_model_patch}},
        )
        if st_put != 200 or not isinstance(body_put, dict):
            return f"subagent_restore_put_http_{st_put}"
        if body_put.get("model") != prior_model:
            return "subagent_restore_model_mismatch"
        if ("model" in (body_put.get("user_fields") or [])) != ("model" in prior_user_fields):
            return "subagent_restore_user_fields_mismatch"
        return None
    except Exception as exc:
        return f"subagent_restore_exception:{type(exc).__name__}"


def check_conversation_transport(
    base_url: str,
    model: str = "gemini-3.8-flash-low-oauth",
    *,
    oauth_provenance: dict[str, Any] | None = None,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Run check #3: minimal `POST /api/v1/threads/messages` turn covering Redis wake listener and `conversation_usages`."""
    if model.endswith("-oauth"):
        prov = oauth_provenance or check_oauth_token_provenance(
            user_id="realrun-pm-a", provider="antigravity-oauth", dsn=dsn
        )
        if prov.get("status") != "PASS":
            return {
                "status": "FAIL",
                "model": model,
                "reason": f"oauth_token_provenance_failed:{prov.get('reason') or 'missing_prior_oauth_login_start'}",
                "oauth_token_provenance_status": prov.get("status"),
                "error": (
                    f"Refusing to run subscription transport probe for realrun-pm-a/{model}: "
                    f"OAuth token provenance check failed ({prov.get('reason')})"
                ),
            }

    t0 = now_utc_iso()
    ws_id: str | None = None
    prior_budget: dict[str, Any] | None = None
    was_configured = False
    budget_modified = False
    budget_restore_error: str | None = None
    workspace_cleanup_error: str | None = None
    restore_error: str | None = None
    early_fail: dict[str, Any] | None = None
    st_msg = 0
    events: list[dict[str, Any]] = []
    err_body: Any = None
    try:
        with RealRunHttpClient._for_check_env_probe(
            base_url=base_url, user_id="realrun-pm-a", timeout_s=120.0
        ) as client:
            st_b, body_b = client.get("/api/v1/dalton/budget")
            st_bt, body_bt = client.get("/api/v1/dalton/budget/today")
            if st_b == 200 and isinstance(body_b, dict):
                prior_budget = body_b
                was_configured = bool(body_b.get("configured"))
            remaining_usd = (
                float(body_bt.get("remaining_usd") or 0.0)
                if st_bt == 200 and isinstance(body_bt, dict)
                else 0.0
            )
            spent_usd = (
                float(body_bt.get("spent_usd") or 0.0)
                if st_bt == 200 and isinstance(body_bt, dict)
                else 0.0
            )
            # Only set a temporary budget if realrun-pm-a has none configured or remaining_usd <= 0
            if not was_configured or remaining_usd <= 0.0:
                probe_cap = max(5.0, spent_usd + 1.0)
                st_put_tmp, _ = client.put(
                    "/api/v1/dalton/budget",
                    json_body={"daily_cap_usd": probe_cap, "default_task_cap_usd": 5.0},
                )
                if st_put_tmp != 200:
                    early_fail = {
                        "status": "FAIL",
                        "reason": f"temporary_budget_put_failed:{st_put_tmp}",
                        "http_status": st_put_tmp,
                    }
                else:
                    budget_modified = True

            if early_fail is None:
                st_ws, body_ws = client.post(
                    "/api/v1/workspaces",
                    json_body={
                        "name": "check-env-transport",
                        "description": "P0J §1.4 check_env transport probe",
                        "config": build_realrun_workspace_config("check_env.py"),
                    },
                )
                if st_ws not in (200, 201) or not isinstance(body_ws, dict):
                    early_fail = {
                        "status": "FAIL",
                        "reason": f"workspace_create_failed:{st_ws}",
                        "http_status": st_ws,
                    }
                else:
                    ws_id = str(body_ws.get("workspace_id") or "")
                    try:
                        st_msg, events, err_body = client.stream_sse(
                            "POST",
                            "/api/v1/threads/messages",
                            json_body={
                                "workspace_id": ws_id,
                                "messages": [{"role": "user", "content": "Reply with ONLY the single word: PONG"}],
                                "llm_model": model,
                                "agent_mode": "flash",
                            },
                            max_events=300,
                        )
                    except Exception as exc:
                        early_fail = {
                            "status": "FAIL",
                            "reason": f"stream_sse_exception:{type(exc).__name__}",
                            "error": _safe_exc_summary(exc, "conversation_transport stream_sse failed"),
                        }
    except Exception as exc:
        early_fail = {
            "status": "FAIL",
            "reason": f"probe_request_exception:{type(exc).__name__}",
            "error": _safe_exc_summary(exc, "conversation_transport request failed"),
        }
    finally:
        if ws_id or budget_modified:
            cleanup_errors: list[str] = []
            try:
                with RealRunHttpClient._for_check_env_probe(
                    base_url=base_url, user_id="realrun-pm-a", timeout_s=30.0
                ) as cleanup_client:
                    if ws_id:
                        try:
                            st_del_ws, _ = cleanup_client.delete(f"/api/v1/workspaces/{ws_id}")
                            if st_del_ws not in (200, 204, 404):
                                workspace_cleanup_error = f"workspace_delete_http_{st_del_ws}"
                                cleanup_errors.append(workspace_cleanup_error)
                        except Exception as exc:
                            workspace_cleanup_error = f"workspace_delete_exception:{type(exc).__name__}"
                            cleanup_errors.append(workspace_cleanup_error)
                    if budget_modified:
                        try:
                            err_b = _restore_budget_state(
                                cleanup_client,
                                user_id="realrun-pm-a",
                                was_configured=was_configured,
                                prior_budget=prior_budget,
                                dsn=dsn,
                            )
                            if err_b:
                                budget_restore_error = err_b
                                cleanup_errors.append(err_b)
                        except Exception as exc:
                            budget_restore_error = f"budget_restore_exception:{type(exc).__name__}"
                            cleanup_errors.append(budget_restore_error)
            except Exception as exc:
                cleanup_exc_msg = f"cleanup_exception:{type(exc).__name__}"
                if ws_id and workspace_cleanup_error is None:
                    workspace_cleanup_error = cleanup_exc_msg
                if budget_modified and budget_restore_error is None:
                    budget_restore_error = cleanup_exc_msg
                cleanup_errors.append(cleanup_exc_msg)
            if cleanup_errors:
                restore_error = "; ".join(cleanup_errors)

    if early_fail is not None:
        early_fail["budget_restored"] = budget_restore_error is None
        early_fail["budget_restore_error"] = budget_restore_error
        early_fail["workspace_cleaned"] = workspace_cleanup_error is None
        early_fail["workspace_cleanup_error"] = workspace_cleanup_error
        early_fail["restore_error"] = restore_error
        if restore_error:
            base_reason = str(early_fail.get("reason") or "probe_failed")
            early_fail["reason"] = f"{base_reason}; restore_error={restore_error}"
            early_fail["error"] = early_fail.get("error") or restore_error
        return early_fail

    # Allow 0.5s for async usage persistence to commit
    time.sleep(0.5)
    t1 = now_utc_iso()
    j = dump_journal(user_id="realrun-pm-a", since=t0, until=t1)
    conv_rows = j.get("conversation_usages") or []
    pm_rows = [r for r in conv_rows if r.get("user_id") == "realrun-pm-a"]
    unexpected_users = j.get("totals", {}).get("unexpected_user_ids") or []
    probe_cash_usd = round(sum(float(r.get("cash_usd") or 0.0) for r in pm_rows), 6)

    ok = (
        st_msg == 200
        and len(events) > 0
        and len(pm_rows) >= 1
        and probe_cash_usd == 0.0
        and len(unexpected_users) == 0
        and not j.get("totals", {}).get("conversation_usages_unavailable", False)
        and restore_error is None
    )
    return {
        "status": "PASS" if ok else "FAIL",
        "model": model,
        "http_status": st_msg,
        "sse_events_count": len(events),
        "conversation_usages_count": len(pm_rows),
        "cash_usd": probe_cash_usd,
        "user_id_verified": len(pm_rows) >= 1 and all(r.get("user_id") == "realrun-pm-a" for r in pm_rows),
        "budget_restored": budget_restore_error is None,
        "budget_restore_error": budget_restore_error,
        "workspace_cleaned": workspace_cleanup_error is None,
        "workspace_cleanup_error": workspace_cleanup_error,
        "restore_error": restore_error,
        "error": err_body if st_msg != 200 else restore_error,
    }


def check_bridge(bridge_url: str = "http://127.0.0.1:8960") -> dict[str, Any]:
    """Run check #4: bridge `/healthz` and one real MCP read."""
    token = _read_bridge_token()
    if not token:
        return {"status": "FAIL", "reason": "missing_bridge_token"}
    try:
        with httpx.Client(timeout=30.0, trust_env=False) as c:
            h_resp = c.get(f"{bridge_url.rstrip('/')}/healthz")
            rpc_resp = c.post(
                f"{bridge_url.rstrip('/')}/mcp/{token}",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "catalog",
                        "arguments": {},
                    },
                },
            )
        rpc_body = rpc_resp.json() if rpc_resp.status_code == 200 else {}
        blocks = ((rpc_body.get("result") or {}).get("content") or []) if isinstance(rpc_body, dict) else []
        ok = h_resp.status_code == 200 and rpc_resp.status_code == 200 and len(blocks) > 0
        return {
            "status": "PASS" if ok else "FAIL",
            "bridge_url": bridge_url,
            "healthz_status": h_resp.status_code,
            "mcp_read_status": rpc_resp.status_code,
            "mcp_content_blocks": len(blocks),
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "bridge_url": bridge_url,
            "error": _safe_exc_summary(exc, "bridge healthz or MCP read failed"),
        }


def check_broker(broker_url: str | None = None) -> dict[str, Any]:
    """Run check #5: OpenClaw broker `/health` and `/models?detail=1`."""
    url = (
        broker_url
        or os.getenv("DALTON_BROKER_URL")
        or "http://127.0.0.1:18789/dalton-broker/v1"
    ).rstrip("/")
    token = os.getenv("DALTON_BROKER_TOKEN", "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=25.0, trust_env=False) as c:
            h_resp = c.get(f"{url}/health", headers=headers)
            m_resp = c.get(f"{url}/models?detail=1", headers=headers)
        h_body = h_resp.json() if h_resp.status_code == 200 else {}
        m_body = m_resp.json() if m_resp.status_code == 200 else {}
        models_list = (
            (m_body.get("data") or m_body.get("models"))
            if isinstance(m_body, dict)
            else []
        )
        ok = (
            h_resp.status_code == 200
            and bool(h_body.get("ok"))
            and m_resp.status_code == 200
            and isinstance(models_list, list)
            and len(models_list) > 0
        )
        return {
            "status": "PASS" if ok else "FAIL",
            "broker_url": url,
            "health_status": h_resp.status_code,
            "models_status": m_resp.status_code,
            "models_count": len(models_list) if isinstance(models_list, list) else 0,
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "broker_url": url,
            "error": _safe_exc_summary(exc, "broker health or models request failed"),
        }


def run_all_checks(
    *,
    backend_url: str = "http://127.0.0.1:8000",
    bridge_url: str = "http://127.0.0.1:8960",
    broker_url: str | None = None,
    transport_model: str = "gemini-3.8-flash-low-oauth",
    strict_static_scan: bool = False,
    skip_oauth_and_transport: bool = False,
) -> dict[str, Any]:
    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if not svc_token:
        raise RuntimeError(
            "INTERNAL_SERVICE_TOKEN is not set in environment or .env (P0J §1.2, L40). "
            "Refusing to run check_env without service-token authentication."
        )

    broker_res = check_broker(broker_url)
    identity_res, version_res, deployment_res = check_identity_and_version(backend_url)
    migrated_ws_res = check_migrated_workspace_ownership()
    realrun_ws_res = check_realrun_workspace_provenance()
    bridge_res = check_bridge(bridge_url)
    static_res = scan_realrun_scripts()

    checks: dict[str, dict[str, Any]] = {
        "identity": identity_res,
        "version": version_res,
        "migrated_workspace_ownership": migrated_ws_res,
        "realrun_workspace_provenance": realrun_ws_res,
        "bridge": bridge_res,
        "broker": broker_res,
        "deployment": deployment_res,
        "static_script_scan": static_res,
    }
    if not skip_oauth_and_transport:
        oauth_prov_res = check_oauth_token_provenance(user_id="realrun-pm-a", provider="antigravity-oauth")
        transport_res = check_conversation_transport(
            backend_url, model=transport_model, oauth_provenance=oauth_prov_res
        )
        checks["oauth_token_provenance"] = oauth_prov_res
        checks["conversation_transport"] = transport_res

    failed = [name for name, item in checks.items() if item.get("status") != "PASS"]
    summary = {
        "checked_at": now_utc_iso(),
        "all_passed": len(failed) == 0,
        "pass_count": sum(1 for item in checks.values() if item.get("status") == "PASS"),
        "fail_count": len(failed),
        "failed_checks": failed,
        "git_sha": version_res.get("git_sha"),
        "git_dirty": version_res.get("git_dirty"),
        "backend_git_sha": version_res.get("backend_git_sha"),
        "backend_git_dirty": version_res.get("backend_git_dirty"),
        "deployment": deployment_res.get("deployment"),
        "identity_mode": "service_token",
        "pm_user_ids_source": identity_res.get("pm_user_ids_source"),
        "checks": checks,
    }
    assert_no_credentials_in_obj(summary)
    return summary


def main() -> int:
    load_repo_dotenv()
    parser = argparse.ArgumentParser(description="Verify Dalton real-run environment (P0J §1.4).")
    parser.add_argument("--backend", default="http://127.0.0.1:8000", help="Backend base URL")
    parser.add_argument("--bridge", default="http://127.0.0.1:8960", help="ResearchData bridge base URL")
    parser.add_argument("--broker", default=None, help="OpenClaw broker base URL")
    parser.add_argument(
        "--model",
        default="gemini-3.8-flash-low-oauth",
        help="Subscription model for transport probe (cash_usd == 0)",
    )
    parser.add_argument("--strict-static-scan", action="store_true", help="Fail on pre-J3 legacy script items")
    args = parser.parse_args()

    try:
        summary = run_all_checks(
            backend_url=args.backend,
            bridge_url=args.bridge,
            broker_url=args.broker,
            transport_model=args.model,
            strict_static_scan=args.strict_static_scan,
        )
    except Exception as exc:
        err_msg = (
            str(exc)
            if isinstance(exc, RuntimeError) and "INTERNAL_SERVICE_TOKEN" in str(exc)
            else _safe_exc_summary(exc, "check_env execution failed")
        )
        print(json.dumps({"all_passed": False, "error": err_msg}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
