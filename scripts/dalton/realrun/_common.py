#!/usr/bin/env python3
"""Shared utilities for Dalton real-run evidence scripts (P0I §2.1–§2.2, P0J §1.2–§1.6, L37, L40–L42).

Responsibilities:
1. Build the mandatory v2 evidence envelope (`evidence_version: 2`, `git_sha`, `git_dirty`,
   `backend_git_sha`, `backend_git_dirty`, `script_path`, `script_sha256`, `started_at`,
   `ended_at`, `backend`, `test_user_id`, `identity_mode`, `pm_user_ids_source`, `deployment`,
   `environment`, `synthetic` / `synthetic_seed`, `request_trace`, `journal` / `llm_journal`,
   `conversation_usages`, `assertions`, `summary_title`, `key_findings`, `notes`,
   `valid`, `invalid_reasons`).
2. Validate `synthetic` declarations against the 6 allowed injection categories and the
   explicit `COMMITTED_TEST_HOOKS` registry with exact category/entry_point matching (P0J §1.5, L37).
3. Enforce clean working tree (excluding no tracked directories), merged-into-main ancestry,
   live backend version parity, live `check_env` pass, and `INTERNAL_SERVICE_TOKEN`
   per-user identity self-check (L40, L41).
4. Support `--record`: without `--record`, perform dry-run verification without writing any files;
   with `--record`, write raw evidence JSON and runtime summary Markdown exclusively under `var/dalton/`,
   and provide a one-shot command to render summaries from `var/dalton/` into
   `docs/dalton/dev-guide/trial-runs/realrun-<YYYYMMDD>/G<N>.md` after all families complete (L42).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import uuid

from dotenv import load_dotenv
import httpx

from scripts.dalton.realrun.journal_dump import dump_journal, format_iso_utc

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env", override=False)

ALLOWED_SYNTHETIC_KINDS = frozenset(
    {
        "settings_api_budget",
        "knowledge_api_agenda",
        "knowledge_api_import_fixture",
        "watch_state_unsee_filing",
        "committed_test_hook",
        "committed_fixture_input",
    }
)

COMMITTED_TEST_HOOKS: dict[str, dict[str, str]] = {
    "scripts/dalton/researchdata_bridge.py": {
        "module": "scripts/dalton/researchdata_bridge.py",
        "code_location": "scripts/dalton/researchdata_bridge.py:168-190",
        "flags": "--simulate-upstream / DALTON_BRIDGE_TEST_MODE / DALTON_BRIDGE_TEST_DELAY_S",
        "description": "Simulate >240s upstream delay or synthetic upstream errors on researchdata_bridge.py (G7.A / G7.C).",
    },
    "integrations/openclaw/dalton-langalpha-broker": {
        "module": "integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs",
        "code_location": "integrations/openclaw/dalton-langalpha-broker/src/invoke.mjs:287-382",
        "flags": "replayOnly / crash-before-clear",
        "description": "Broker v2 idempotency journal pending/indeterminate test hook (G5.A).",
    },
}

FORBIDDEN_SYNTHETIC_SUBSTRINGS = (
    "direct_signal_insert",
    "direct_journal_insert",
    "direct_budget_insert",
    "preflight_check_direct",
    "enforce_local_admission_direct",
    "registered_test_hook",
    "cached_llm_replay",
)

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{20,}|xai-[A-Za-z0-9_\-]{20,}|gho_[A-Za-z0-9_]{20,}|ya29\.[A-Za-z0-9_\-.]{20,})"
)

_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

_SETTINGS_BUDGET_RE = re.compile(r"^(?:PUT\s+)?/api/v1/dalton/budget$")
_KNOWLEDGE_AGENDA_RE = re.compile(
    r"^(?:(?:(?:POST|PUT)\s+)?/api/v1/dalton/knowledge/[^/\s]+/agenda(?:/(?:import|items))?|dalton_agenda_(?:note|update))$"
)
_KNOWLEDGE_IMPORT_ROUTE_RE = re.compile(
    r"(?:POST\s+)?/api/v1/dalton/knowledge/[^/\s]+/(?:[A-Za-z0-9_{}-]+/)?import\b"
)
_FIXTURE_REL_PATH_RE = re.compile(r"\b(tests/fixtures/[A-Za-z0-9_./\-]+\.json)\b")
_WATCH_UNSEE_RE = re.compile(r"^dalton_watch_state(?:\.state)?[.:]seen_accessions$")

_GLOBAL_REQUEST_TRACE: list[dict[str, Any]] = []


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_git_sha(short: bool = True) -> str:
    cmd = ["git", "-c", "safe.directory=*", "--no-optional-locks", "-C", str(REPO_ROOT), "rev-parse"]
    if short:
        cmd.append("--short=8")
    cmd.append("HEAD")
    return subprocess.check_output(cmd, text=True).strip()


def is_git_dirty() -> bool:
    """Return True if working tree has any uncommitted changes (no directory exclusions; `var/` is gitignored)."""
    raw = subprocess.check_output(
        ["git", "-c", "safe.directory=*", "--no-optional-locks", "-C", str(REPO_ROOT), "status", "--porcelain"],
        text=True,
    )
    return bool(raw.strip())


def is_head_merged_into_main(ref: str = "origin/dalton/main") -> bool:
    """Return True if HEAD is an ancestor of (or equal to) `origin/dalton/main` (or `dalton/main`)."""
    for candidate in (ref, "dalton/main"):
        res = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "--no-optional-locks",
                "-C",
                str(REPO_ROOT),
                "merge-base",
                "--is-ancestor",
                "HEAD",
                candidate,
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return True
    return False


def is_main_ancestor_of_head(ref: str = "origin/dalton/main") -> bool:
    """Return True if `origin/dalton/main` is an ancestor of HEAD (or HEAD is an ancestor of `origin/dalton/main`)."""
    if is_head_merged_into_main(ref):
        return True
    for candidate in (ref, "dalton/main"):
        res = subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "--no-optional-locks",
                "-C",
                str(REPO_ROOT),
                "merge-base",
                "--is-ancestor",
                candidate,
                "HEAD",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return True
    return False


def _validate_entry_point_for_kind(
    kind: str,
    entry_point: str,
    item: dict[str, Any] | None = None,
    *,
    repo_root: Path = REPO_ROOT,
) -> None:
    ep = (entry_point or "").strip()
    if not ep:
        raise ValueError(f"Synthetic declaration for kind '{kind}' must specify a non-empty 'entry_point'")

    if kind == "settings_api_budget":
        if not _SETTINGS_BUDGET_RE.match(ep):
            raise ValueError(
                f"Synthetic kind 'settings_api_budget' requires exact entry_point 'PUT /api/v1/dalton/budget', got: {ep!r}"
            )
    elif kind == "knowledge_api_agenda":
        if not _KNOWLEDGE_AGENDA_RE.match(ep):
            raise ValueError(
                f"Synthetic kind 'knowledge_api_agenda' requires exact knowledge agenda endpoint or tool "
                f"('POST /api/v1/dalton/knowledge/{{ws}}/agenda/import' or 'dalton_agenda_update'), got: {ep!r}"
            )
    elif kind == "knowledge_api_import_fixture":
        if not _KNOWLEDGE_IMPORT_ROUTE_RE.search(ep):
            raise ValueError(
                f"Synthetic kind 'knowledge_api_import_fixture' requires knowledge import endpoint in entry_point, got: {ep!r}"
            )
        fixture_candidates = list(_FIXTURE_REL_PATH_RE.findall(ep))
        if item and isinstance(item.get("fixture_path"), str) and item["fixture_path"].strip():
            fixture_candidates.append(item["fixture_path"].strip())
        if item and isinstance(item.get("fixture_paths"), list):
            fixture_candidates.extend(str(x).strip() for x in item["fixture_paths"] if str(x).strip())
        if not fixture_candidates:
            raise ValueError(
                f"Synthetic kind 'knowledge_api_import_fixture' must specify a committed file under 'tests/fixtures/' "
                f"(in entry_point or fixture_path), got: {ep!r}"
            )
        for rel_fix in fixture_candidates:
            if not rel_fix.startswith("tests/fixtures/"):
                raise ValueError(
                    f"Synthetic kind 'knowledge_api_import_fixture' fixture must reside under 'tests/fixtures/', got: {rel_fix!r}"
                )
            if not (repo_root / rel_fix).is_file():
                raise ValueError(
                    f"Synthetic kind 'knowledge_api_import_fixture' fixture file does not exist: {rel_fix!r}"
                )
    elif kind == "watch_state_unsee_filing":
        if not _WATCH_UNSEE_RE.match(ep):
            raise ValueError(
                f"Synthetic kind 'watch_state_unsee_filing' requires exact 'dalton_watch_state.state.seen_accessions', got: {ep!r}"
            )
    elif kind == "committed_test_hook":
        matched_hook = any(
            ep == hook_key or ep.startswith(f"{hook_key} ") or ep.startswith(f"{hook_key}:")
            for hook_key in COMMITTED_TEST_HOOKS
        )
        if not matched_hook:
            raise ValueError(
                f"Synthetic kind 'committed_test_hook' entry_point {ep!r} is not registered in "
                f"COMMITTED_TEST_HOOKS {sorted(COMMITTED_TEST_HOOKS.keys())}"
            )
        for hook_key, meta in COMMITTED_TEST_HOOKS.items():
            if not meta.get("code_location"):
                raise ValueError(f"COMMITTED_TEST_HOOKS[{hook_key!r}] is missing mandatory 'code_location'")
    elif kind == "committed_fixture_input":
        if not _FIXTURE_REL_PATH_RE.fullmatch(ep):
            raise ValueError(
                f"Synthetic kind 'committed_fixture_input' requires a committed path under 'tests/fixtures/*.json', got: {ep!r}"
            )
        if not (repo_root / ep).is_file():
            raise ValueError(
                f"Synthetic kind 'committed_fixture_input' file does not exist in repository: {ep!r}"
            )


def validate_synthetic_declarations(
    synthetic: list[Any],
    *,
    repo_root: Path = REPO_ROOT,
) -> list[dict[str, Any] | str]:
    """Validate `synthetic` list against P0G §0.2 / P0J §1.5 6 allowed injection categories and COMMITTED_TEST_HOOKS."""
    validated: list[dict[str, Any] | str] = []
    for item in synthetic:
        if isinstance(item, str):
            raw_str = item.strip()
            for bad in FORBIDDEN_SYNTHETIC_SUBSTRINGS:
                if bad in raw_str:
                    raise ValueError(f"Forbidden synthetic injection declared: {raw_str}")
            parts = raw_str.split(":", 1)
            kind_prefix = parts[0].strip()
            entry_point = parts[1].strip() if len(parts) > 1 else ""
            if kind_prefix not in ALLOWED_SYNTHETIC_KINDS:
                raise ValueError(
                    f"Synthetic kind '{kind_prefix}' is not in ALLOWED_SYNTHETIC_KINDS "
                    f"{sorted(ALLOWED_SYNTHETIC_KINDS)}"
                )
            _validate_entry_point_for_kind(kind_prefix, entry_point, repo_root=repo_root)
            validated.append(raw_str)
        elif isinstance(item, dict):
            kind = str(item.get("kind") or "").strip()
            entry_point = str(item.get("entry_point") or "").strip()
            for bad in FORBIDDEN_SYNTHETIC_SUBSTRINGS:
                if bad == kind or bad in entry_point:
                    raise ValueError(f"Forbidden synthetic injection declared: {item!r}")
            if kind not in ALLOWED_SYNTHETIC_KINDS:
                raise ValueError(
                    f"Synthetic kind '{kind}' is not in ALLOWED_SYNTHETIC_KINDS "
                    f"{sorted(ALLOWED_SYNTHETIC_KINDS)}"
                )
            _validate_entry_point_for_kind(kind, entry_point, item=item, repo_root=repo_root)
            validated.append(item)
        else:
            raise TypeError(f"Unsupported synthetic item type: {type(item).__name__}")
    return validated


def compute_file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_no_credentials_in_obj(obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    m = _SECRET_PATTERN.search(text)
    if m:
        raise ValueError(f"Refusing to write evidence containing secret token pattern: {m.group(0)[:8]}***")
    m_email = _EMAIL_PATTERN.search(text)
    if m_email:
        raise ValueError(f"Refusing to write evidence containing email address pattern: {m_email.group(0)[:3]}***")


def _resolve_script_path_and_sha256(script: str) -> tuple[str, str]:
    token = (script or "").strip().split()[0] if script else ""
    candidate = Path(token)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    if candidate.is_file():
        try:
            rel = candidate.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        except ValueError:
            rel = token
        return rel, compute_file_sha256(candidate)
    return token or script, "0" * 64


def _infer_family_from_script(script_path: str) -> str:
    m = re.search(r"\b(g[1-7])_", script_path.lower())
    if m:
        return m.group(1).upper()
    return "G0"


def _probe_backend_health_sync(base_url: str, user_id: str) -> dict[str, Any]:
    """Query live `/api/v1/dalton/openclaw/health` without caching or fallback (L41, Review Item 4)."""
    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if not svc_token:
        raise RuntimeError(
            "INTERNAL_SERVICE_TOKEN is not set in environment/.env (L40); cannot probe backend health."
        )
    headers: dict[str, str] = {
        "X-User-Id": user_id,
        "X-Service-Token": svc_token,
    }
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=10.0, trust_env=False) as c:
            resp = c.get("/api/v1/dalton/openclaw/health")
    except Exception as exc:
        raise RuntimeError(f"Backend health check failed against {base_url}: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(
            f"Backend health check returned HTTP {resp.status_code} (expected 200): {resp.text[:200]}"
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Backend health check returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Backend health check payload is not a JSON object: {data!r}")

    b_sha = str(data.get("git_sha") or "").strip()
    if not b_sha or b_sha == "unknown":
        raise RuntimeError(
            f"Backend health check returned missing or unknown git_sha ({b_sha!r}); refusing to proceed (L41)."
        )
    return data


def assert_clean_and_synced_environment(
    *,
    base_url: str = "http://127.0.0.1:8000",
    user_id: str = "realrun-pm-a",
    git_sha_fn: Callable[[], str] = get_git_sha,
    git_dirty_fn: Callable[[], bool] = is_git_dirty,
    merged_main_fn: Callable[[], bool] = is_head_merged_into_main,
    main_ancestor_fn: Callable[[], bool] = is_main_ancestor_of_head,
    health_fn: Callable[[str, str], dict[str, Any]] = _probe_backend_health_sync,
) -> dict[str, Any]:
    """Fail fast (exit code != 0) if worktree is dirty, HEAD is not in main, backend is unreachable, or SHA/dirty drifts (L40, L41)."""
    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if not svc_token:
        raise SystemExit(
            "ERROR: INTERNAL_SERVICE_TOKEN is not set in environment/.env (L40). Refusing to run real-run script."
        )
    if git_dirty_fn():
        raise SystemExit(
            "ERROR: Working tree is dirty (git status --porcelain is non-empty, L41). "
            "Refusing to run or write evidence."
        )
    if not merged_main_fn():
        raise SystemExit(
            "ERROR: HEAD is not merged into origin/dalton/main (L41). Refusing to run or write formal evidence."
        )
    if not main_ancestor_fn():
        raise SystemExit(
            "ERROR: HEAD does not share ancestry with origin/dalton/main (L41). Refusing to run on orphaned commit."
        )

    local_sha = git_sha_fn()
    try:
        health = health_fn(base_url, user_id)
    except Exception as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    b_sha = str(health.get("git_sha") or "").strip()
    b_dirty = bool(health.get("git_dirty", True))
    if not b_sha or b_sha == "unknown":
        raise SystemExit(
            f"ERROR: Backend reported missing/unknown git_sha ({b_sha!r}) (L41). Refusing to run."
        )
    if b_sha != local_sha:
        raise SystemExit(
            f"ERROR: Backend git_sha ({b_sha}) != local HEAD ({local_sha}) (L41). Restart backend at HEAD first."
        )
    if b_dirty:
        raise SystemExit(
            "ERROR: Backend reported git_dirty=True at startup (L41). Restart backend from a clean working tree."
        )
    return health


def new_test_user_id(
    family: str,
    *,
    preflight_fn: Callable[..., Any] | None = assert_clean_and_synced_environment,
) -> str:
    """Create a fresh, unique test user ID so runs never hit stale cached journal rows."""
    if preflight_fn is not None:
        preflight_fn()
    slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uid = uuid.uuid4().hex[:8]
    return f"realrun-{slug}-{stamp}-{short_uid}"


def _default_live_env_check(backend_url: str) -> dict[str, Any]:
    from scripts.dalton.realrun.check_env import run_all_checks

    target_url = backend_url if backend_url.startswith("http") else "http://127.0.0.1:8000"
    return run_all_checks(backend_url=target_url)


def build_evidence_envelope(
    *,
    script: str,
    started_at: str | datetime,
    ended_at: str | datetime,
    backend: str,
    test_user_id: str,
    synthetic: list[Any],
    journal: dict[str, Any] | None = None,
    dsn: str | None = None,
    include_null_user_journal: bool = False,
    extra_fields: dict[str, Any] | None = None,
    family: str | None = None,
    sub_item: str | None = None,
    request_trace: list[dict[str, Any]] | None = None,
    assertions: list[dict[str, Any]] | None = None,
    git_sha_fn: Callable[[], str] = get_git_sha,
    git_dirty_fn: Callable[[], bool] = is_git_dirty,
    merged_main_fn: Callable[[], bool] = is_head_merged_into_main,
    main_ancestor_fn: Callable[[], bool] = is_main_ancestor_of_head,
    health_fn: Callable[[str, str], dict[str, Any]] = _probe_backend_health_sync,
    env_check_fn: Callable[[str], dict[str, Any]] = _default_live_env_check,
) -> dict[str, Any]:
    """Construct a P0J §1.3 v2 compliant evidence JSON dictionary with live identity, version & check_env self-checks."""
    started_iso = format_iso_utc(started_at) or now_utc_iso()
    ended_iso = format_iso_utc(ended_at) or now_utc_iso()
    validated_synthetic = validate_synthetic_declarations(synthetic)

    allowed_users = [u.strip() for u in str(test_user_id).split(",") if u.strip()]
    if extra_fields and isinstance(extra_fields.get("test_user_ids"), list):
        for u in extra_fields["test_user_ids"]:
            if str(u).strip() and str(u).strip() not in allowed_users:
                allowed_users.append(str(u).strip())

    probe_url = backend if backend.startswith("http") else "http://127.0.0.1:8000"
    health = assert_clean_and_synced_environment(
        base_url=probe_url,
        user_id=allowed_users[0] if allowed_users else test_user_id,
        git_sha_fn=git_sha_fn,
        git_dirty_fn=git_dirty_fn,
        merged_main_fn=merged_main_fn,
        main_ancestor_fn=main_ancestor_fn,
        health_fn=health_fn,
    )

    if journal is None:
        journal = dump_journal(
            user_id=allowed_users if len(allowed_users) > 1 else test_user_id,
            since=started_iso,
            until=ended_iso,
            dsn=dsn,
            include_null_user=include_null_user_journal,
        )

    git_sha = git_sha_fn()
    git_dirty = git_dirty_fn()
    script_path, script_sha256 = _resolve_script_path_and_sha256(script)
    resolved_family = family or (extra_fields or {}).get("family") or _infer_family_from_script(script_path)
    resolved_sub_item = sub_item or (extra_fields or {}).get("sub_item") or f"{resolved_family}.all"

    backend_git_sha = str(health.get("git_sha") or "").strip()
    if not backend_git_sha or backend_git_sha == "unknown":
        raise RuntimeError(
            f"Backend health check returned missing or unknown git_sha ({backend_git_sha!r}) (L41)."
        )
    backend_git_dirty = bool(health.get("git_dirty", True))
    deployment = str(health.get("deployment") or ("docker" if os.path.exists("/.dockerenv") else "host"))
    pm_source = str(
        health.get("pm_user_ids_source")
        or ("env" if os.getenv("DALTON_PM_USER_IDS", "").strip() else "default")
    )

    env_payload = env_check_fn(probe_url)
    if not isinstance(env_payload, dict) or not env_payload.get("all_passed", False):
        failed_list = env_payload.get("failed_checks") if isinstance(env_payload, dict) else "unknown"
        raise RuntimeError(
            f"Live check_env failed during build_evidence_envelope (failed_checks={failed_list}); "
            "all check_env checks must pass before recording evidence (P0J §1.4)."
        )

    invalid_reasons: list[str] = []
    if git_dirty:
        invalid_reasons.append("worktree_git_dirty")
    if backend_git_dirty:
        invalid_reasons.append("backend_git_dirty")
    if backend_git_sha != git_sha:
        invalid_reasons.append(f"backend_git_sha_mismatch:{backend_git_sha}!={git_sha}")
    if "local-dev-user" in allowed_users:
        invalid_reasons.append("test_user_id_is_local_dev_user")

    totals = (journal or {}).get("totals") or {}
    if totals.get("conversation_usages_unavailable") is True:
        invalid_reasons.append("conversation_usages_unavailable")

    unexpected_uids = list(totals.get("unexpected_user_ids") or [])
    if unexpected_uids:
        invalid_reasons.append(f"unexpected_window_user_ids:{','.join(str(x) for x in unexpected_uids)}")

    allowed_set = {u for u in allowed_users if u and u != "<NULL>"}
    for r in (journal or {}).get("rows") or []:
        r_uid = r.get("user_id")
        if r_uid is None or not str(r_uid).strip() or str(r_uid).strip() == "<NULL>":
            invalid_reasons.append("journal_row_null_user")
            break
        if r_uid == "local-dev-user":
            invalid_reasons.append("journal_row_owned_by_local_dev_user")
            break
        if str(r_uid) not in allowed_set:
            invalid_reasons.append(f"journal_row_unexpected_user:{r_uid}")
            break
    for cu in (journal or {}).get("conversation_usages") or []:
        c_uid = cu.get("user_id")
        if c_uid is None or not str(c_uid).strip() or str(c_uid).strip() == "<NULL>":
            invalid_reasons.append("conversation_usage_null_user")
            break
        if c_uid == "local-dev-user":
            invalid_reasons.append("conversation_usage_owned_by_local_dev_user")
            break
        if str(c_uid) not in allowed_set:
            invalid_reasons.append(f"conversation_usage_unexpected_user:{c_uid}")
            break

    trace_items = (
        request_trace
        if request_trace is not None
        else (
            (extra_fields or {}).get("request_trace")
            if isinstance((extra_fields or {}).get("request_trace"), list)
            else list(_GLOBAL_REQUEST_TRACE)
        )
    )
    assertion_items = (
        assertions
        if assertions is not None
        else (
            (extra_fields or {}).get("assertions")
            if isinstance((extra_fields or {}).get("assertions"), list)
            else []
        )
    )

    envelope: dict[str, Any] = {
        "evidence_version": 2,
        "family": resolved_family,
        "sub_item": resolved_sub_item,
        "run_id": str((extra_fields or {}).get("run_id") or uuid.uuid4()),
        "started_at": started_iso,
        "ended_at": ended_iso,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "backend_git_sha": backend_git_sha,
        "backend_git_dirty": backend_git_dirty,
        "script": script,
        "script_path": script_path,
        "script_sha256": script_sha256,
        "backend": backend,
        "test_user_id": test_user_id,
        "identity_mode": "service_token",
        "pm_user_ids_source": pm_source,
        "deployment": deployment,
        "environment": env_payload,
        "synthetic": validated_synthetic,
        "synthetic_seed": validated_synthetic,
        "request_trace": trace_items,
        "journal": journal,
        "llm_journal": list((journal or {}).get("rows") or []),
        "conversation_usages": list((journal or {}).get("conversation_usages") or []),
        "assertions": assertion_items,
        "valid": len(invalid_reasons) == 0,
        "invalid_reasons": invalid_reasons,
    }
    if extra_fields:
        for k, v in extra_fields.items():
            if k not in envelope:
                envelope[k] = v
    assert_no_credentials_in_obj(envelope)
    return envelope


def render_summary_markdown(
    *,
    family_code: str | None = None,
    title: str | None = None,
    raw_json_rel_path: str,
    raw_json_sha256: str,
    envelope: dict[str, Any],
    key_findings: list[tuple[str, Any]] | list[list[Any]] | None = None,
    notes: list[str] | None = None,
) -> str:
    """Render the trial-run Markdown summary required by P0I §2.1 #6 / P0J §1.6 directly from the evidence JSON."""
    resolved_family = family_code or str(envelope.get("family") or "G0")
    resolved_title = title or str(envelope.get("summary_title") or resolved_family)
    resolved_findings = key_findings if key_findings is not None else (envelope.get("key_findings") or [])
    resolved_notes = notes if notes is not None else (envelope.get("notes") or [])

    journal = envelope.get("journal") or {}
    totals = journal.get("totals") or {}
    rows = journal.get("rows") or []
    synthetic = envelope.get("synthetic") or []

    lines: list[str] = [
        f"# {resolved_family} 真实运行验证摘要 — {resolved_title}",
        "",
        "## 1. 运行元数据与原始证据校验",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| `evidence_version` | `{envelope.get('evidence_version', 2)}` |",
        f"| `git_sha` | `{envelope.get('git_sha')}` |",
        f"| `git_dirty` | `{str(bool(envelope.get('git_dirty', False))).lower()}` |",
        f"| `backend_git_sha` | `{envelope.get('backend_git_sha', envelope.get('git_sha'))}` |",
        f"| `backend_git_dirty` | `{str(bool(envelope.get('backend_git_dirty', False))).lower()}` |",
        f"| `script` | `{envelope.get('script')}` |",
        f"| `script_sha256` | `{envelope.get('script_sha256', '')}` |",
        f"| `started_at` | `{envelope.get('started_at')}` |",
        f"| `ended_at` | `{envelope.get('ended_at')}` |",
        f"| `backend` | `{envelope.get('backend')}` |",
        f"| `deployment` | `{envelope.get('deployment', 'host')}` |",
        f"| `test_user_id` | `{envelope.get('test_user_id')}` |",
        f"| `identity_mode` | `{envelope.get('identity_mode', 'service_token')}` |",
        f"| `pm_user_ids_source` | `{envelope.get('pm_user_ids_source', 'env')}` |",
        f"| `valid` | `{str(bool(envelope.get('valid', True))).lower()}` |",
        f"| 原始证据路径 | `{raw_json_rel_path}` |",
        f"| 原始 JSON `sha256` | `{raw_json_sha256}` |",
        "",
        "## 2. 非真实成分声明 (`synthetic`)",
        "",
    ]
    if not synthetic:
        lines.append("- `[]`（全链路无任何注入、模拟或替身）")
    else:
        for item in synthetic:
            if isinstance(item, dict):
                desc = item.get("description") or item.get("reason") or ""
                lines.append(
                    f"- **`{item.get('kind')}`**（入口：`{item.get('entry_point')}`）：{desc}"
                )
            else:
                lines.append(f"- `{item}`")

    lines.extend(
        [
            "",
            "## 3. 关键验证字段汇总",
            "",
            "| 检查项 | 实测结果 |",
            "|---|---|",
        ]
    )
    for pair in resolved_findings:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            k, v = pair[0], pair[1]
            val_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
            lines.append(f"| {k} | `{val_str}` |")

    lines.extend(
        [
            "",
            "## 4. 调用日志对账 (`dalton_llm_journal`)",
            "",
            f"- **总调用数 (`total_calls`)**：`{totals.get('total_calls', 0)}`",
            f"- **真实新调用 (`fresh_calls`, `replayed=false`)**：`{totals.get('fresh_calls', 0)}`",
            f"- **缓存回放 (`replayed_calls`, `replayed=true`)**：`{totals.get('replayed_calls', 0)}`",
            f"- **状态分布**：`done={totals.get('done_calls', 0)}`, `failed={totals.get('failed_calls', 0)}`, `indeterminate={totals.get('indeterminate_calls', 0)}`",
            f"- **日志现金花费 (`cash_usd`)**：`${totals.get('cash_usd', 0.0):.6f}`",
            f"- **日志名义花费 (`notional_usd`)**：`${totals.get('notional_usd', 0.0):.6f}`",
            f"- **对话轮次 (`conversation_turns`)**：`{totals.get('conversation_turns', 0)}`（对话现金 `${totals.get('conversation_cash_usd', 0.0):.6f}`）",
            f"- **合计现金花费 (`combined_cash_usd`)**：`${totals.get('combined_cash_usd', 0.0):.6f}`",
            "",
        ]
    )

    if rows:
        lines.extend(
            [
                "| `invocation_id` | `purpose` | `model_key` | `state` | `replayed` | `total_tokens` | `cash_usd` | `notional_usd` | `created_at` |",
                "|---|---|---|---|---|---:|---:|---:|---|",
            ]
        )
        for r in rows[:50]:
            inv = str(r.get("invocation_id") or "")
            short_inv = inv[:18] + "…" if len(inv) > 19 else inv
            lines.append(
                f"| `{short_inv}` | `{r.get('purpose')}` | `{r.get('model_key')}` | "
                f"`{r.get('state')}` | `{str(bool(r.get('replayed'))).lower()}` | "
                f"`{r.get('total_tokens', 0)}` | `${float(r.get('cash_usd', 0.0)):.6f}` | "
                f"`${float(r.get('notional_usd', 0.0)):.6f}` | `{r.get('created_at')}` |"
            )
        if len(rows) > 50:
            lines.append(f"| ... | 其余 {len(rows) - 50} 条记录见原始 JSON | | | | | | | |")
    else:
        lines.append("_本窗口内无 `dalton_llm_journal` 记录。_")

    conv_usages = journal.get("conversation_usages") or []
    if conv_usages:
        lines.extend(
            [
                "",
                "### 对话与子智能体调用明细 (`conversation_usages`)",
                "",
                "| `conversation_response_id` | `#` | `model` | `billing_type` | `input_tokens` | `output_tokens` | `cost_usd` | `created_at` |",
                "|---|---:|---|---|---:|---:|---:|---|",
            ]
        )
        for cu in conv_usages:
            resp_id = str(cu.get("conversation_response_id") or "")
            short_resp = resp_id[:18] + "…" if len(resp_id) > 19 else resp_id
            tu_sum = cu.get("token_usage_summary") or {}
            per_calls = tu_sum.get("per_call_costs") or []
            by_model = tu_sum.get("by_model") or {}
            if per_calls:
                for idx_c, pc in enumerate(per_calls, 1):
                    lines.append(
                        f"| `{short_resp}` | `{idx_c}` | `{pc.get('model')}` | `{pc.get('billing_type')}` | "
                        f"`{pc.get('input_tokens', 0)}` | `{pc.get('output_tokens', 0)}` | "
                        f"`${float(pc.get('cost', 0.0)):.6f}` | `{cu.get('created_at')}` |"
                    )
            elif by_model:
                for idx_c, (m_name, m_stats) in enumerate(by_model.items(), 1):
                    b_type = "byok" if cu.get("is_byok") else "oauth"
                    lines.append(
                        f"| `{short_resp}` | `{idx_c}` | `{m_name}` | `{b_type}` | "
                        f"`{m_stats.get('input_tokens', 0)}` | `{m_stats.get('output_tokens', 0)}` | "
                        f"`${float(m_stats.get('total_cost', cu.get('cash_usd', 0.0))):.6f}` | `{cu.get('created_at')}` |"
                    )
            else:
                lines.append(
                    f"| `{short_resp}` | `1` | `-` | `-` | `0` | `0` | "
                    f"`${float(cu.get('cash_usd', 0.0)):.6f}` | `{cu.get('created_at')}` |"
                )

    if resolved_notes:
        lines.extend(["", "## 5. 补充说明", ""])
        for n in resolved_notes:
            lines.append(f"- {n}")

    lines.append("")
    return "\n".join(lines)


