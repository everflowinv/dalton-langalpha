#!/usr/bin/env python3
"""Shared utilities for Dalton real-run evidence scripts (P0I §2.1–§2.2, P0J §1.2–§1.6, L37, L40–L42).

Responsibilities:
1. Build the mandatory v2 evidence envelope (`evidence_version: 2`, `git_sha`, `git_dirty`,
   `backend_git_sha`, `backend_git_dirty`, `script_path`, `script_sha256`, `started_at`,
   `ended_at`, `backend`, `test_user_id`, `identity_mode`, `pm_user_ids_source`, `deployment`,
   `synthetic` / `synthetic_seed`, `request_trace`, `journal` / `llm_journal`,
   `conversation_usages`, `assertions`, `valid`, `invalid_reasons`).
2. Validate `synthetic` declarations against the 6 allowed injection categories and the
   explicit `COMMITTED_TEST_HOOKS` registry (P0G §0.2, P0J §1.5, L37).
3. Enforce clean working tree, backend version parity, and `INTERNAL_SERVICE_TOKEN`
   per-user identity self-check (L40, L41).
4. Compute SHA-256 of raw evidence JSON under `var/dalton/` and render a credential-free,
   email-free Markdown summary under `docs/dalton/dev-guide/trial-runs/realrun-<YYYYMMDD>/G<N>.md`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

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
        "flags": "--simulate-upstream / DALTON_BRIDGE_TEST_MODE / DALTON_BRIDGE_TEST_DELAY_S",
        "description": "Simulate >240s upstream delay or synthetic upstream errors on researchdata_bridge.py (G7.A / G7.C).",
    },
    "integrations/openclaw/dalton-langalpha-broker": {
        "module": "integrations/openclaw/dalton-langalpha-broker/src/broker.mjs",
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

_LAST_BACKEND_HEALTH: dict[str, Any] = {}
_GLOBAL_REQUEST_TRACE: list[dict[str, Any]] = []


def _in_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_test_user_id(family: str) -> str:
    """Create a fresh, unique test user ID so runs never hit stale cached journal rows."""
    if not _in_pytest():
        assert_clean_and_synced_environment()
    slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uid = uuid.uuid4().hex[:8]
    return f"realrun-{slug}-{stamp}-{short_uid}"


def get_git_sha(short: bool = True) -> str:
    cmd = ["git", "--no-optional-locks", "-C", str(REPO_ROOT), "rev-parse"]
    if short:
        cmd.append("--short=8")
    cmd.append("HEAD")
    return subprocess.check_output(cmd, text=True).strip()


def is_git_dirty() -> bool:
    """Return True if working tree has uncommitted changes outside `var/` and `docs/dalton/dev-guide/trial-runs/` (P0J §1.3, L41)."""
    raw = subprocess.check_output(
        ["git", "--no-optional-locks", "-C", str(REPO_ROOT), "status", "--porcelain"],
        text=True,
    )
    for line in raw.splitlines():
        if not line.strip():
            continue
        path_part = line[3:].strip().strip('"')
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[-1].strip().strip('"')
        if path_part == "var" or path_part.startswith("var/"):
            continue
        if path_part.startswith("docs/dalton/dev-guide/trial-runs/"):
            continue
        return True
    return False


def is_head_merged_into_main(ref: str = "origin/dalton/main") -> bool:
    """Return True if HEAD is an ancestor of (or equal to) `origin/dalton/main` (or `dalton/main`)."""
    for candidate in (ref, "dalton/main"):
        res = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", "HEAD", candidate],
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
            ["git", "--no-optional-locks", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", candidate, "HEAD"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            return True
    return False


def _validate_entry_point_for_kind(kind: str, entry_point: str) -> None:
    if not entry_point:
        raise ValueError(f"Synthetic declaration for kind '{kind}' must specify a non-empty 'entry_point'")
    if kind == "settings_api_budget":
        if "/api/v1/dalton/budget" not in entry_point:
            raise ValueError(
                f"Synthetic kind 'settings_api_budget' requires entry_point containing '/api/v1/dalton/budget', got: {entry_point!r}"
            )
    elif kind == "knowledge_api_agenda":
        if "/api/v1/dalton/knowledge/" not in entry_point and "agenda" not in entry_point:
            raise ValueError(
                f"Synthetic kind 'knowledge_api_agenda' requires knowledge agenda entry_point, got: {entry_point!r}"
            )
    elif kind == "knowledge_api_import_fixture":
        if "/api/v1/dalton/knowledge/" not in entry_point and "tests/fixtures/" not in entry_point:
            raise ValueError(
                f"Synthetic kind 'knowledge_api_import_fixture' requires knowledge import or tests/fixtures/ entry_point, got: {entry_point!r}"
            )
    elif kind == "watch_state_unsee_filing":
        if "dalton_watch_state" not in entry_point and "seen_accessions" not in entry_point:
            raise ValueError(
                f"Synthetic kind 'watch_state_unsee_filing' requires dalton_watch_state.seen_accessions entry_point, got: {entry_point!r}"
            )
    elif kind == "committed_test_hook":
        if not any(hook_key in entry_point for hook_key in COMMITTED_TEST_HOOKS):
            raise ValueError(
                f"Synthetic kind 'committed_test_hook' entry_point {entry_point!r} is not registered in "
                f"COMMITTED_TEST_HOOKS {sorted(COMMITTED_TEST_HOOKS.keys())}"
            )
    elif kind == "committed_fixture_input":
        if "tests/fixtures/" not in entry_point and "docs/dalton/" not in entry_point:
            raise ValueError(
                f"Synthetic kind 'committed_fixture_input' requires committed fixture path under tests/fixtures/ or docs/dalton/, got: {entry_point!r}"
            )


def validate_synthetic_declarations(synthetic: list[Any]) -> list[dict[str, Any] | str]:
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
            if entry_point:
                _validate_entry_point_for_kind(kind_prefix, entry_point)
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
            _validate_entry_point_for_kind(kind, entry_point)
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
    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    headers: dict[str, str] = {"X-User-Id": user_id}
    if svc_token:
        headers["X-Service-Token"] = svc_token
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=10.0, trust_env=False) as c:
            resp = c.get("/api/v1/dalton/openclaw/health")
            if resp.status_code == 200 and "application/json" in resp.headers.get("content-type", ""):
                data = resp.json()
                if isinstance(data, dict):
                    _LAST_BACKEND_HEALTH.update(data)
                    return data
    except Exception:
        pass
    return dict(_LAST_BACKEND_HEALTH)


def assert_clean_and_synced_environment(
    *,
    base_url: str = "http://127.0.0.1:8000",
    user_id: str = "realrun-pm-a",
    require_merged_main: bool = True,
) -> dict[str, Any]:
    """Fail fast (exit code != 0) if worktree is dirty, HEAD is not in main, backend SHA/dirty drifts, or service token is missing (L40, L41)."""
    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if not svc_token:
        raise SystemExit(
            "ERROR: INTERNAL_SERVICE_TOKEN is not set in environment/.env (L40). Refusing to run real-run script."
        )
    if is_git_dirty():
        raise SystemExit(
            "ERROR: Working tree is dirty (git status --porcelain excluding var/ is non-empty, L41). "
            "Refusing to run or write evidence."
        )
    allow_unmerged = os.getenv("DALTON_ALLOW_UNMERGED_BRANCH") == "1"
    if require_merged_main and not allow_unmerged and not is_head_merged_into_main():
        raise SystemExit(
            "ERROR: HEAD is not merged into origin/dalton/main (L41). Refusing to write formal evidence."
        )
    if not is_main_ancestor_of_head():
        raise SystemExit(
            "ERROR: HEAD does not share ancestry with origin/dalton/main (L41). Refusing to run on orphaned commit."
        )

    local_sha = get_git_sha(short=True)
    health = _probe_backend_health_sync(base_url, user_id)
    if health:
        b_sha = str(health.get("git_sha") or "")
        b_dirty = bool(health.get("git_dirty", False))
        if b_sha and b_sha != "unknown" and b_sha != local_sha:
            raise SystemExit(
                f"ERROR: Backend git_sha ({b_sha}) != local HEAD ({local_sha}) (L41). Restart backend at HEAD first."
            )
        if b_dirty:
            raise SystemExit(
                "ERROR: Backend reported git_dirty=True at startup (L41). Restart backend from a clean working tree."
            )
    return health


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
) -> dict[str, Any]:
    """Construct a P0J §1.3 v2 compliant evidence JSON dictionary with identity & version self-checks."""
    started_iso = format_iso_utc(started_at) or now_utc_iso()
    ended_iso = format_iso_utc(ended_at) or now_utc_iso()
    validated_synthetic = validate_synthetic_declarations(synthetic)

    allowed_users = [u.strip() for u in str(test_user_id).split(",") if u.strip()]
    if extra_fields and isinstance(extra_fields.get("test_user_ids"), list):
        for u in extra_fields["test_user_ids"]:
            if str(u).strip() and str(u).strip() not in allowed_users:
                allowed_users.append(str(u).strip())

    if journal is None:
        journal = dump_journal(
            user_id=allowed_users if len(allowed_users) > 1 else test_user_id,
            since=started_iso,
            until=ended_iso,
            dsn=dsn,
            include_null_user=include_null_user_journal,
        )

    git_sha = get_git_sha(short=True)
    git_dirty = False if _in_pytest() else is_git_dirty()
    script_path, script_sha256 = _resolve_script_path_and_sha256(script)
    resolved_family = family or (extra_fields or {}).get("family") or _infer_family_from_script(script_path)
    resolved_sub_item = sub_item or (extra_fields or {}).get("sub_item") or f"{resolved_family}.all"

    health = dict(_LAST_BACKEND_HEALTH)
    if not health and not _in_pytest() and backend.startswith("http"):
        health = _probe_backend_health_sync(backend, allowed_users[0] if allowed_users else test_user_id)

    backend_git_sha = str(health.get("git_sha") or git_sha)
    backend_git_dirty = bool(health.get("git_dirty", False))
    deployment = str(health.get("deployment") or ("docker" if os.path.exists("/.dockerenv") else "host"))
    pm_source = str(
        health.get("pm_user_ids_source")
        or ("env" if os.getenv("DALTON_PM_USER_IDS", "").strip() else "dalton.yaml")
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

    allowed_set = set(allowed_users)
    if include_null_user_journal:
        allowed_set.add("<NULL>")
    for r in (journal or {}).get("rows") or []:
        r_uid = r.get("user_id")
        if r_uid == "local-dev-user":
            invalid_reasons.append("journal_row_owned_by_local_dev_user")
            break
        if r_uid is not None and str(r_uid) not in allowed_set:
            invalid_reasons.append(f"journal_row_unexpected_user:{r_uid}")
            break
    for cu in (journal or {}).get("conversation_usages") or []:
        c_uid = cu.get("user_id")
        if c_uid == "local-dev-user":
            invalid_reasons.append("conversation_usage_owned_by_local_dev_user")
            break
        if c_uid is not None and str(c_uid) not in allowed_set:
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

    env_cache_path = REPO_ROOT / "var" / "dalton-realrun" / "check_env.latest.json"
    env_payload: dict[str, Any] = {
        "deployment": deployment,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "backend_git_sha": backend_git_sha,
        "backend_git_dirty": backend_git_dirty,
        "identity_mode": "service_token",
        "pm_user_ids_source": pm_source,
    }
    if env_cache_path.is_file():
        try:
            cached_env = json.loads(env_cache_path.read_text(encoding="utf-8"))
            if isinstance(cached_env, dict):
                env_payload = cached_env
        except Exception:
            pass

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
    family_code: str,
    title: str,
    raw_json_rel_path: str,
    raw_json_sha256: str,
    envelope: dict[str, Any],
    key_findings: list[tuple[str, Any]],
    notes: list[str] | None = None,
) -> str:
    """Render the trial-run Markdown summary required by P0I §2.1 #6 / P0J §1.6."""
    journal = envelope.get("journal") or {}
    totals = journal.get("totals") or {}
    rows = journal.get("rows") or []
    synthetic = envelope.get("synthetic") or []

    lines: list[str] = [
        f"# {family_code} 真实运行验证摘要 — {title}",
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
    for k, v in key_findings:
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

    if notes:
        lines.extend(["", "## 5. 补充说明", ""])
        for n in notes:
            lines.append(f"- {n}")

    lines.append("")
    return "\n".join(lines)


def write_evidence_and_summary(
    *,
    family_code: str,
    title: str,
    raw_json_path: str | Path,
    summary_md_path: str | Path,
    envelope: dict[str, Any],
    key_findings: list[tuple[str, Any]],
    notes: list[str] | None = None,
) -> tuple[Path, Path, str]:
    """Write raw JSON evidence to `var/dalton/...` and Markdown summary to `trial-runs/realrun-<date>/G<N>.md`."""
    assert_no_credentials_in_obj(envelope)
    raw_path = Path(raw_json_path)
    if not raw_path.is_absolute():
        raw_path = REPO_ROOT / raw_path

    if not _in_pytest():
        if envelope.get("git_dirty") or is_git_dirty():
            raise SystemExit(
                "ERROR: Refusing to write evidence on dirty working tree (L41)."
            )
        if envelope.get("backend_git_dirty"):
            raise SystemExit(
                "ERROR: Refusing to write evidence because backend reported git_dirty=True (L41)."
            )
        if envelope.get("backend_git_sha") and envelope.get("backend_git_sha") != envelope.get("git_sha"):
            raise SystemExit(
                f"ERROR: Refusing to write evidence due to backend SHA mismatch "
                f"({envelope.get('backend_git_sha')} != {envelope.get('git_sha')}) (L41)."
            )

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    sha256_hex = compute_file_sha256(raw_path)
    try:
        rel_raw = raw_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel_raw = raw_path.as_posix()

    md_path = Path(summary_md_path)
    if not md_path.is_absolute():
        md_path = REPO_ROOT / md_path
    md_path.parent.mkdir(parents=True, exist_ok=True)

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
    md_path.write_text(md_text, encoding="utf-8")

    if not _in_pytest() and not envelope.get("valid", True):
        raise SystemExit(
            f"ERROR: Evidence marked valid=False (reasons: {envelope.get('invalid_reasons')})"
        )
    return raw_path, md_path, sha256_hex


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

        if not _in_pytest() and not skip_env_preflight:
            assert_clean_and_synced_environment(base_url=self.base_url, user_id=self.user_id)

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
