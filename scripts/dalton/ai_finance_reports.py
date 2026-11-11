#!/usr/bin/env python3
"""Save one AI-finance daily, its original card, and the post-send seen state.

The model supplies the daily and the broadcast news keys. Paths, the owner,
and the batch clock come from the operator config and the ingested signal
bundle. This module does not send a card, call an embedding API, or write the
GitHub observation pool.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROFILE = "ai-finance-daily"
HKT = ZoneInfo("Asia/Hong_Kong")
CARD_TITLE = "\U0001f4ca AI + \u91d1\u878d\u6295\u8d44\u65e5\u62a5 \u2014 {date}"
MAIN_TITLE = "## \U0001f4ca AI + \u91d1\u878d\u6295\u8d44\u65e5\u62a5"
CROSS_TITLE = "#### \u8de8\u65e5\u5171\u6027\u4e0e\u65b0\u8d8b\u52bf"
HEADINGS = (
    "### \u4eca\u65e5\u8981\u95fb",
    "### GitHub AI \u9879\u76ee\u52a8\u91cf",
    "### Earnings Calls\uff1aAI \u7684\u76f4\u63a5\u4e1a\u52a1\u5f71\u54cd",
    "### \u6765\u6e90\u8bf4\u660e",
)
MAX_MARKDOWN_BYTES = 128 * 1024
MAX_CARD_JSON_BYTES = 262144
MAX_RESPONSE_BYTES = 384 * 1024
MAX_SEEN_BYTES = 2 * 1024 * 1024
MAX_CHARS = 32000
NEWS_KEEP_DAYS = 7
GITHUB_KEEP_DAYS = 30
_FIRST_DATE_ENV = "DALTON_AI_FINANCE_FIRST_REPORT_DATE"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_PHASE = re.compile(r"^[a-z_]{1,40}$")
_GITHUB_ITEM = re.compile(r"(?m)^\d+\. \*\*\[([\w.-]+/[\w.-]+)\]")
_NUMBERED = re.compile(r"(?m)^(\d+)\. ")
_ACTIONS = frozenset({"save", "get", "commit_seen"})
_COMMON_KEYS = frozenset({
    "action",
    "batch_id",
    "automation_execution_id",
    "owner_user_id",
    "user_id",
    "workspace_id",
    "thread_id",
    "profile",
})
_THREAD_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_OVERLAY_CARD = Path(__file__).resolve().parent / "openclaw_overlays" / "scripts" / "feishu_send_card.py"
_CARD_RENDERER: Any = None
_MESSAGES = {
    "not_configured": "AI-finance reports are not configured.",
    "config_invalid": "AI-finance config is invalid.",
    "invalid_request": "The request is not valid.",
    "unknown_field": "Request keys are not allowed.",
    "invalid_action": "action must be save, get, or commit_seen.",
    "invalid_batch": "batch_id must be a real YYYY-MM-DD business date.",
    "invalid_owner": "owner_user_id must be a canonical UUID.",
    "invalid_identity": "Server run identity is not a canonical UUID.",
    "owner_mismatch": "This batch belongs to another owner.",
    "profile_mismatch": "This batch profile is not ai-finance-daily.",
    "not_ready": "The ingested signal bundle is not ready for a report.",
    "bundle_missing": "Save a signal bundle before this report.",
    "compare_unavailable": "The saved comparison cannot be read.",
    "clock_inconsistent": "The saved batch clock could not be read.",
    "not_found": "The prepared batch is not available.",
    "batch_not_ready": "The prepared batch is not complete.",
    "report_missing": "Save the daily before this action.",
    "report_invalid": "The daily did not pass the report check.",
    "report_too_large": "The card does not fit in the report limit.",
    "content_conflict": "This batch already has a different daily.",
    "hash_mismatch": "The saved report failed the hash check.",
    "commit_inconsistent": "The saved report could not be read.",
    "news_mismatch": "The news keys do not match the daily.",
    "report_date_closed": "This business date is outside the allowed report window.",
    "seen_incomplete": "The broadcast state was only partly updated. Retry the same commit.",
    "seen_failed": "The broadcast state could not be updated.",
    "path_escape": "The report path is not inside this state root.",
    "offset_past_end": "offset is past the end of the daily.",
    "invalid_offset": "offset must be a nonnegative integer.",
    "invalid_max_chars": "max_chars must be an integer from 0 to 32000.",
    "response_exceeds_bound": "The page does not fit in the response bound.",
    "report_failed": "The report operation did not finish.",
    "report_timeout": "The report operation did not finish before the timeout.",
}


def operation_timeout_seconds(action: str) -> float:
    """Outer wait for one report action. Save includes the local validator."""
    if action == "save":
        return 60.0
    return 30.0


def first_report_date() -> str | None | dict[str, Any]:
    """Operator floor for a new formal daily. Unset means no extra floor.

    The value is a business date, not a secret. A blank value is ignored.
    A value that is not a real YYYY-MM-DD date fails closed.
    """
    raw = os.environ.get(_FIRST_DATE_ENV, "").strip()
    if not raw:
        return None
    parsed = _iso_date(raw)
    if parsed is None:
        return _error("config_invalid")
    return parsed


def _overlay_card_renderer() -> Any:
    """Load only the overlay card renderer from the file next to this module.

    The overlay module defines send helpers, but importing it does not read
    config or send a card. Callers use ``markdown_to_card_elements`` only.
    """
    global _CARD_RENDERER
    renderer = _CARD_RENDERER
    if renderer is not None:
        return renderer
    spec = importlib.util.spec_from_file_location("_ai_finance_overlay_card", _OVERLAY_CARD)
    if spec is None or spec.loader is None:
        raise ImportError("card renderer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded = getattr(module, "markdown_to_card_elements", None)
    if not callable(loaded):
        raise ImportError("card renderer")
    _CARD_RENDERER = loaded
    return loaded


def render_daily_card(markdown: str, batch_id: str) -> dict[str, Any]:
    """Render one card with the checked-in overlay renderer, independent of cwd."""
    card = _overlay_card_renderer()(markdown, CARD_TITLE.format(date=batch_id))
    if not isinstance(card, dict):
        raise TypeError("card")
    return card


def parse_report_request(body: object) -> dict[str, Any]:
    """Accept one report action. Paths, models, and recipients are refused."""
    if not isinstance(body, dict):
        return _error("invalid_request")
    action = body.get("action")
    if action not in _ACTIONS:
        if any(not isinstance(key, str) for key in body):
            return _error("invalid_request")
        return _error("invalid_action" if "action" in body else "invalid_request")
    allowed = set(_COMMON_KEYS)
    if action == "save":
        allowed |= {"markdown", "news_events"}
    elif action == "get":
        allowed |= {"offset", "max_chars"}
    else:
        allowed |= {"body_sha256", "content_sha256"}
    unknown = [key for key in body if key not in allowed]
    if unknown:
        return _error("unknown_field")
    batch_id = _iso_date(body.get("batch_id"))
    if batch_id is None:
        return _error("invalid_batch")
    owner = _canonical_uuid(body.get("owner_user_id"))
    user_id = _canonical_uuid(body.get("user_id"))
    if owner is None:
        return _error("invalid_owner")
    if user_id is None or body.get("profile") != PROFILE:
        return _error("invalid_identity" if user_id is None else "profile_mismatch")
    for key in ("automation_execution_id", "workspace_id", "thread_id"):
        if _canonical_uuid(body.get(key)) is None:
            return _error("invalid_identity")
    parsed: dict[str, Any] = {
        "ok": True,
        "action": action,
        "batch_id": batch_id,
        "owner_user_id": owner,
        "user_id": user_id,
        "profile": PROFILE,
    }
    if action == "save":
        if not isinstance(body.get("markdown"), str) or "news_events" not in body:
            return _error("invalid_request")
        parsed["markdown"] = body["markdown"]
        parsed["news_events"] = body["news_events"]
    elif action == "get":
        offset = body["offset"] if "offset" in body else 0
        max_chars = body["max_chars"] if "max_chars" in body else 0
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > 32 * 1024 * 1024:
            return _error("invalid_offset")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 <= max_chars <= MAX_CHARS:
            return _error("invalid_max_chars")
        parsed["offset"] = offset
        parsed["max_chars"] = max_chars
    else:
        body_sha = body.get("body_sha256")
        content_sha = body.get("content_sha256")
        if not isinstance(body_sha, str) or _HEX64.fullmatch(body_sha) is None:
            return _error("hash_mismatch")
        if not isinstance(content_sha, str) or _HEX64.fullmatch(content_sha) is None:
            return _error("hash_mismatch")
        parsed["body_sha256"] = body_sha
        parsed["content_sha256"] = content_sha
    return parsed


def execute_report(parsed: Mapping[str, Any], *, state_root: Path) -> dict[str, Any]:
    """Run one parsed report action against the operator state root."""
    if parsed.get("ok") is not True or parsed.get("action") not in _ACTIONS:
        return _error("invalid_request")
    if parsed["action"] == "save":
        return save_report(
            state_root=state_root,
            batch_id=str(parsed["batch_id"]),
            owner_user_id=str(parsed["owner_user_id"]),
            markdown=parsed["markdown"],
            news_events=parsed["news_events"],
        )
    if parsed["action"] == "get":
        return get_report(
            state_root=state_root,
            batch_id=str(parsed["batch_id"]),
            owner_user_id=str(parsed["owner_user_id"]),
            offset=int(parsed.get("offset", 0)),
            max_chars=int(parsed.get("max_chars", 0)),
        )
    return commit_report_seen(
        state_root=state_root,
        batch_id=str(parsed["batch_id"]),
        owner_user_id=str(parsed["owner_user_id"]),
        body_sha256=str(parsed["body_sha256"]),
        content_sha256=str(parsed["content_sha256"]),
    )


def save_report(
    *,
    state_root: Path,
    batch_id: str,
    owner_user_id: str,
    markdown: str,
    news_events: object,
) -> dict[str, Any]:
    """Validate one daily, render its card, and store that exact pair.

    The same batch and the same bytes reuse the saved record. A different
    daily, news set, or research bundle conflicts and leaves the saved copy.
    """
    closed = _closed_date(batch_id)
    if closed is not None:
        return closed
    root = _require_root(state_root)
    if isinstance(root, dict):
        return root
    if _iso_date(batch_id) is None:
        return _error("invalid_batch")
    owner = _canonical_uuid(owner_user_id)
    if owner is None:
        return _error("invalid_owner")
    if not isinstance(markdown, str):
        return _error("invalid_request")
    bundle = _signals().read_report_bundle(
        state_root=root,
        batch_id=batch_id,
        owner_user_id=owner,
    )
    if bundle.get("ok") is not True:
        return _relay(bundle, batch_id)
    try:
        checked = _check_daily(markdown, news_events, bundle, batch_id)
    except (OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return _error("report_invalid", batch_id=batch_id)
    if checked.get("ok") is not True:
        return checked
    with _report_lock(root, batch_id):
        return _save_locked(root, batch_id, owner, bundle, checked)


def get_report(
    *,
    state_root: Path,
    batch_id: str,
    owner_user_id: str,
    offset: int = 0,
    max_chars: int = 0,
) -> dict[str, Any]:
    """Return the saved daily metadata and original card.

    ``max_chars`` above zero adds one page of the original Markdown. The
    page uses the same response bound as the signal read. No path is returned.
    """
    root = _require_root(state_root)
    if isinstance(root, dict):
        return root
    if _iso_date(batch_id) is None:
        return _error("invalid_batch")
    owner = _canonical_uuid(owner_user_id)
    if owner is None:
        return _error("invalid_owner")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _error("invalid_offset")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 0 <= max_chars <= MAX_CHARS:
        return _error("invalid_max_chars")
    with _report_lock(root, batch_id):
        loaded = _load_saved(root, batch_id, owner)
        if loaded.get("ok") is not True:
            return loaded
        public = _public_saved(loaded, action="get")
        if max_chars == 0 and offset == 0:
            return public
        return _page_body(public, loaded["markdown"], offset, max_chars)


def commit_report_seen(
    *,
    state_root: Path,
    batch_id: str,
    owner_user_id: str,
    body_sha256: str,
    content_sha256: str,
) -> dict[str, Any]:
    """Merge the saved broadcast into news seen and GitHub seen.

    The caller has already confirmed delivery. This function checks the saved
    hashes, then updates the two seen files and writes the completion record.
    A repeated call keeps the original broadcast time.
    """
    closed = _closed_date(batch_id)
    if closed is not None:
        return closed
    root = _require_root(state_root)
    if isinstance(root, dict):
        return root
    if _iso_date(batch_id) is None:
        return _error("invalid_batch")
    owner = _canonical_uuid(owner_user_id)
    if owner is None:
        return _error("invalid_owner")
    if not isinstance(body_sha256, str) or _HEX64.fullmatch(body_sha256) is None:
        return _error("hash_mismatch")
    if not isinstance(content_sha256, str) or _HEX64.fullmatch(content_sha256) is None:
        return _error("hash_mismatch")
    with _report_lock(root, batch_id):
        loaded = _load_saved(root, batch_id, owner)
        if loaded.get("ok") is not True:
            return loaded
        if loaded["body_sha256"] != body_sha256 or loaded["content_sha256"] != content_sha256:
            return _error("hash_mismatch", batch_id=batch_id)
        with _seen_lock(root):
            return _commit_seen_locked(root, batch_id, loaded)


def card_content_sha256(cards: list[dict[str, Any]]) -> str:
    """Match ``automation_report_content_sha256`` for one card batch.

    The host does not import the database module. The backend checks this
    digest with that function before it accepts delivery.
    """
    canonical = {
        "profile": PROFILE,
        "content_kind": "cards",
        "cards": cards,
        "failure_text": None,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _closed_date(batch_id: str) -> dict[str, Any] | None:
    floor = first_report_date()
    if isinstance(floor, dict):
        return floor
    if floor is not None and _iso_date(batch_id) is not None and batch_id < floor:
        return _error("report_date_closed", batch_id=batch_id)
    return None


def _signals() -> Any:
    try:
        from scripts.dalton import ai_finance_signals as signals
    except ImportError:
        import ai_finance_signals as signals
    return signals


def _validators() -> tuple[Any, Any, Any]:
    try:
        from scripts.dalton.ai_finance.ai_finance_daily_validate import (
            validate_github_tracking,
            validate_markdown,
            validate_records,
        )
    except ImportError:
        from ai_finance.ai_finance_daily_validate import (
            validate_github_tracking,
            validate_markdown,
            validate_records,
        )
    return validate_markdown, validate_github_tracking, validate_records


def _check_daily(
    markdown: str,
    news_events: object,
    bundle: Mapping[str, Any],
    batch_id: str,
) -> dict[str, Any]:
    encoded = markdown.encode("utf-8")
    if len(encoded) > MAX_MARKDOWN_BYTES or "\x00" in markdown:
        return _error("report_invalid", batch_id=batch_id)
    structure = _structure_problem(markdown)
    if structure is not None:
        return _error("report_invalid", batch_id=batch_id, reason=structure)
    news_keys = _news_keys(news_events)
    if isinstance(news_keys, dict):
        return news_keys
    github_doc = bundle.get("github_candidates")
    decisions = bundle.get("github_decisions")
    scan = bundle.get("earnings_scan")
    dispositions = bundle.get("earnings_dispositions")
    records = bundle.get("signal_records")
    if not all(isinstance(item, dict) for item in (github_doc, decisions, scan, dispositions, records)):
        return _error("commit_inconsistent", batch_id=batch_id)
    with tempfile.TemporaryDirectory(prefix="ai-finance-report-") as temporary:
        directory = Path(temporary)
        markdown_path = directory / "daily.md"
        candidates_path = directory / "github_candidates.json"
        decisions_path = directory / "github_decisions.json"
        scan_path = directory / "earnings_scan.json"
        dispositions_path = directory / "earnings_dispositions.json"
        signals_path = directory / "signal_records.json"
        markdown_path.write_bytes(encoded)
        candidates_path.write_bytes(_canonical(github_doc))
        decisions_path.write_bytes(_canonical(decisions))
        scan_path.write_bytes(_canonical(scan))
        dispositions_path.write_bytes(_canonical(dispositions))
        signals_path.write_bytes(_canonical(records))
        violations: list[str] = []
        validate_markdown, validate_github, validate_records = _validators()
        metrics = validate_markdown(markdown_path, violations)
        metrics.update(validate_github(markdown_path, candidates_path, violations, decisions_path))
        metrics.update(validate_records(
            scan_path, dispositions_path, signals_path, violations, decisions_path,
        ))
    if violations or not isinstance(metrics, dict):
        return _invalid(batch_id, violations)
    news_items = metrics.get("news_items")
    if isinstance(news_items, bool) or not isinstance(news_items, int) or news_items != len(news_keys):
        return _error("news_mismatch", batch_id=batch_id)
    broadcast = _github_broadcast(markdown, github_doc, decisions)
    if isinstance(broadcast, dict) and broadcast.get("ok") is False:
        return broadcast
    assert isinstance(broadcast, dict)
    try:
        card = render_daily_card(markdown, batch_id)
    except (OSError, TypeError, ValueError, ImportError):
        return _error("report_invalid", batch_id=batch_id)
    card_bytes = json.dumps([card], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(card_bytes) > MAX_CARD_JSON_BYTES:
        return _error("report_too_large", batch_id=batch_id)
    started = _parse_instant(bundle.get("started_at"))
    if started is None:
        return _error("clock_inconsistent", batch_id=batch_id)
    body_sha = hashlib.sha256(encoded).hexdigest()
    content_sha = card_content_sha256([card])
    rows = list(broadcast["rows"])
    identity = _identity_sha(
        body_sha,
        news_keys,
        rows,
        str(bundle["bundle_hash"]),
        str(bundle["generation"]),
    )
    return {
        "ok": True,
        "markdown": markdown,
        "encoded": encoded,
        "card": card,
        "news_keys": news_keys,
        "rows": rows,
        "complete_through": broadcast["complete_through"],
        "broadcast_at": started.timestamp(),
        "body_sha256": body_sha,
        "content_sha256": content_sha,
        "identity_sha256": identity,
        "bundle_hash": bundle["bundle_hash"],
        "generation": bundle["generation"],
        "manifest_sha256": bundle["manifest_sha256"],
        "compare_sha256": bundle["compare_sha256"],
        "outcome_sha256": bundle["outcome_sha256"],
    }


def _structure_problem(markdown: str) -> str | None:
    if markdown.count(MAIN_TITLE) != 1:
        return "main_title"
    positions = [markdown.find(MAIN_TITLE)]
    for heading in HEADINGS:
        if markdown.count(heading) != 1:
            return "heading"
        positions.append(markdown.find(heading))
    if markdown.count(CROSS_TITLE) != 1:
        return "cross_title"
    positions.append(markdown.find(CROSS_TITLE))
    ordered = [markdown.find(MAIN_TITLE), *[markdown.find(item) for item in HEADINGS]]
    if ordered != sorted(ordered):
        return "heading_order"
    cross_at = markdown.find(CROSS_TITLE)
    if not (markdown.find(HEADINGS[2]) < cross_at < markdown.find(HEADINGS[3])):
        return "cross_title"
    for start, end in (
        (HEADINGS[0], HEADINGS[1]),
        (HEADINGS[1], HEADINGS[2]),
        (HEADINGS[2], HEADINGS[3]),
    ):
        section = markdown.split(start, 1)[1].split(end, 1)[0]
        numbers = [int(match) for match in _NUMBERED.findall(section)]
        if numbers and numbers[0] != 1:
            return "numbering"
    return None


def _news_keys(value: object) -> list[str] | dict[str, Any]:
    """Accept the imported seen shape: hex digest keys and numeric timestamps.

    The numeric values prove the shape. The saved broadcast time comes from
    the batch clock, not from these numbers.
    """
    if not isinstance(value, dict) or len(value) > 8:
        return _error("invalid_request")
    keys: list[str] = []
    for key, stamp in value.items():
        if not isinstance(key, str) or _HEX64.fullmatch(key) is None:
            return _error("invalid_request")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(float(stamp)):
            return _error("invalid_request")
        keys.append(key)
    return sorted(set(keys))


def _github_broadcast(
    markdown: str,
    github_doc: Mapping[str, Any],
    decisions: Mapping[str, Any],
) -> dict[str, Any]:
    section = markdown.split(HEADINGS[1], 1)[1].split(HEADINGS[2], 1)[0]
    shown = _GITHUB_ITEM.findall(section)
    if len(shown) != len({name.lower() for name in shown}):
        return _error("report_invalid", reason="github_duplicate")
    records = decisions.get("records")
    candidates = github_doc.get("candidates")
    if not isinstance(records, list) or not isinstance(candidates, list):
        return _error("commit_inconsistent")
    by_repo = {}
    for item in candidates:
        if isinstance(item, dict) and isinstance(item.get("full_name"), str):
            by_repo[item["full_name"].lower()] = item
    displayed: list[str] = []
    for record in records:
        if not isinstance(record, dict) or record.get("displayed") is not True:
            continue
        if record.get("decision") != "include" or not isinstance(record.get("repo"), str):
            return _error("report_invalid", reason="github_displayed")
        displayed.append(record["repo"].lower())
    if len(displayed) != len(set(displayed)) or set(displayed) != {name.lower() for name in shown}:
        return _error("report_invalid", reason="github_displayed")
    through = github_doc.get("complete_through")
    if displayed and _iso_date(through) is None:
        return _error("report_invalid", reason="complete_through")
    if not displayed and through is not None and _iso_date(through) is None:
        return _error("report_invalid", reason="complete_through")
    rows: list[dict[str, Any]] = []
    for name in sorted(displayed):
        item = by_repo.get(name)
        momentum = item.get("momentum") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(momentum, dict):
            return _error("report_invalid", reason="github_momentum")
        gain = momentum.get("daily_gain")
        phase = momentum.get("phase")
        item_through = momentum.get("complete_through")
        official = item.get("full_name")
        if isinstance(gain, bool) or not isinstance(gain, int) or gain < 0:
            return _error("report_invalid", reason="github_momentum")
        if not isinstance(phase, str) or _PHASE.fullmatch(phase) is None:
            return _error("report_invalid", reason="github_momentum")
        if not isinstance(official, str) or _REPO.fullmatch(official) is None:
            return _error("report_invalid", reason="github_momentum")
        if item_through is not None and item_through != through:
            return _error("report_invalid", reason="complete_through")
        rows.append({
            "repo": official,
            "last_daily_gain": gain,
            "last_bucket": phase,
            "last_complete_through": through,
        })
    return {
        "ok": True,
        "rows": rows,
        "complete_through": through if isinstance(through, str) else None,
    }


def _identity_sha(
    body_sha: str,
    news_keys: list[str],
    rows: list[dict[str, Any]],
    bundle_hash: str,
    generation: str,
) -> str:
    payload = {
        "body_sha256": body_sha,
        "bundle_hash": bundle_hash,
        "generation": generation,
        "github": rows,
        "news": news_keys,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _save_locked(
    root: Path,
    batch_id: str,
    owner: str,
    bundle: Mapping[str, Any],
    checked: Mapping[str, Any],
) -> dict[str, Any]:
    existing = _load_saved(root, batch_id, owner)
    if existing.get("ok") is True:
        if existing["identity_sha256"] == checked["identity_sha256"]:
            public = _public_saved(existing, action="save")
            public["reused"] = True
            return public
        return _error("content_conflict", batch_id=batch_id)
    if existing.get("code") not in {None, "report_missing"}:
        return existing
    directory = _report_dir(root, batch_id)
    if isinstance(directory, dict):
        return directory
    broadcast = {
        "schema": 1,
        "news_keys": list(checked["news_keys"]),
        "github": list(checked["rows"]),
        "complete_through": checked["complete_through"],
        "broadcast_at": checked["broadcast_at"],
    }
    report = {
        "schema": 1,
        "batch_id": batch_id,
        "profile": PROFILE,
        "owner_user_id": owner,
        "report_run_id": f"ai-finance:{batch_id}",
        "bundle_hash": checked["bundle_hash"],
        "generation": checked["generation"],
        "manifest_sha256": checked["manifest_sha256"],
        "compare_sha256": checked["compare_sha256"],
        "outcome_sha256": checked["outcome_sha256"],
        "body_sha256": checked["body_sha256"],
        "content_sha256": checked["content_sha256"],
        "identity_sha256": checked["identity_sha256"],
        "broadcast_sha256": hashlib.sha256(_canonical(broadcast)).hexdigest(),
        "news_count": len(checked["news_keys"]),
        "github_count": len(checked["rows"]),
    }
    _atomic_write(directory / "body.md", checked["encoded"])
    _atomic_write(directory / "card.json", _canonical(checked["card"]))
    _atomic_write(directory / "broadcast.json", _canonical(broadcast))
    _atomic_write(directory / "report.json", _canonical(report))
    loaded = _load_saved(root, batch_id, owner)
    if loaded.get("ok") is not True or loaded["identity_sha256"] != checked["identity_sha256"]:
        return _error("commit_inconsistent", batch_id=batch_id)
    public = _public_saved(loaded, action="save")
    public["reused"] = False
    return public


def _load_saved(root: Path, batch_id: str, owner: str) -> dict[str, Any]:
    directory = root / "reports" / batch_id
    if _real_dir(directory) is None:
        return _error("report_missing", batch_id=batch_id)
    if _real_dir(directory) is not True:
        return _error("path_escape", batch_id=batch_id)
    report = _read_json(directory / "report.json", 64 * 1024)
    if report is None:
        if _read_file(directory / "report.json", max_bytes=64 * 1024) is None and not (directory / "report.json").exists():
            return _error("report_missing", batch_id=batch_id)
        return _error("commit_inconsistent", batch_id=batch_id)
    if report.get("owner_user_id") != owner:
        return _error("owner_mismatch", batch_id=batch_id)
    if not _report_shape(report, batch_id, owner):
        return _error("commit_inconsistent", batch_id=batch_id)
    body = _read_file(directory / "body.md", max_bytes=MAX_MARKDOWN_BYTES)
    card_raw = _read_file(directory / "card.json", max_bytes=MAX_CARD_JSON_BYTES)
    broadcast_raw = _read_file(directory / "broadcast.json", max_bytes=256 * 1024)
    if body is None or card_raw is None or broadcast_raw is None:
        return _error("commit_inconsistent", batch_id=batch_id)
    if hashlib.sha256(body).hexdigest() != report["body_sha256"]:
        return _error("hash_mismatch", batch_id=batch_id)
    if hashlib.sha256(broadcast_raw).hexdigest() != report["broadcast_sha256"]:
        return _error("hash_mismatch", batch_id=batch_id)
    try:
        card = json.loads(card_raw.decode("utf-8"))
        broadcast = json.loads(broadcast_raw.decode("utf-8"))
        markdown = body.decode("utf-8")
    except (UnicodeError, json.JSONDecodeError):
        return _error("commit_inconsistent", batch_id=batch_id)
    if not isinstance(card, dict) or not isinstance(broadcast, dict):
        return _error("commit_inconsistent", batch_id=batch_id)
    if card_content_sha256([card]) != report["content_sha256"]:
        return _error("hash_mismatch", batch_id=batch_id)
    completion = _completion(directory, report)
    return {
        "ok": True,
        "report": report,
        "markdown": markdown,
        "card": card,
        "broadcast": broadcast,
        "body_sha256": report["body_sha256"],
        "content_sha256": report["content_sha256"],
        "identity_sha256": report["identity_sha256"],
        "seen_status": "complete" if completion else "pending",
    }


def _report_shape(data: Mapping[str, Any], batch_id: str, owner: str) -> bool:
    if data.get("schema") != 1 or data.get("batch_id") != batch_id or data.get("profile") != PROFILE:
        return False
    if data.get("owner_user_id") != owner or data.get("report_run_id") != f"ai-finance:{batch_id}":
        return False
    for key in (
        "bundle_hash",
        "manifest_sha256",
        "compare_sha256",
        "outcome_sha256",
        "body_sha256",
        "content_sha256",
        "identity_sha256",
        "broadcast_sha256",
    ):
        if not isinstance(data.get(key), str) or _HEX64.fullmatch(data[key]) is None:
            return False
    generation = data.get("generation")
    if not isinstance(generation, str) or not re.fullmatch(r"[a-f0-9]{32}", generation):
        return False
    for key in ("news_count", "github_count"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    return True


def _completion(directory: Path, report: Mapping[str, Any]) -> bool:
    data = _read_json(directory / "seen_commit.json", 64 * 1024)
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("complete") is not True:
        return False
    return (
        data.get("batch_id") == report["batch_id"]
        and data.get("body_sha256") == report["body_sha256"]
        and data.get("content_sha256") == report["content_sha256"]
        and data.get("broadcast_sha256") == report["broadcast_sha256"]
    )


def _public_saved(loaded: Mapping[str, Any], *, action: str) -> dict[str, Any]:
    report = loaded["report"]
    return {
        "ok": True,
        "action": action,
        "code": "ok",
        "batch_id": report["batch_id"],
        "profile": PROFILE,
        "owner_user_id": report["owner_user_id"],
        "report_run_id": report["report_run_id"],
        "bundle_hash": report["bundle_hash"],
        "generation": report["generation"],
        "manifest_sha256": report["manifest_sha256"],
        "body_sha256": report["body_sha256"],
        "content_sha256": report["content_sha256"],
        "seen_status": loaded["seen_status"],
        "news_count": report["news_count"],
        "github_count": report["github_count"],
        "card": loaded["card"],
        "reused": False,
    }


def _page_body(public: dict[str, Any], markdown: str, offset: int, max_chars: int) -> dict[str, Any]:
    total = len(markdown)
    if offset > total:
        return _error(
            "offset_past_end",
            batch_id=public["batch_id"],
            offset=offset,
            total_chars=total,
            body_sha256=public["body_sha256"],
        )
    window = min(max_chars, total - offset)
    shell = dict(public)
    shell["action"] = "get"
    shell["offset"] = offset
    shell["max_chars"] = max_chars
    shell["total_chars"] = total
    lo = 0
    hi = window
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        page = _slice_page(shell, markdown, offset, mid, total)
        if len(_encode(page)) <= MAX_RESPONSE_BYTES:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if window > 0 and best == 0:
        return _error("response_exceeds_bound", batch_id=public["batch_id"])
    return _slice_page(shell, markdown, offset, best, total)


def _slice_page(shell: Mapping[str, Any], full: str, offset: int, count: int, total: int) -> dict[str, Any]:
    end = offset + count
    page = dict(shell)
    page["text"] = full[offset:end]
    page["next_offset"] = None if end >= total else end
    page["complete"] = end >= total
    return page


def _commit_seen_locked(root: Path, batch_id: str, loaded: Mapping[str, Any]) -> dict[str, Any]:
    report = loaded["report"]
    broadcast = loaded["broadcast"]
    news_keys = broadcast.get("news_keys")
    rows = broadcast.get("github")
    broadcast_at = broadcast.get("broadcast_at")
    if not isinstance(news_keys, list) or not isinstance(rows, list):
        return _error("commit_inconsistent", batch_id=batch_id)
    if isinstance(broadcast_at, bool) or not isinstance(broadcast_at, (int, float)) or not math.isfinite(float(broadcast_at)):
        return _error("commit_inconsistent", batch_id=batch_id)
    directory = root / "reports" / batch_id
    already = _completion(directory, report)
    news_path = root / "seen.json"
    github_path = root / "github_seen.json"
    try:
        news = _merge_news(news_path, [str(item) for item in news_keys], float(broadcast_at))
        github = _merge_github(github_path, rows, float(broadcast_at))
    except _SeenRejected as exc:
        return _error(exc.code, batch_id=batch_id)
    if isinstance(news, dict) and news.get("ok") is False:
        return news
    if isinstance(github, dict) and github.get("ok") is False:
        return github
    assert isinstance(news, bytes) and isinstance(github, bytes)
    if not _replace_seen(news_path, news):
        return _error("seen_incomplete", batch_id=batch_id)
    if not _replace_seen(github_path, github):
        return _error("seen_incomplete", batch_id=batch_id)
    completion = {
        "schema": 1,
        "batch_id": batch_id,
        "body_sha256": report["body_sha256"],
        "content_sha256": report["content_sha256"],
        "broadcast_sha256": report["broadcast_sha256"],
        "broadcast_at": float(broadcast_at),
        "complete": True,
    }
    if not _replace_seen(directory / "seen_commit.json", _canonical(completion)):
        return _error("seen_incomplete", batch_id=batch_id)
    if not _completion(directory, report):
        return _error("seen_incomplete", batch_id=batch_id)
    return {
        "ok": True,
        "action": "commit_seen",
        "code": "ok",
        "batch_id": batch_id,
        "profile": PROFILE,
        "body_sha256": report["body_sha256"],
        "content_sha256": report["content_sha256"],
        "seen_status": "complete",
        "reused": already,
        "news_count": report["news_count"],
        "github_count": report["github_count"],
    }


class _SeenRejected(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _merge_news(path: Path, keys: list[str], broadcast_at: float) -> bytes | dict[str, Any]:
    current = _read_seen_object(path)
    if isinstance(current, dict) and current.get("ok") is False:
        return current
    assert isinstance(current, dict)
    merged: dict[str, Any] = dict(current)
    for key in keys:
        if _HEX64.fullmatch(key) is None:
            raise _SeenRejected("commit_inconsistent")
        if key not in merged:
            merged[key] = broadcast_at
    cutoff = broadcast_at - NEWS_KEEP_DAYS * 86400
    kept: dict[str, Any] = {}
    for key, stamp in merged.items():
        if isinstance(key, str) and _HEX64.fullmatch(key) and _finite_number(stamp):
            if float(stamp) >= cutoff:
                kept[key] = stamp
            continue
        kept[key] = stamp
    return _pretty(kept)


def _merge_github(path: Path, rows: list[object], broadcast_at: float) -> bytes | dict[str, Any]:
    current = _read_seen_object(path)
    if isinstance(current, dict) and current.get("ok") is False:
        return current
    assert isinstance(current, dict)
    if path.exists() and "repos" not in current:
        return _error("commit_inconsistent")
    repos = current.get("repos", {})
    if not isinstance(repos, dict):
        return _error("commit_inconsistent")
    kept: dict[str, Any] = {name: entry for name, entry in repos.items() if isinstance(name, str)}
    protected: set[str] = set()
    cutoff = broadcast_at - GITHUB_KEEP_DAYS * 86400
    for row in rows:
        if not isinstance(row, dict):
            raise _SeenRejected("commit_inconsistent")
        repo = row.get("repo")
        through = row.get("last_complete_through")
        gain = row.get("last_daily_gain")
        phase = row.get("last_bucket")
        if not isinstance(repo, str) or _REPO.fullmatch(repo) is None or _iso_date(through) is None:
            raise _SeenRejected("commit_inconsistent")
        if isinstance(gain, bool) or not isinstance(gain, int) or not isinstance(phase, str):
            raise _SeenRejected("commit_inconsistent")
        existing_name = _repo_key(kept, repo)
        existing = kept.get(existing_name) if existing_name is not None else None
        if isinstance(existing, dict):
            prior = existing.get("last_complete_through")
            if isinstance(prior, str) and prior >= str(through):
                protected.add(existing_name)
                continue
            if existing_name != repo:
                kept.pop(existing_name, None)
        kept[repo] = {
            "last_ts": broadcast_at,
            "last_daily_gain": gain,
            "last_bucket": phase,
            "last_complete_through": through,
        }
    pruned = {
        name: entry
        for name, entry in kept.items()
        if name in protected or not (
            isinstance(entry, dict)
            and _finite_number(entry.get("last_ts"))
            and float(entry["last_ts"]) < cutoff
        )
    }
    merged = {key: value for key, value in current.items() if key != "repos"}
    merged["repos"] = pruned
    return _pretty(merged)


def _repo_key(repos: Mapping[str, Any], name: str) -> str | None:
    if name in repos:
        return name
    lowered = name.lower()
    for key in repos:
        if key.lower() == lowered:
            return key
    return None


def _read_seen_object(path: Path) -> dict[str, Any]:
    if _is_symlink(path):
        return _error("path_escape")
    if not path.exists():
        return {}
    raw = _read_file(path, max_bytes=MAX_SEEN_BYTES)
    if raw is None:
        return _error("commit_inconsistent")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _error("commit_inconsistent")
    if not isinstance(data, dict):
        return _error("commit_inconsistent")
    return data


def _replace_seen(path: Path, payload: bytes) -> bool:
    try:
        if _is_symlink(path):
            return False
        _atomic_write(path, payload)
    except OSError:
        return False
    return True


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _report_dir(root: Path, batch_id: str) -> Path | dict[str, Any]:
    parent = root / "reports"
    if not _ensure_real_dir(parent):
        return _error("path_escape", batch_id=batch_id)
    directory = parent / batch_id
    if directory.exists():
        if _real_dir(directory) is not True:
            return _error("path_escape", batch_id=batch_id)
        return directory
    directory.mkdir()
    if _real_dir(directory) is not True:
        return _error("path_escape", batch_id=batch_id)
    return directory


def _relay(payload: Mapping[str, Any], batch_id: str) -> dict[str, Any]:
    code = payload.get("code")
    if not isinstance(code, str) or code not in _MESSAGES:
        code = "not_ready" if payload.get("status") in {"open", "ingesting"} else "report_failed"
    extra: dict[str, Any] = {"batch_id": batch_id}
    if payload.get("status") in {"open", "ingesting", "ingested"}:
        extra["status"] = payload["status"]
    return _error(code, **extra)


def _invalid(batch_id: str, violations: list[str]) -> dict[str, Any]:
    safe = [_safe_violation(item) for item in violations[:8] if _safe_violation(item)]
    return _error("report_invalid", batch_id=batch_id, violation_count=len(violations), violations=safe)


_VIOLATION_CREDENTIAL = re.compile(
    r"(?i)(?<![\w.-])(?:"
    r"bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|sk-[A-Za-z0-9-]{16,}"
    r"|(?:api[_-]?key|secret|token|password|passwd|authorization)(?![\w.-])\s*[:=]\s*[^\s,，。;]+"
    r")"
)
_VIOLATION_URL = re.compile(r"(?i)\b(?:https?|file)://[^\s,，。;]+")
_VIOLATION_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s,，。;]*")
_VIOLATION_UNC = re.compile(r"\\\\[^\s,，。;]*")
_VIOLATION_TILDE = re.compile(r"~[/\\][^\s,，。;]*")
_VIOLATION_ABSOLUTE = re.compile(r"(?<![\w.-])/(?:[^\s,，。;\\])+")
_VIOLATION_SLASH_RUN = re.compile(r"[^\s,，。;]*[/\\][^\s,，。;]*")
_VIOLATION_EDGE = "([{\"'.,:;)]}"


def _violation_repo(token: str) -> bool:
    """True for one bounded owner/repo, not a path or a longer slash chain."""
    if len(token) > 201 or token.count("/") != 1 or "\\" in token:
        return False
    owner, repo = token.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        return False
    if not re.fullmatch(r"[\w.-]{1,100}", owner) or not re.fullmatch(r"[\w.-]{1,100}", repo):
        return False
    return bool(re.search(r"[A-Za-z0-9]", owner) and re.search(r"[A-Za-z0-9]", repo))


def _keep_violation_repo(match: re.Match[str]) -> str:
    token = match.group(0).strip(_VIOLATION_EDGE)
    if _violation_repo(token):
        return token
    return " "


def _clip_violation(text: str) -> str:
    if len(text) <= 180:
        return text
    clipped = text[:180]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.strip()


def _violation_residue_unsafe(text: str) -> bool:
    if "~/" in text or "~\\" in text or _VIOLATION_CREDENTIAL.search(text):
        return True
    residue = _VIOLATION_SLASH_RUN.sub(
        lambda match: " " if _violation_repo(match.group(0).strip(_VIOLATION_EDGE)) else match.group(0),
        text,
    )
    return "/" in residue or "\\" in residue


def _safe_violation(text: str) -> str:
    """Keep validator owner/repo names. Drop filesystem paths and credential material."""
    cleaned = _VIOLATION_CREDENTIAL.sub(" ", text)
    cleaned = _VIOLATION_URL.sub(" ", cleaned)
    cleaned = _VIOLATION_DRIVE.sub(" ", cleaned)
    cleaned = _VIOLATION_UNC.sub(" ", cleaned)
    cleaned = _VIOLATION_TILDE.sub(" ", cleaned)
    cleaned = _VIOLATION_ABSOLUTE.sub(" ", cleaned)
    cleaned = _VIOLATION_SLASH_RUN.sub(_keep_violation_repo, cleaned)
    cleaned = _clip_violation(re.sub(r"\s+", " ", cleaned).strip())
    cleaned = _VIOLATION_SLASH_RUN.sub(_keep_violation_repo, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 180:
        cleaned = _clip_violation(cleaned)
    if not cleaned or _violation_residue_unsafe(cleaned):
        return ""
    return cleaned[:180]


def _error(code: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "code": code,
        "error": _MESSAGES.get(code, "The report operation did not finish."),
    }
    for key, value in extra.items():
        if key in {"text", "content", "body", "path", "state_root", "markdown", "card"}:
            continue
        payload[key] = value
    if code == "report_invalid" and isinstance(extra.get("reason"), str):
        payload["violations"] = [_safe_violation(extra["reason"])]
        payload["violation_count"] = 1
    return payload


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value.strip()) != 36:
        return None
    text = value.strip()
    try:
        import uuid
        parsed = uuid.UUID(text)
    except (TypeError, ValueError, AttributeError):
        return None
    canonical = str(parsed)
    if canonical != text.lower():
        return None
    return canonical


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64 or "/" in value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _require_root(state_root: Path) -> Path | dict[str, Any]:
    if not isinstance(state_root, Path) or not state_root.is_absolute():
        return _error("config_invalid")
    if any(part in {".openclaw", ".feishu-daily-ai-finance"} for part in state_root.parts):
        return _error("config_invalid")
    try:
        if state_root.is_symlink() or not state_root.is_dir():
            return _error("config_invalid")
    except OSError:
        return _error("config_invalid")
    return state_root


def _thread_lock(name: str) -> threading.Lock:
    with _THREAD_GUARD:
        lock = _THREAD_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[name] = lock
        return lock


@contextlib.contextmanager
def _flock(path: Path, thread_lock: threading.Lock) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with thread_lock:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _report_lock(root: Path, batch_id: str) -> Iterator[None]:
    with _flock(root / "locks" / f"reports-{batch_id}.lock", _thread_lock(f"report:{batch_id}")):
        yield


@contextlib.contextmanager
def _seen_lock(root: Path) -> Iterator[None]:
    with _flock(root / "locks" / "ai-finance-seen.lock", _thread_lock("seen")):
        yield


def _ensure_real_dir(path: Path) -> bool:
    if path.exists():
        return _real_dir(path) is True
    path.mkdir(parents=True)
    return _real_dir(path) is True


def _real_dir(path: Path) -> bool | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    return True


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _read_json(path: Path, max_bytes: int) -> dict[str, Any] | None:
    raw = _read_file(path, max_bytes=max_bytes)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _read_file(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                return None
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        temporary = ""
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _canonical(document: object) -> bytes:
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _pretty(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _encode(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