def _redirect_docs_summary_to_var(md_path: Path, repo_root: Path = REPO_ROOT) -> Path:
    """Redirect any `docs/dalton/...` runtime summary path to `var/dalton/...` so runtime never dirties `docs/` (Review Item 15)."""
    docs_root = (repo_root / "docs" / "dalton").resolve()
    try:
        rel_under_dalton = md_path.resolve().relative_to(docs_root)
        return repo_root / "var" / "dalton" / rel_under_dalton
    except ValueError:
        return md_path


def write_evidence_and_summary(
    *,
    family_code: str,
    title: str,
    raw_json_path: str | Path,
    summary_md_path: str | Path,
    envelope: dict[str, Any],
    key_findings: list[tuple[str, Any]],
    notes: list[str] | None = None,
    record: bool | None = None,
    git_dirty_fn: Callable[[], bool] = is_git_dirty,
    merged_main_fn: Callable[[], bool] = is_head_merged_into_main,
) -> tuple[Path, Path, str]:
    """Validate and optionally write raw JSON + runtime Markdown summary under `var/dalton/` when `--record` is enabled."""
    if record is None:
        record = "--record" in sys.argv

    envelope["family"] = family_code
    envelope["summary_title"] = title
    envelope["key_findings"] = [[k, v] for k, v in key_findings]
    envelope["notes"] = list(notes or [])
    assert_no_credentials_in_obj(envelope)

    if envelope.get("git_dirty") or git_dirty_fn():
        raise SystemExit(
            "ERROR: Refusing to write evidence on dirty working tree (L41)."
        )
    if not merged_main_fn():
        raise SystemExit(
            "ERROR: Refusing to write evidence when HEAD is not merged into origin/dalton/main (L41)."
        )
    if envelope.get("backend_git_dirty"):
        raise SystemExit(
            "ERROR: Refusing to write evidence because backend reported git_dirty=True (L41)."
        )
    if not envelope.get("backend_git_sha") or envelope.get("backend_git_sha") == "unknown":
        raise SystemExit(
            "ERROR: Refusing to write evidence because backend_git_sha is missing or unknown (L41)."
        )
    if envelope.get("backend_git_sha") != envelope.get("git_sha"):
        raise SystemExit(
            f"ERROR: Refusing to write evidence due to backend SHA mismatch "
            f"({envelope.get('backend_git_sha')} != {envelope.get('git_sha')}) (L41)."
        )
    if not envelope.get("valid", True):
        raise SystemExit(
            f"ERROR: Evidence marked valid=False (reasons: {envelope.get('invalid_reasons')})"
        )

    raw_path = Path(raw_json_path)
    if not raw_path.is_absolute():
        raw_path = REPO_ROOT / raw_path

    try:
        rel_raw = raw_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel_raw = raw_path.as_posix()

    raw_md_path = Path(summary_md_path)
    if not raw_md_path.is_absolute():
        raw_md_path = REPO_ROOT / raw_md_path
    md_path = _redirect_docs_summary_to_var(raw_md_path, repo_root=REPO_ROOT)

    serialized_json = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
    sha256_hex = hashlib.sha256(serialized_json.encode("utf-8")).hexdigest()

    md_text = render_summary_markdown(
        family_code=family_code,
        title=title,
        raw_json_rel_path=rel_raw,
        raw_json_sha256=sha256_hex,
        envelope=envelope,
        key_findings=key_findings,
        notes=notes,
    )
    assert_no_credentials_in_obj({"markdown": md_text})

    if not record:
        return raw_path, md_path, sha256_hex

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(serialized_json, encoding="utf-8")

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_text, encoding="utf-8")
    return raw_path, md_path, sha256_hex


