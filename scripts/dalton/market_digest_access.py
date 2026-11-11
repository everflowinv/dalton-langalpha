"""Page one prepared market-digest batch.

``state_root`` is the operator path. The request body cannot name a file.
Only ``read_inputs`` reads the batch. This module does not write, and it
does not log batch text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from scripts.dalton.market_digest_inputs import PrepareConfigError, read_inputs
except ImportError:
    from market_digest_inputs import PrepareConfigError, read_inputs

SOURCE_READ_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_CHARS = 16000
MAX_CHARS = 32000
MAX_RESPONSE_BYTES = 128 * 1024

_REQUEST_KEYS = frozenset({"period", "date", "offset", "max_chars"})
_READY = frozenset({"ready", "ready_degraded"})
_HASH_KEYS = (
    "input_sha256",
    "emails_sha256",
    "tmtb_sha256",
    "digest_source_sha256",
    "tmtb_source_sha256",
)
_FUNDA_FIELDS = (
    ("research_eligible_count", "int"),
    ("research_excluded_count", "int"),
    ("status", "str"),
    ("received", "int"),
    ("pending_documents", "int"),
    ("index_pending", "bool"),
)
_TMTB_FIELDS = (
    ("status", "str"),
    ("count", "int"),
    ("new_count", "int"),
    ("pending_snippets_count", "int"),
    ("quota_hit", "bool"),
    ("failure_count", "int"),
    ("degraded", "bool"),
    ("reason", "str"),
    ("summary_only", "bool"),
)
_IMAGE_FIELDS = (
    ("pending", "int"),
    ("historical_backlog", "int"),
    ("status", "str"),
)
_HEX = frozenset("0123456789abcdef")
_DOCUMENT_KEYS = ("text", "emails", "tmtb_docs", "pending_snippets", "body", "content")


def read_market_digest_page(state_root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """Return one Unicode page, or a whole-body error with no document text.

    ``body`` accepts only ``period``, ``date``, ``offset``, and ``max_chars``.
    ``offset`` defaults to 0 and ``max_chars`` to 16000. A usable ``ready`` or
    ``ready_degraded`` batch is serialized in full; the returned ``text`` is a
    character slice of that same string. Invalid and oversize batches keep
    allowlisted metadata only.
    """
    parsed, rejected = _parse_request(body)
    if rejected is not None:
        return rejected
    assert parsed is not None
    if not isinstance(state_root, Path):
        return _error("config_invalid", "state_root must be an absolute existing directory.")
    try:
        public = read_inputs(
            state_root,
            parsed["period"],
            date=parsed["date"],
            max_bytes=SOURCE_READ_MAX_BYTES,
        )
    except PrepareConfigError:
        return _error("config_invalid", "state_root must be an absolute existing directory.")
    except Exception:
        return _error("read_failed", "The prepared batch could not be read.")
    if not isinstance(public, Mapping):
        return _error("read_failed", "The prepared batch could not be read.")
    return _page_public(public, parsed)


def _parse_request(
    body: object,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(body, Mapping):
        return None, _error("invalid_request", "Request body must be an object.")
    if set(body) - _REQUEST_KEYS:
        return None, _error(
            "unknown_field",
            "Request keys must be period, date, offset, and max_chars.",
        )
    if "period" not in body or "date" not in body:
        return None, _error("invalid_request", "period and date are required.")
    period = body.get("period")
    if not isinstance(period, str) or period.strip() not in {"AM", "PM"}:
        return None, _error("invalid_period", "period must be AM or PM.")
    date = _iso_date(body.get("date"))
    if date is None:
        return None, _error("invalid_date", "date must be a real YYYY-MM-DD.")
    offset = body["offset"] if "offset" in body else 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return None, _error("invalid_offset", "offset must be a nonnegative integer.")
    max_chars = body["max_chars"] if "max_chars" in body else DEFAULT_MAX_CHARS
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= MAX_CHARS
    ):
        return None, _error(
            "invalid_max_chars",
            "max_chars must be an integer from 1 to 32000.",
        )
    return {
        "period": period.strip(),
        "date": date,
        "offset": offset,
        "max_chars": max_chars,
    }, None


def _page_public(public: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any]:
    period = str(parsed["period"])
    date = str(parsed["date"])
    offset = int(parsed["offset"])
    max_chars = int(parsed["max_chars"])
    seen_period = public.get("period")
    if isinstance(seen_period, str) and seen_period != period:
        return _fail(public, period, date, "period_mismatch", "The prepared batch period does not match the request.")
    if not _usable(public):
        code = _failure_code(public)
        message = (
            "The prepared batch exceeds the read bound."
            if code == "source_oversize"
            else "The prepared batch is not a complete usable body."
            if code == "body_invalid"
            else "The prepared batch is not ready to read."
        )
        return _fail(public, period, date, code, message)
    emails = public.get("emails")
    docs = public.get("tmtb_docs")
    pending = public.get("pending_snippets")
    if not isinstance(emails, list) or not isinstance(docs, list) or not isinstance(pending, list):
        return _fail(public, period, date, "body_invalid", "The prepared batch is not a complete usable body.")
    authoritative = _iso_date(public.get("authoritative_date"))
    if authoritative is None:
        return _fail(public, period, date, "body_invalid", "The prepared batch is not a complete usable body.")
    if authoritative != date:
        return _fail(public, period, date, "date_mismatch", "The prepared batch date does not match the request.")
    batch_id = f"{period}/{date}"
    if public.get("batch_id") != batch_id:
        return _fail(public, period, date, "body_invalid", "The prepared batch is not a complete usable body.")
    if _counts_differ(public, emails, docs, pending):
        return _fail(public, period, date, "body_invalid", "The prepared batch is not a complete usable body.")
    try:
        full = _canonical(emails, docs, pending)
    except (TypeError, ValueError):
        return _fail(public, period, date, "body_invalid", "The prepared batch is not a complete usable body.")
    total = len(full)
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    shell = _metadata(public, period=period, date=date)
    shell.update(
        {
            "ok": True,
            "period": period,
            "date": date,
            "authoritative_date": authoritative,
            "batch_id": batch_id,
            "offset": offset,
            "max_chars": max_chars,
            "total_chars": total,
            "content_sha256": digest,
            "emails_count": len(emails),
            "tmtb_count": len(docs),
            "pending_snippets_count": len(pending),
            "usable": True,
            "truncated": False,
            "degraded": _degraded(public),
        }
    )
    if offset > total:
        denied = dict(shell)
        denied["ok"] = False
        denied["code"] = "offset_past_end"
        denied["error"] = "offset is past the end of the serialized batch."
        return denied
    return _fit_page(shell, full, offset, max_chars, total)


def _usable(public: Mapping[str, Any]) -> bool:
    return (
        public.get("ok") is True
        and public.get("usable") is True
        and public.get("truncated") is not True
        and public.get("preparation_status") in _READY
    )


def _failure_code(public: Mapping[str, Any]) -> str:
    status = public.get("preparation_status")
    reason = public.get("reason")
    if public.get("truncated") is True or status == "content_exceeds_bound":
        return "source_oversize"
    if status == "snapshot_hash_mismatch" or reason in {
        "snapshot_hash_mismatch",
        "snapshot_unreadable",
    }:
        return "body_invalid"
    return "not_ready"


def _counts_differ(
    public: Mapping[str, Any],
    emails: list[Any],
    docs: list[Any],
    pending: list[Any],
) -> bool:
    stored_emails = public.get("emails_count")
    if isinstance(stored_emails, int) and not isinstance(stored_emails, bool):
        if stored_emails != len(emails):
            return True
    tmtb = public.get("tmtb")
    if not isinstance(tmtb, Mapping):
        return False
    stored_docs = tmtb.get("count")
    if isinstance(stored_docs, int) and not isinstance(stored_docs, bool) and stored_docs != len(docs):
        return True
    stored_pending = tmtb.get("pending_snippets_count")
    return (
        isinstance(stored_pending, int)
        and not isinstance(stored_pending, bool)
        and stored_pending != len(pending)
    )


def _canonical(emails: list[Any], docs: list[Any], pending: list[Any]) -> str:
    return json.dumps(
        {"emails": emails, "pending_snippets": pending, "tmtb_docs": docs},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fit_page(
    shell: dict[str, Any],
    full: str,
    offset: int,
    max_chars: int,
    total: int,
) -> dict[str, Any]:
    window = min(max_chars, total - offset)
    lo = 0
    hi = window
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(_encode(_slice_page(shell, full, offset, mid, total))) <= MAX_RESPONSE_BYTES:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if window > 0 and best == 0:
        return _response_bound(shell)
    page = _slice_page(shell, full, offset, best, total)
    if len(_encode(page)) > MAX_RESPONSE_BYTES:
        return _response_bound(shell)
    return page


def _slice_page(
    shell: Mapping[str, Any],
    full: str,
    offset: int,
    count: int,
    total: int,
) -> dict[str, Any]:
    end = offset + count
    page = dict(shell)
    page["text"] = full[offset:end]
    page["next_offset"] = None if end >= total else end
    return page


def _response_bound(shell: Mapping[str, Any]) -> dict[str, Any]:
    denied = _error(
        "response_exceeds_bound",
        "The page does not fit in the response bound.",
        **{key: value for key, value in shell.items() if key != "ok"},
    )
    if len(_encode(denied)) <= MAX_RESPONSE_BYTES:
        return denied
    return _error("response_exceeds_bound", "The page does not fit in the response bound.")


def _fail(
    public: Mapping[str, Any],
    period: str,
    date: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return _error(code, message, **_metadata(public, period=period, date=date))


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    payload.update(extra)
    for key in _DOCUMENT_KEYS:
        payload.pop(key, None)
    return payload


def _metadata(public: Mapping[str, Any], *, period: str, date: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"period": period, "date": date, "degraded": _degraded(public)}
    status = _short(public.get("preparation_status"))
    if status is not None:
        meta["preparation_status"] = status
    reason = _short(public.get("reason"))
    if reason is not None:
        meta["reason"] = reason
    authoritative = _iso_date(public.get("authoritative_date"))
    if authoritative is not None:
        meta["authoritative_date"] = authoritative
    started = _iso_date(public.get("started_hkt_date"))
    if started is not None:
        meta["started_hkt_date"] = started
    batch = public.get("batch_id")
    if isinstance(batch, str) and not _pathlike(batch) and len(batch) <= 80:
        meta["batch_id"] = batch
    outcome = _short(public.get("digest_outcome"))
    if outcome is not None:
        meta["digest_outcome"] = outcome
    emails_count = _number(public.get("emails_count"))
    if emails_count is not None:
        meta["emails_count"] = emails_count
    tmtb = _copy_fields(public.get("tmtb"), _TMTB_FIELDS)
    if tmtb is not None:
        meta["tmtb"] = tmtb
        if "count" in tmtb:
            meta["tmtb_count"] = tmtb["count"]
        if "pending_snippets_count" in tmtb:
            meta["pending_snippets_count"] = tmtb["pending_snippets_count"]
    funda = _copy_fields(public.get("funda"), _FUNDA_FIELDS)
    if funda is not None:
        meta["funda"] = funda
    images = _copy_images(public.get("image_backlog"))
    if images is not None:
        meta["image_backlog"] = images
    senders = _copy_senders(public.get("priority_senders"))
    if senders is not None:
        meta["priority_senders"] = senders
    hashes = _copy_hashes(public.get("hashes"))
    if hashes is not None:
        meta["hashes"] = hashes
    usable = public.get("usable")
    if isinstance(usable, bool):
        meta["usable"] = usable
    meta["truncated"] = public.get("truncated") is True
    if status == "content_exceeds_bound":
        size = _number(public.get("bytes"))
        if size is not None:
            meta["bytes"] = size
    return meta


def _degraded(public: Mapping[str, Any]) -> bool:
    if public.get("preparation_status") == "ready_degraded":
        return True
    tmtb = public.get("tmtb")
    return isinstance(tmtb, Mapping) and tmtb.get("degraded") is True


def _copy_fields(
    raw: object,
    fields: tuple[tuple[str, str], ...],
) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    copied: dict[str, Any] = {}
    for key, kind in fields:
        if key not in raw:
            continue
        value = raw.get(key)
        cleaned: Any
        if kind == "str":
            cleaned = _short(value)
        elif kind == "int":
            cleaned = _number(value)
        else:
            cleaned = value if isinstance(value, bool) else None
        if cleaned is not None:
            copied[key] = cleaned
    return copied or None


def _copy_images(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    copied: dict[str, Any] = {}
    status = _short(raw.get("status"))
    if status is not None:
        copied["status"] = status
    sources = raw.get("sources")
    kept: dict[str, Any] = {}
    if isinstance(sources, Mapping):
        for name in ("funda-ai", "sales-notes"):
            item = _copy_fields(sources.get(name), _IMAGE_FIELDS)
            if item is not None:
                kept[name] = item
    if kept:
        copied["sources"] = kept
    return copied or None


def _copy_senders(raw: object) -> dict[str, bool] | None:
    if not isinstance(raw, Mapping):
        return None
    copied: dict[str, bool] = {}
    for key, value in raw.items():
        if (
            isinstance(key, str)
            and isinstance(value, bool)
            and len(key) <= 80
            and not _pathlike(key)
        ):
            copied[key] = value
        if len(copied) >= 20:
            break
    return copied or None


def _copy_hashes(raw: object) -> dict[str, str | None] | None:
    if not isinstance(raw, Mapping):
        return None
    copied: dict[str, str | None] = {}
    for key in _HASH_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if value is None:
            copied[key] = None
        elif isinstance(value, str) and len(value) == 64 and all(ch in _HEX for ch in value):
            copied[key] = value
    return copied or None


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 10:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != text:
        return None
    return text


def _short(value: object, limit: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or _pathlike(text):
        return None
    return text


def _number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _pathlike(value: str) -> bool:
    folded = value.lower()
    if value.startswith(("/", "~", "\\")) or "\\" in value or "file:" in folded:
        return True
    return any(
        needle in folded
        for needle in ("/users/", "/home/", "/var/", "/tmp/", "/private/", "/opt/")
    )


def _encode(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


__all__ = [
    "DEFAULT_MAX_CHARS",
    "MAX_CHARS",
    "MAX_RESPONSE_BYTES",
    "SOURCE_READ_MAX_BYTES",
    "read_market_digest_page",
]