def render_summaries_from_var_to_docs(
    *,
    date_str: str,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Render `G<N>.md` into `docs/dalton/dev-guide/trial-runs/realrun-<date>/` strictly from `var/dalton/trial-runs/realrun-<date>/*.json` (L42, Item 15)."""
    var_dir = repo_root / "var" / "dalton" / "trial-runs" / f"realrun-{date_str}"
    if not var_dir.is_dir():
        raise FileNotFoundError(f"Evidence directory not found: {var_dir}")
    docs_dir = repo_root / "docs" / "dalton" / "dev-guide" / "trial-runs" / f"realrun-{date_str}"
    docs_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for json_file in sorted(var_dir.glob("g*.json")):
        payload = json.loads(json_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        sub_item = str(payload.get("sub_item") or "")
        if sub_item and not sub_item.endswith(".all") and "_" in json_file.stem:
            # Skip sub-item intermediate JSONs like g5_1_broker.json if g5_broker_and_antigravity.json exists
            m_sub = re.match(r"^g[1-7]_\d+_", json_file.name)
            if m_sub:
                continue
        family_code = str(payload.get("family") or _infer_family_from_script(json_file.name))
        sha256_hex = compute_file_sha256(json_file)
        try:
            rel_raw = json_file.relative_to(repo_root).as_posix()
        except ValueError:
            rel_raw = json_file.as_posix()
        md_text = render_summary_markdown(
            family_code=family_code,
            title=payload.get("summary_title"),
            raw_json_rel_path=rel_raw,
            raw_json_sha256=sha256_hex,
            envelope=payload,
        )
        assert_no_credentials_in_obj({"markdown": md_text})
        out_md = docs_dir / f"{family_code}.md"
        out_md.write_text(md_text, encoding="utf-8")
        written.append(out_md.relative_to(repo_root).as_posix())
    return written


class RealRunHttpClient:
    """Thin HTTP client for operating against the live backend (`http://127.0.0.1:8000`) with service-token identity (L40, L41)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str,
        timeout_s: float = 120.0,
        extra_headers: dict[str, str] | None = None,
        skip_env_preflight: bool = False,
        preflight_fn: Callable[..., Any] | None = assert_clean_and_synced_environment,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = str(user_id).strip()
        if not self.user_id:
            raise ValueError("user_id must be non-empty for RealRunHttpClient")

        svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
        if not svc_token:
            raise RuntimeError(
                "INTERNAL_SERVICE_TOKEN is required in environment/.env for RealRunHttpClient (L40); "
                "refusing to fall back to unauthenticated requests."
            )

        if not skip_env_preflight and preflight_fn is not None:
            preflight_fn(base_url=self.base_url, user_id=self.user_id)

        headers = {
            "X-User-Id": self.user_id,
            "X-Service-Token": svc_token,
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout_s,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RealRunHttpClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def ensure_user(self) -> tuple[int, Any]:
        """Provision `self.user_id` via `POST /api/v1/users` (idempotent on 409) without storing email addresses."""
        return self.request(
            "POST",
            "/api/v1/users",
            json_body={
                "email": None,
                "name": f"RealRun {self.user_id}",
                "timezone": "America/New_York",
                "locale": "zh-CN",
            },
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        resp = self._client.request(
            method.upper(),
            path,
            json=json_body,
            params=params,
            headers=headers,
        )
        _GLOBAL_REQUEST_TRACE.append(
            {
                "method": method.upper(),
                "path": path,
                "user_id": self.user_id,
                "status_code": resp.status_code,
            }
        )
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text
        return resp.status_code, resp.text

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        return self.request("GET", path, params=params, headers=headers)

    def post(
        self,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        return self.request("POST", path, json_body=json_body, params=params, headers=headers)

    def put(
        self,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        return self.request("PUT", path, json_body=json_body, params=params, headers=headers)

    def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        return self.request("DELETE", path, params=params, headers=headers)

    def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        max_events: int = 500,
    ) -> tuple[int, list[dict[str, Any]], Any]:
        """Send an HTTP request expecting SSE (`text/event-stream`) or JSON error."""
        events: list[dict[str, Any]] = []
        with self._client.stream(
            method.upper(),
            path,
            json=json_body,
            headers=headers,
        ) as resp:
            status = resp.status_code
            _GLOBAL_REQUEST_TRACE.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "user_id": self.user_id,
                    "status_code": status,
                }
            )
            ctype = resp.headers.get("content-type", "")
            if status >= 400 or "text/event-stream" not in ctype:
                body_bytes = resp.read()
                text = body_bytes.decode("utf-8", errors="replace")
                try:
                    return status, events, json.loads(text)
                except Exception:
                    return status, events, text

            curr_event = "message"
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    curr_event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    raw_data = line.split(":", 1)[1].strip()
                    try:
                        parsed_data = json.loads(raw_data)
                    except Exception:
                        parsed_data = raw_data
                    events.append({"event": curr_event, "data": parsed_data})
                    if len(events) >= max_events:
                        break
            return status, events, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render trial-run Markdown summaries from var/dalton/trial-runs/realrun-<date>/ into docs/dalton/dev-guide/trial-runs/realrun-<date>/ (P0J §1.6, L42)."
    )
    parser.add_argument("--render-docs", action="store_true", required=True, help="Render G<N>.md summaries into docs/")
    parser.add_argument("--date", required=True, help="Trial-run date string (YYYYMMDD)")
    args = parser.parse_args(argv)
    written = render_summaries_from_var_to_docs(date_str=args.date)
    print(json.dumps({"rendered_summaries": written}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
