#!/usr/bin/env python3
"""13F Holdings Analyzer — deterministic formatting, auto-classification, biotech filter."""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from edgar import Company, set_identity

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 2000)
pd.set_option("display.max_colwidth", None)

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_SUBDIR = Path("state") / "dalton-13f-tracker"
DEFAULT_EDGAR_IDENTITY = "DaltonResearch research@everflowinv.com"

_CLASSIFICATION_BUCKETS = ("china_book", "ai_semi_saas", "other_us_book")
_DICT_DATA_FILES = (
    "merge_rules.json",
    "institution_map.json",
    "name_to_ticker.json",
    "ticker_aliases.json",
)

# Runtime workspace state (re-initialized via _init_state)
_WORKSPACE_DIR: Path | None = None
_STATE_DIR: Path | None = None
_WRITES_ENABLED: bool = False

_BUILTIN_CLASSIFICATION: dict[str, Any] = {}
_BUILTIN_DICTS: dict[str, dict[str, Any]] = {}
_BOOT_CLASSIFICATION_MAP: dict[str, str] = {}
_BOOT_DICTS: dict[str, dict[str, Any]] = {}
_BOOT_PATCHES: dict[str, dict[str, Any]] = {}

_CLASSIFICATION: dict[str, Any] = {}
_MERGE_RULES: dict[str, Any] = {}
_INSTITUTION_MAP: dict[str, Any] = {}
_NAME_TO_TICKER: dict[str, Any] = {}
_TICKER_ALIASES: dict[str, Any] = {}

_CHINA_TICKERS: set[str] = set()
_AI_SEMI_TICKERS: set[str] = set()
_OTHER_US_TICKERS: set[str] = set()


# ---------------------------------------------------------------------------
# Identity (SEC_USER_AGENT -> EDGAR_IDENTITY -> built-in default)
# ---------------------------------------------------------------------------
def resolve_sec_identity() -> tuple[str, str]:
    """Resolve SEC EDGAR identity in order: SEC_USER_AGENT -> EDGAR_IDENTITY -> default."""
    sec_ua = os.getenv("SEC_USER_AGENT", "").strip()
    if sec_ua:
        return sec_ua, "SEC_USER_AGENT"
    edgar_id = os.getenv("EDGAR_IDENTITY", "").strip()
    if edgar_id:
        return edgar_id, "EDGAR_IDENTITY"
    return DEFAULT_EDGAR_IDENTITY, "default"


def configure_identity(*, warn_default: bool = False) -> tuple[str, str]:
    """Configure edgartools User-Agent identity from environment or built-in default."""
    identity, source = resolve_sec_identity()
    if warn_default and source == "default":
        print(
            "WARNING: SEC_USER_AGENT / EDGAR_IDENTITY not set; using default identity.",
            file=sys.stderr,
        )
    set_identity(identity)
    return identity, source


# ---------------------------------------------------------------------------
# Built-in read-only data + workspace overlay
# ---------------------------------------------------------------------------
def _read_json_file(path: Path) -> dict[str, Any]:
    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _merge_classification(builtin: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge built-in classification with workspace overlay (workspace wins)."""
    sets: dict[str, set[str]] = {
        b: {str(t).upper().strip() for t in builtin.get(b, []) if str(t).strip()}
        for b in _CLASSIFICATION_BUCKETS
    }
    for bucket in _CLASSIFICATION_BUCKETS:
        for raw_t in overlay.get(bucket, []) or []:
            t = str(raw_t).upper().strip()
            if not t:
                continue
            for b in _CLASSIFICATION_BUCKETS:
                sets[b].discard(t)
            sets[bucket].add(t)

    merged: dict[str, Any] = {}
    if "_comment" in builtin:
        merged["_comment"] = builtin["_comment"]
    for bucket in _CLASSIFICATION_BUCKETS:
        merged[bucket] = sorted(sets[bucket])
    return merged


def _is_within_workspace(resolved_path: Path, resolved_workspace: Path) -> bool:
    return resolved_path == resolved_workspace or resolved_path.is_relative_to(resolved_workspace)


def _is_safe_write_target(target: Path) -> bool:
    """Verify state dir and target path both resolve inside _WORKSPACE_DIR."""
    if _WORKSPACE_DIR is None or _STATE_DIR is None:
        return False
    if not _is_within_workspace(_STATE_DIR.resolve(), _WORKSPACE_DIR):
        return False
    return _is_within_workspace(target.resolve(), _WORKSPACE_DIR)


@contextmanager
def _state_lock():
    """Acquire an exclusive advisory lock under _STATE_DIR before reading/writing overlays."""
    if _STATE_DIR is None or _WORKSPACE_DIR is None:
        raise RuntimeError("State directory not initialized")
    if not _is_within_workspace(_STATE_DIR.resolve(), _WORKSPACE_DIR):
        raise RuntimeError(f"State dir resolves outside workspace: {_STATE_DIR}")
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _STATE_DIR / ".lock"
    if not _is_safe_write_target(lock_path):
        raise RuntimeError(f"Lock file resolves outside workspace: {lock_path}")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _init_state(workspace_dir: str | Path | None = None) -> None:
    """Initialize built-in read-only mappings and optional workspace overlay."""
    global _WORKSPACE_DIR, _STATE_DIR, _WRITES_ENABLED
    global _BUILTIN_CLASSIFICATION, _BUILTIN_DICTS
    global _BOOT_CLASSIFICATION_MAP, _BOOT_DICTS, _BOOT_PATCHES
    global _CLASSIFICATION, _MERGE_RULES, _INSTITUTION_MAP, _NAME_TO_TICKER, _TICKER_ALIASES

    _WORKSPACE_DIR = Path(workspace_dir).resolve() if workspace_dir else None
    if _WORKSPACE_DIR is not None:
        raw_state_dir = _WORKSPACE_DIR / STATE_SUBDIR
        resolved_state_dir = raw_state_dir.resolve()
        if _is_within_workspace(resolved_state_dir, _WORKSPACE_DIR):
            _STATE_DIR = resolved_state_dir
            _WRITES_ENABLED = True
        else:
            print(
                f"WARNING: State directory '{raw_state_dir}' resolves outside workspace "
                f"'{_WORKSPACE_DIR}' ('{resolved_state_dir}'); writes disabled.",
                file=sys.stderr,
            )
            _STATE_DIR = None
            _WRITES_ENABLED = False
    else:
        _STATE_DIR = None
        _WRITES_ENABLED = False

    _BUILTIN_CLASSIFICATION = _read_json_file(SCRIPT_DIR / "classification.json")
    _BUILTIN_DICTS = {name: _read_json_file(SCRIPT_DIR / name) for name in _DICT_DATA_FILES}

    overlay_cls = (
        _read_json_file(_STATE_DIR / "classification.json")
        if _STATE_DIR is not None
        else {}
    )
    _CLASSIFICATION = _merge_classification(_BUILTIN_CLASSIFICATION, overlay_cls)
    _BOOT_CLASSIFICATION_MAP = {
        str(t).upper().strip(): bucket
        for bucket in _CLASSIFICATION_BUCKETS
        for t in _CLASSIFICATION.get(bucket, [])
        if str(t).strip()
    }

    merged_dicts: dict[str, dict[str, Any]] = {}
    _BOOT_DICTS = {}
    for name in _DICT_DATA_FILES:
        base = dict(_BUILTIN_DICTS[name])
        if _STATE_DIR is not None:
            overlay = _read_json_file(_STATE_DIR / name)
            base.update(overlay)
        merged_dicts[name] = base
        _BOOT_DICTS[name] = copy.deepcopy(base)

    _BOOT_PATCHES = {}
    _MERGE_RULES = merged_dicts["merge_rules.json"]
    _INSTITUTION_MAP = merged_dicts["institution_map.json"]
    _NAME_TO_TICKER = merged_dicts["name_to_ticker.json"]
    _TICKER_ALIASES = merged_dicts["ticker_aliases.json"]

    _CHINA_TICKERS.clear()
    _CHINA_TICKERS.update(_CLASSIFICATION.get("china_book", []))
    _AI_SEMI_TICKERS.clear()
    _AI_SEMI_TICKERS.update(_CLASSIFICATION.get("ai_semi_saas", []))
    _OTHER_US_TICKERS.clear()
    _OTHER_US_TICKERS.update(_CLASSIFICATION.get("other_us_book", []))


def _save_json(name: str, payload: dict[str, Any]) -> None:
    """Persist process delta into <workspace>/state/dalton-13f-tracker/<name> under lock."""
    if not _WRITES_ENABLED or _STATE_DIR is None:
        return

    target = _STATE_DIR / name
    if not _is_safe_write_target(target):
        print(
            f"WARNING: Refusing to write '{target}' outside workspace '{_WORKSPACE_DIR}'.",
            file=sys.stderr,
        )
        return

    if name == "classification.json":
        current_map = {
            str(t).upper().strip(): bucket
            for bucket in _CLASSIFICATION_BUCKETS
            for t in payload.get(bucket, [])
            if str(t).strip()
        }
        changed = {t: b for t, b in current_map.items() if _BOOT_CLASSIFICATION_MAP.get(t) != b}
        if not changed:
            return
        builtin_sets = {
            b: {str(t).upper().strip() for t in _BUILTIN_CLASSIFICATION.get(b, []) if str(t).strip()}
            for b in _CLASSIFICATION_BUCKETS
        }
        with _state_lock():
            if not _is_safe_write_target(target):
                print(
                    f"WARNING: Refusing to write '{target}' outside workspace '{_WORKSPACE_DIR}'.",
                    file=sys.stderr,
                )
                return
            disk_overlay = _read_json_file(target)
            disk_sets = {
                b: {str(t).upper().strip() for t in (disk_overlay.get(b, []) or []) if str(t).strip()}
                for b in _CLASSIFICATION_BUCKETS
            }
            for t, new_bucket in changed.items():
                for b in _CLASSIFICATION_BUCKETS:
                    disk_sets[b].discard(t)
                if t not in builtin_sets[new_bucket]:
                    disk_sets[new_bucket].add(t)
            overlay_out = {b: sorted(disk_sets[b]) for b in _CLASSIFICATION_BUCKETS}
            _atomic_write_json(target, overlay_out)
            _BOOT_CLASSIFICATION_MAP.update(changed)
        return

    if name in _BUILTIN_DICTS:
        boot_dict = _BOOT_DICTS.get(name, {})
        changed_kv = {
            k: copy.deepcopy(v)
            for k, v in payload.items()
            if k != "_comment" and boot_dict.get(k) != v
        }
        if not changed_kv:
            return
        with _state_lock():
            if not _is_safe_write_target(target):
                print(
                    f"WARNING: Refusing to write '{target}' outside workspace '{_WORKSPACE_DIR}'.",
                    file=sys.stderr,
                )
                return
            disk_overlay = _read_json_file(target)
            merged_overlay = {k: v for k, v in disk_overlay.items() if k != "_comment"}
            merged_overlay.update(changed_kv)
            _atomic_write_json(target, merged_overlay)
            _BOOT_DICTS.setdefault(name, {}).update(copy.deepcopy(changed_kv))


def _append_auto_learn_log(kind: str, key: str, value: str, source: str, note: str = "") -> None:
    if not _WRITES_ENABLED or _STATE_DIR is None:
        return
    log_path = _STATE_DIR / "auto_learn_log.jsonl"
    if not _is_safe_write_target(log_path):
        print(
            f"WARNING: Refusing to write '{log_path}' outside workspace '{_WORKSPACE_DIR}'.",
            file=sys.stderr,
        )
        return
    event = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "key": key,
        "value": value,
        "source": source,
        "note": note,
    }
    with _state_lock():
        if not _is_safe_write_target(log_path):
            print(
                f"WARNING: Refusing to write '{log_path}' outside workspace '{_WORKSPACE_DIR}'.",
                file=sys.stderr,
            )
            return
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _read_patch_store_raw(path: Path) -> dict[str, Any]:
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            return {"patches": []}
    return {"patches": []}


def _load_patch_store() -> dict[str, Any]:
    global _BOOT_PATCHES
    if _STATE_DIR is None:
        return {"patches": []}
    patch_store = _STATE_DIR / "map_patches.json"
    if not _is_safe_write_target(patch_store):
        return {"patches": []}
    data = _read_patch_store_raw(patch_store)
    _BOOT_PATCHES = {
        str(p["id"]): copy.deepcopy(p)
        for p in data.get("patches", [])
        if isinstance(p, dict) and p.get("id")
    }
    return data


def _save_patch_store(store: dict[str, Any]) -> None:
    global _BOOT_PATCHES
    if not _WRITES_ENABLED or _STATE_DIR is None:
        return
    target = _STATE_DIR / "map_patches.json"
    if not _is_safe_write_target(target):
        print(
            f"WARNING: Refusing to write '{target}' outside workspace '{_WORKSPACE_DIR}'.",
            file=sys.stderr,
        )
        return
    local_patches = [p for p in store.get("patches", []) if isinstance(p, dict) and p.get("id")]
    with _state_lock():
        if not _is_safe_write_target(target):
            print(
                f"WARNING: Refusing to write '{target}' outside workspace '{_WORKSPACE_DIR}'.",
                file=sys.stderr,
            )
            return
        disk_store = _read_patch_store_raw(target)
        merged: list[dict[str, Any]] = []
        idx_by_id: dict[str, int] = {}
        for p in disk_store.get("patches", []):
            if not isinstance(p, dict) or not p.get("id"):
                continue
            pid = str(p["id"])
            if pid not in idx_by_id:
                idx_by_id[pid] = len(merged)
                merged.append(copy.deepcopy(p))
        for p in local_patches:
            pid = str(p["id"])
            if pid not in idx_by_id:
                idx_by_id[pid] = len(merged)
                merged.append(copy.deepcopy(p))
            elif p != _BOOT_PATCHES.get(pid):
                merged[idx_by_id[pid]] = copy.deepcopy(p)
        _atomic_write_json(target, {"patches": merged})
        _BOOT_PATCHES = {str(p["id"]): copy.deepcopy(p) for p in merged}


def _canon_category(label: str) -> str:
    s = (label or "").strip().lower()
    if s in ("china", "china book", "中国", "中概"):
        return "China Book"
    if s in ("ai", "ai/semi", "ai/semi/us saas", "tech", "科技"):
        return "AI/Semi/US SaaS"
    return "Other US Book"


def _parse_map_instruction(instruction: str) -> list:
    ops = []
    text = (instruction or "").strip()
    if not text:
        return ops

    chunks = [c.strip() for c in re.split(r"[;；\n]+", text) if c.strip()]
    for ch in chunks:
        # classify move: 把 XYZ 从 A 改到 B  (ticker only, max 6 chars)
        m = re.search(r"把\s*([A-Za-z\.\-]{1,6})\s*从\s*(.+?)\s*改到\s*(.+)$", ch, re.IGNORECASE)
        if m:
            ticker = m.group(1).upper()
            to_cat = _canon_category(m.group(3))
            ops.append({"type": "classification", "ticker": ticker, "category": to_cat, "raw": ch})
            continue

        # classify set: XYZ 分类到 B / 归类到 B / 放到 B  (ticker only, max 6 chars)
        m = re.search(r"([A-Za-z\.\-]{1,6})\s*(?:分类到|归类到|放到|属于)\s*(.+)$", ch, re.IGNORECASE)
        if m:
            ticker = m.group(1).upper()
            cat = _canon_category(m.group(2))
            ops.append({"type": "classification", "ticker": ticker, "category": cat, "raw": ch})
            continue

        # institution alias: 别名 hhlr 对应 0001762304 / hhlr -> 0001762304
        m = re.search(r"(?:别名|机构)?\s*([^\s,，]+)\s*(?:对应|映射到|->|=>)\s*(\d{6,12})", ch, re.IGNORECASE)
        if m:
            alias = m.group(1).strip().lower()
            cik = m.group(2)
            ops.append({"type": "institution", "alias": alias, "cik": cik, "raw": ch})
            continue

        # merge two tickers -> canonical
        m = re.search(r"(?:merge|合并)\s*([A-Za-z\.\-]{1,12})\s*[,，/]\s*([A-Za-z\.\-]{1,12})\s*(?:->|到|为)\s*([A-Za-z][A-Za-z\s\.\-&]{1,40})", ch, re.IGNORECASE)
        if m:
            t1, t2, name = m.group(1).upper(), m.group(2).upper(), m.group(3).strip()
            ops.append({"type": "merge", "ticker": t1, "canonical": name, "raw": ch})
            ops.append({"type": "merge", "ticker": t2, "canonical": name, "raw": ch})
            continue

        # single merge: GOOG -> Alphabet
        m = re.search(r"([A-Za-z\.\-]{1,12})\s*(?:merge到|合并到|->|=>)\s*([A-Za-z][A-Za-z\s\.\-&]{1,40})", ch, re.IGNORECASE)
        if m:
            t, name = m.group(1).upper(), m.group(2).strip()
            ops.append({"type": "merge", "ticker": t, "canonical": name, "raw": ch})
            continue

        # ticker alias: alias XRS -> TAL / XRS 别名 TAL / XRS 实际是 TAL
        m = re.search(r"(?:alias|别名|实际是|就是|映射)\s*([A-Za-z\.\-]{1,6})\s*(?:->|=>|→|到|为)\s*([A-Za-z\.\-]{1,6})", ch, re.IGNORECASE)
        if not m:
            m = re.search(r"([A-Za-z\.\-]{1,6})\s*(?:别名|实际是|就是|映射到)\s*([A-Za-z\.\-]{1,6})", ch, re.IGNORECASE)
        if m:
            sec_t, cls_t = m.group(1).upper(), m.group(2).upper()
            ops.append({"type": "alias", "sec_ticker": sec_t, "classification_ticker": cls_t, "raw": ch})
            continue

    return ops


def _apply_ops(ops: list, source: str = "manual") -> list:
    applied = []
    for op in ops:
        t = op.get("type")
        if t == "classification":
            ticker = op.get("ticker", "").upper()
            category = _canon_category(op.get("category", "Other US Book"))
            _learn_classification(ticker, category, source=source)
            applied.append({"type": t, "ticker": ticker, "category": category})
        elif t == "institution":
            alias = op.get("alias", "").lower().strip()
            cik = str(op.get("cik", "")).strip()
            _learn_institution_alias(alias, cik, source=source)
            applied.append({"type": t, "alias": alias, "cik": cik})
        elif t == "merge":
            ticker = op.get("ticker", "").upper().strip()
            canonical = op.get("canonical", "").strip()
            _learn_merge_rule(ticker, canonical, source=source)
            applied.append({"type": t, "ticker": ticker, "canonical": canonical})
        elif t == "alias":
            sec_t = op.get("sec_ticker", "").upper().strip()
            cls_t = op.get("classification_ticker", "").upper().strip()
            _learn_ticker_alias(sec_t, cls_t, source=source)
            applied.append({"type": t, "sec_ticker": sec_t, "classification_ticker": cls_t})
    return applied


_BIOTECH_KEYWORDS = [
    "bio", "therapeutics", "pharma", "medicines", "life sciences",
    "genomics", "oncology", "immun", "geneg", "biopharma",
]
_BIOTECH_TICKERS = {
    "LEGN", "ONC", "MAZE", "ALGS", "CTKB", "SGMT", "AVBP", "IMAB",
    "GOSS", "CONTINEUM", "ZLAB", "BGNE", "MRNA", "BNTX",
}

_CHINA_HINTS = [
    "alibaba", "pdd", "jd", "baidu", "netease", "futu", "beke", "yatsen",
    "tuya", "uxin", "mogu", "vnet", "agora", "china", "hong kong", "hldg ltd",
]
_AI_HINTS = [
    "semiconductor", "nvidia", "microsoft", "alphabet", "meta", "amazon", "tesla",
    "software", "cloud", "analytics", "saas", "tsm", "broadcom", "adobe",
    "artificial intelligence", "machine learning",
]


def _category_key(category: str) -> str:
    if category == "China Book":
        return "china_book"
    if category == "AI/Semi/US SaaS":
        return "ai_semi_saas"
    return "other_us_book"


def _infer_category_fallback(ticker: str, issuer: str) -> str:
    t = (ticker or "").upper()
    text = f"{ticker} {issuer}".lower()
    if t in _CHINA_TICKERS:
        return "China Book"
    if t in _AI_SEMI_TICKERS:
        return "AI/Semi/US SaaS"
    if t in _OTHER_US_TICKERS:
        return "Other US Book"
    if any(h in text for h in _CHINA_HINTS):
        return "China Book"
    if any(h in text for h in _AI_HINTS):
        return "AI/Semi/US SaaS"
    return "Other US Book"


def _learn_name_to_ticker(ticker: str, issuer: str, short_name: str = ""):
    """Record name->ticker mappings from SEC data for reverse lookup."""
    t = (ticker or "").upper().strip()
    if not t or len(t) > 6 or t == "NAN":
        return
    if not _is_valid_ticker(t):
        return
    changed = False
    for name in (issuer, short_name):
        key = (name or "").upper().strip()
        if key and key != t and len(key) > 1:
            if _NAME_TO_TICKER.get(key) != t:
                _NAME_TO_TICKER[key] = t
                changed = True
    if changed:
        _save_json("name_to_ticker.json", _NAME_TO_TICKER)


def _resolve_ticker(input_str: str) -> str:
    """Resolve a company name or ticker to the canonical SEC ticker.
    Returns the resolved ticker (uppercase), or the original input if no match found."""
    s = (input_str or "").upper().strip()
    if not s:
        return s
    # Already a known ticker?
    known = _CHINA_TICKERS | _AI_SEMI_TICKERS | _OTHER_US_TICKERS
    if s in known:
        return s
    # Try reverse lookup
    if s in _NAME_TO_TICKER:
        return _NAME_TO_TICKER[s]
    # Try partial match (input is substring of a known name)
    candidates = [(k, v) for k, v in _NAME_TO_TICKER.items() if s in k]
    if len(candidates) == 1:
        return candidates[0][1]
    return s


def _learn_ticker_alias(sec_ticker: str, classification_ticker: str, source: str = "auto"):
    """Record SEC ticker -> classification ticker alias (e.g. XRS -> TAL)."""
    s = (sec_ticker or "").upper().strip()
    c = (classification_ticker or "").upper().strip()
    if not s or not c or s == c or s.startswith("_"):
        return
    if not _is_valid_ticker(s) or not _is_valid_ticker(c):
        return
    if _TICKER_ALIASES.get(s) == c:
        return
    _TICKER_ALIASES[s] = c
    _save_json("ticker_aliases.json", _TICKER_ALIASES)
    _append_auto_learn_log("ticker_alias", s, c, source, f"SEC ticker {s} → classification ticker {c}")
    print(f"INFO: Auto-learned ticker alias: {s} → {c}", file=sys.stderr)


def _is_valid_ticker(s: str) -> bool:
    """Check if string looks like a valid US ticker (1-6 uppercase letters, optional dot/dash suffix)."""
    return bool(re.match(r"^[A-Z]{1,6}(?:[.\-][A-Z]{1,2})?$", s))


def _learn_classification(ticker: str, category: str, source: str = "fallback"):
    raw = (ticker or "").upper().strip()
    if not raw:
        return
    # Auto-resolve company names to SEC tickers (e.g. "DOORDASH" -> "DASH")
    t = _resolve_ticker(raw)
    if t != raw:
        print(f"INFO: Resolved '{raw}' → '{t}' (SEC ticker)", file=sys.stderr)
    if not _is_valid_ticker(t):
        print(
            f"WARNING: Rejected classification entry '{raw}' — could not resolve to a valid ticker. "
            f"Run 'compare' first to populate the name→ticker lookup.",
            file=sys.stderr,
        )
        return
    # For manual/external sources, only accept tickers already seen in SEC data
    known = _CHINA_TICKERS | _AI_SEMI_TICKERS | _OTHER_US_TICKERS
    if source not in ("auto-fallback",) and t not in known:
        print(
            f"WARNING: Rejected classification for unknown ticker '{t}'. "
            f"Only SEC-sourced tickers are accepted. Run 'compare' first to auto-discover, "
            f"or verify the ticker symbol is correct.",
            file=sys.stderr,
        )
        return
    key = _category_key(category)
    if key not in _CLASSIFICATION:
        _CLASSIFICATION[key] = []
    if t in _CLASSIFICATION[key]:
        return
    # remove from other buckets first
    for bucket in _CLASSIFICATION_BUCKETS:
        if bucket not in _CLASSIFICATION:
            _CLASSIFICATION[bucket] = []
        if t in _CLASSIFICATION[bucket]:
            _CLASSIFICATION[bucket].remove(t)
    _CLASSIFICATION[key].append(t)
    _CLASSIFICATION[key] = sorted(set(_CLASSIFICATION[key]))
    _save_json("classification.json", _CLASSIFICATION)
    _CHINA_TICKERS.clear()
    _CHINA_TICKERS.update(_CLASSIFICATION.get("china_book", []))
    _AI_SEMI_TICKERS.clear()
    _AI_SEMI_TICKERS.update(_CLASSIFICATION.get("ai_semi_saas", []))
    _OTHER_US_TICKERS.clear()
    _OTHER_US_TICKERS.update(_CLASSIFICATION.get("other_us_book", []))
    _append_auto_learn_log("classification", t, category, source)


def _learn_institution_alias(alias: str, cik_value: str, source: str = "lookup"):
    a = (alias or "").strip().lower()
    if not a or a.isdigit() or a.startswith("_"):
        return
    if _INSTITUTION_MAP.get(a) == cik_value:
        return
    if a not in _INSTITUTION_MAP:
        _INSTITUTION_MAP[a] = cik_value
        _save_json("institution_map.json", _INSTITUTION_MAP)
        _append_auto_learn_log("institution", a, cik_value, source)


def _learn_merge_rule(ticker: str, canonical: str, source: str = "inferred"):
    t = (ticker or "").upper().strip()
    c = (canonical or "").strip()
    if not t or not c:
        return
    if _MERGE_RULES.get(t) == c:
        return
    if t not in _MERGE_RULES:
        _MERGE_RULES[t] = c
        _save_json("merge_rules.json", _MERGE_RULES)
        _append_auto_learn_log("merge", t, c, source)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _emit(payload: dict, as_json: bool = False):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))


def _error_hint(msg: str) -> str:
    m = (msg or "").lower()
    if "sec_user_agent" in m or "edgar_identity" in m or "user-agent" in m:
        return "Set SEC_USER_AGENT or EDGAR_IDENTITY: export SEC_USER_AGENT='Name <email>'"
    if "not enough" in m:
        return "Reduce --offset or verify the institution has enough 13F history."
    if "ssl" in m or "tls" in m or "eof" in m:
        return "Network/TLS issue. Check proxy and access to data.sec.gov."
    if "finding institution" in m or "not found" in m:
        return "Use a valid ticker, CIK, or institution name from institution_map.json."
    if "rate" in m or "429" in m:
        return "SEC rate limit. Wait 10s and retry."
    return "Check parameters and network; then retry."


def _retry(fn, max_retries=2, delay=2.0):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_transient = any(
                k in err_str
                for k in [
                    "timeout", "timed out", "connection", "reset", "429",
                    "rate", "ssl", "eof", "too many",
                ]
            )
            if not is_transient or attempt >= max_retries:
                raise
            time.sleep(delay * (attempt + 1))
    raise last_err


# ---------------------------------------------------------------------------
# Institution resolution (#7)
# ---------------------------------------------------------------------------
def _resolve_institution(identifier: str, auto_learn: bool = True):
    """Resolve institution from name/CIK/ticker."""
    raw_identifier = (identifier or "").strip()
    mapped = _INSTITUTION_MAP.get(raw_identifier.lower())
    if mapped:
        identifier = mapped

    if str(identifier).startswith("_"):
        raise RuntimeError(f"Invalid institution: {identifier}")

    try:
        if str(identifier).isdigit() or (str(identifier).startswith("0") and str(identifier).replace("0", "").isdigit()):
            company = Company(int(identifier))
        else:
            company = Company(identifier)

        # auto-learn alias -> CIK mapping
        if auto_learn:
            cik = str(getattr(company, "cik", "") or "")
            if cik and raw_identifier and raw_identifier.lower() != cik:
                _learn_institution_alias(raw_identifier, cik, source="auto")

        return company
    except Exception as e:
        raise RuntimeError(f"Error finding institution '{identifier}': {e}") from e


# ---------------------------------------------------------------------------
# Quarter parsing (#6)
# ---------------------------------------------------------------------------
def _quarter_to_offset(quarter_str: str, company) -> int:
    """Convert '25Q4' or '2025Q4' to offset from latest filing."""
    m = re.match(r"(\d{2,4})[Qq](\d)", quarter_str)
    if not m:
        raise ValueError(f"Invalid quarter format: '{quarter_str}'. Use e.g. '25Q4' or '2025Q3'.")

    year = int(m.group(1))
    if year < 100:
        year += 2000
    q = int(m.group(2))

    # Quarter end months: Q1=Mar, Q2=Jun, Q3=Sep, Q4=Dec
    q_end_month = {1: 3, 2: 6, 3: 9, 4: 12}
    if q not in q_end_month:
        raise ValueError(f"Invalid quarter number: Q{q}")

    target_period = f"{year}-{q_end_month[q]:02d}"

    # Scan filings to find offset
    filings = _retry(lambda: company.get_filings(form="13F-HR"))
    for i in range(min(len(filings), 20)):
        try:
            obj = filings[i].obj()
            period = str(obj.report_period)
            if period.startswith(target_period):
                return i
        except Exception:
            continue

    raise RuntimeError(f"Could not find 13F filing for {quarter_str} (target period: {target_period}-*)")


def _quarter_label(period_str: str) -> str:
    """Convert '2025-09-30' or '2025-09' to '25Q3'."""
    m = re.match(r"(\d{4})-(\d{2})", str(period_str))
    if not m:
        return ""
    year = int(m.group(1)) % 100
    month = int(m.group(2))
    q_map = {3: 1, 6: 2, 9: 3, 12: 4}
    q = q_map.get(month, 0)
    if q == 0:
        return ""
    return f"{year}Q{q}"


# ---------------------------------------------------------------------------
# Biotech filter (#2)
# ---------------------------------------------------------------------------
def _is_biotech(ticker: str, issuer: str) -> bool:
    if ticker and ticker.upper() in _BIOTECH_TICKERS:
        return True
    issuer_lower = (issuer or "").lower()
    return any(kw in issuer_lower for kw in _BIOTECH_KEYWORDS)


# ---------------------------------------------------------------------------
# Classification (#3)
# ---------------------------------------------------------------------------
def _classify(ticker: str, issuer: str = "", auto_learn: bool = True) -> str:
    t = (ticker or "").upper()

    # 1) Resolve ticker alias from ticker_aliases.json (SEC ticker -> common ticker)
    resolved = _TICKER_ALIASES.get(t, t)

    # 2) Check merge rules first — find any sibling in a classification bucket
    canonical = _MERGE_RULES.get(resolved, _MERGE_RULES.get(t, ""))
    if canonical:
        for k, v in _MERGE_RULES.items():
            if v == canonical and k.upper() in _CHINA_TICKERS:
                return "China Book"
            if v == canonical and k.upper() in _AI_SEMI_TICKERS:
                return "AI/Semi/US SaaS"

    # 3) Direct lookup — check resolved alias first, then original SEC ticker
    all_classified = _CHINA_TICKERS | _AI_SEMI_TICKERS | _OTHER_US_TICKERS
    for check_t in (resolved, t):
        if check_t in _CHINA_TICKERS:
            return "China Book"
        if check_t in _AI_SEMI_TICKERS:
            return "AI/Semi/US SaaS"
        if check_t in _OTHER_US_TICKERS:
            return "Other US Book"

    # 4) Auto-detect alias: if SEC ticker not classified, but issuer name maps to a
    #    known classified ticker via name_to_ticker.json, auto-learn the alias
    if auto_learn and t and t not in all_classified:
        issuer_upper = (issuer or "").upper().strip()
        candidate = _NAME_TO_TICKER.get(issuer_upper, "")
        if candidate and candidate != t and candidate in all_classified:
            _learn_ticker_alias(t, candidate, source="auto-issuer-match")
            if candidate in _CHINA_TICKERS:
                return "China Book"
            if candidate in _AI_SEMI_TICKERS:
                return "AI/Semi/US SaaS"
            if candidate in _OTHER_US_TICKERS:
                return "Other US Book"

    # 5) Fallback classifier + auto-learn
    category = _infer_category_fallback(t, issuer)
    if auto_learn and t:
        _learn_classification(t, category, source="auto-fallback")
    return category


# ---------------------------------------------------------------------------
# Short name (#4 Step 4)
# ---------------------------------------------------------------------------
_SHORT_NAMES = {
    "AMAZON COM INC": "Amazon", "ALPHABET INC": "Alphabet",
    "META PLATFORMS INC": "Meta", "MICROSOFT CORP": "Microsoft",
    "APPLE INC": "Apple", "NVIDIA CORP": "NVIDIA",
    "TAIWAN SEMICONDUCTOR MFG CO LTD": "TSM",
    "BROADCOM INC": "Broadcom", "TESLA INC": "Tesla",
    "ALIBABA GROUP HOLDING LTD": "Alibaba", "ALIBABA GROUP HLDG LTD": "Alibaba",
    "PINDUODUO INC": "PDD", "PDD HOLDINGS INC": "PDD",
    "JD COM INC": "JD", "JD.COM INC": "JD",
    "BAIDU INC": "Baidu", "NETEASE INC": "NetEase",
    "FUTU HOLDINGS LTD": "FUTU", "FUTU HLDGS LTD": "FUTU",
    "KE HOLDINGS INC": "BEKE", "KE HLDGS INC": "BEKE",
    "FULL TRUCK ALLIANCE CO LTD": "YMM",
    "ISHARES BITCOIN TRUST ETF": "IBIT",
}


def _short_name(issuer: str, ticker: str) -> str:
    """Get short display name."""
    # Check canonical merge name
    canonical = _MERGE_RULES.get((ticker or "").upper())
    if canonical and not canonical.startswith("_"):
        return canonical

    # Check short name map
    issuer_upper = (issuer or "").upper().strip()
    if issuer_upper in _SHORT_NAMES:
        return _SHORT_NAMES[issuer_upper]

    # Fallback: strip common corporate suffixes and noise words
    raw = (issuer or "").strip()
    if raw:
        # Normalize dots and spacing
        cleaned = re.sub(r"[\.,]", " ", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        tokens = cleaned.split(" ")

        # Remove trailing legal/company suffixes repeatedly
        strip_set = {
            "inc", "corp", "corporation", "ltd", "limited", "plc", "co", "company", "llc",
            "group", "holding", "holdings", "hldg", "hldgs", "sa", "nv", "ag",
        }
        while tokens and tokens[-1].lower() in strip_set:
            tokens.pop()

        # Keep first 2 tokens after cleanup for concise name
        if tokens:
            short_tokens = tokens[:2]
            name = " ".join(short_tokens).title()
            return name

    return ticker or issuer or "Unknown"


# ---------------------------------------------------------------------------
# Value formatting (#1 Step 3)
# ---------------------------------------------------------------------------
def _format_value(value: float) -> str:
    """Format value in Chinese convention. edgartools returns Value in actual USD."""
    actual = value
    if actual >= 100_000_000:
        yi = actual / 100_000_000
        return f"{yi:.1f}亿美元"
    else:
        wan = round(actual / 10_000)
        # Round to nearest hundred wan
        wan = round(wan / 100) * 100
        # Avoid 0 for non-zero positions
        if actual > 0 and wan == 0:
            wan = 100
        return f"{wan}万美元"


# ---------------------------------------------------------------------------
# Change template (#1 Step 4)
# ---------------------------------------------------------------------------
def _change_template(name: str, value: float, prev_shares: int, chg_shares: int,
                     prev_value: float) -> str:
    val_str = _format_value(value)

    if prev_shares == 0 and chg_shares > 0:
        return f"建仓{name}，{val_str}"

    if chg_shares == 0 and prev_shares > 0:
        return f"{name}仓位不变，{val_str}"

    if prev_shares > 0 and (prev_shares + chg_shares) == 0:
        prev_val_str = _format_value(prev_value)
        return f"清仓{name}（之前持仓{prev_val_str}）"

    if prev_shares > 0:
        pct = round(abs(chg_shares) / prev_shares * 100)
        if pct <= 2:
            return f"{name}仓位几乎不变，{val_str}"
        if chg_shares > 0:
            if pct > 50:
                prev_val_str = _format_value(prev_value)
                return f"大幅加仓{name}至{val_str}，之前持仓{prev_val_str}"
            else:
                return f"加仓{name} {pct}%至{val_str}"
        else:
            return f"减仓{name} {pct}%至{val_str}"

    return f"{name}仓位不变，{val_str}"


def _detect_action(prev_shares: int, chg_shares: int) -> str:
    """Classify a position change into an action label."""
    shares = prev_shares + chg_shares
    if prev_shares == 0 and chg_shares > 0:
        return "建仓"
    if prev_shares > 0 and shares == 0:
        return "清仓"
    if chg_shares == 0 and prev_shares > 0:
        return "不变"
    if prev_shares > 0:
        pct = round(abs(chg_shares) / prev_shares * 100)
        if pct <= 2:
            return "几乎不变"
        if chg_shares > 0:
            return "大幅加仓" if pct > 50 else "加仓"
        else:
            return "减仓"
    return "不变"


def _extract_actions(comparison_rows: list, exclude_biotech: bool = True) -> dict:
    """Extract per-company action from comparison rows. Returns {merge_key: action_str}."""
    merged = {}
    for row in comparison_rows:
        ticker = str(row.get("Ticker") or row.get("ticker") or "").upper().strip()
        issuer = str(row.get("Issuer") or row.get("issuer") or "")
        if exclude_biotech and _is_biotech(ticker, issuer):
            continue
        # CB detection (must mirror _build_report logic)
        cusip = str(row.get("Cusip") or row.get("cusip") or "").strip()
        instrument = "equity"
        if len(cusip) >= 8:
            issue_chars = cusip[6:8]
            if any(c.isalpha() for c in issue_chars):
                instrument = "cb"
        canonical = _MERGE_RULES.get(ticker)
        merge_key = canonical or _short_name(issuer, ticker)
        if instrument == "cb":
            merge_key = merge_key + " CB"
        if merge_key not in merged:
            merged[merge_key] = {"shares": 0, "prev_shares": 0, "chg_shares": 0}
        entry = merged[merge_key]
        shares = _safe_int(row.get("SharesPrnAmount") or row.get("Shares") or row.get("shares") or 0)
        chg = _safe_int(row.get("ShareChange") or row.get("Chg") or row.get("chg") or row.get("Change") or 0)
        entry["shares"] += shares
        entry["chg_shares"] += chg
        status = str(row.get("Status") or "").upper().strip()
        if status == "NEW":
            pass
        else:
            prev_explicit = _safe_int(row.get("PrevShares") or row.get("Prev Shares") or row.get("prev_shares") or 0)
            if prev_explicit > 0:
                entry["prev_shares"] += prev_explicit
            elif shares > 0 or chg != 0:
                entry["prev_shares"] += max(0, shares - chg)

    result = {}
    for key, e in merged.items():
        e["chg_shares"] = e["shares"] - e["prev_shares"]
        result[key] = _detect_action(e["prev_shares"], e["chg_shares"])
    return result


# ---------------------------------------------------------------------------
# Core: build formatted report (#1)
# ---------------------------------------------------------------------------
def _build_report(comparison_rows: list, exclude_biotech: bool = True, auto_learn: bool = True,
                  prior_actions=None, prev_quarter_label: str = "") -> dict:
    """Process comparison rows into categorized, formatted report."""

    # edgartools >= 5.62 normalizes Value / PrevValue units to USD via value_unit_resolution.

    # Auto-learn merge rules from class share names (safe heuristic)
    if auto_learn:
        issuer_groups = {}
        for row in comparison_rows:
            ticker = str(row.get("Ticker") or row.get("ticker") or "").upper().strip()
            issuer = str(row.get("Issuer") or row.get("issuer") or "")
            if not ticker or not issuer:
                continue
            if "class" in issuer.lower():
                base = re.sub(r"\bclass\s+[a-z0-9]+\b", "", issuer, flags=re.IGNORECASE)
                base = re.sub(r"\s+", " ", base).strip()
                issuer_groups.setdefault(base, set()).add(ticker)

        for base, tickers in issuer_groups.items():
            if len(tickers) >= 2:
                canonical = _short_name(base, "")
                for t in tickers:
                    _learn_merge_rule(t, canonical, source="auto-class-share")

    # Step 1: Filter biotech + merge share classes
    merged = {}
    for row in comparison_rows:
        ticker = str(row.get("Ticker") or row.get("ticker") or "").upper().strip()
        issuer = str(row.get("Issuer") or row.get("issuer") or "")

        if exclude_biotech and _is_biotech(ticker, issuer):
            continue

        # Detect instrument type from CUSIP (issue number = chars 7-8)
        cusip = str(row.get("Cusip") or row.get("cusip") or "").strip()
        instrument = "equity"
        if len(cusip) >= 8:
            issue_chars = cusip[6:8]
            if any(c.isalpha() for c in issue_chars):
                instrument = "cb"  # convertible bond / note

        # Determine merge key — keep CB separate from equity
        canonical = _MERGE_RULES.get(ticker)
        short = _short_name(issuer, ticker)
        merge_key = canonical or short
        if instrument == "cb":
            merge_key = merge_key + " CB"

        # Learn name->ticker reverse mapping from SEC data
        if ticker and auto_learn:
            _learn_name_to_ticker(ticker, issuer, short)

        if merge_key not in merged:
            merged[merge_key] = {
                "name": merge_key,
                "ticker": ticker,
                "issuer": issuer,
                "shares": 0, "value": 0,
                "prev_shares": 0, "prev_value": 0,
                "chg_shares": 0,
                "category": _classify(ticker, issuer, auto_learn=auto_learn),
            }

        entry = merged[merge_key]
        # Accumulate
        shares = _safe_int(row.get("SharesPrnAmount") or row.get("Shares") or row.get("shares") or 0)
        value = _safe_float(row.get("Value") or row.get("value") or 0)
        chg = _safe_int(row.get("ShareChange") or row.get("Chg") or row.get("chg") or row.get("Change") or 0)

        entry["shares"] += shares
        entry["value"] += value
        entry["chg_shares"] += chg

        # Calculate prev shares (prefer explicit PrevShares)
        status = str(row.get("Status") or "").upper().strip()
        if status == "NEW":
            pass
        else:
            prev_shares_explicit = _safe_int(row.get("PrevShares") or row.get("Prev Shares") or row.get("prev_shares") or 0)
            if prev_shares_explicit > 0:
                entry["prev_shares"] += prev_shares_explicit
            elif shares > 0 or chg != 0:
                entry["prev_shares"] += max(0, shares - chg)

        # Try to get prev value
        prev_val = _safe_float(row.get("PrevValue") or row.get("Prev Value") or row.get("prev_value") or 0)
        if prev_val > 0:
            entry["prev_value"] += prev_val
        elif entry["prev_shares"] > 0 and entry["shares"] > 0:
            ratio = entry["prev_shares"] / max(entry["shares"], 1)
            entry["prev_value"] = entry["value"] * ratio

    # Step 2-5: Categorize, format, sort
    categories = {"China Book": [], "AI/Semi/US SaaS": [], "Other US Book": []}

    for entry in merged.values():
        entry["chg_shares"] = entry["shares"] - entry["prev_shares"]
        cat = entry["category"]
        if cat not in categories:
            cat = "Other US Book"

        is_closed = entry["shares"] == 0 and entry["prev_shares"] > 0
        text = _change_template(
            entry["name"], entry["value"],
            entry["prev_shares"], entry["chg_shares"],
            entry["prev_value"],
        )

        # Prepend prior-quarter trend context if available
        if prior_actions and prev_quarter_label:
            cur_action = _detect_action(entry["prev_shares"], entry["chg_shares"])
            prior_act = prior_actions.get(entry["name"], "")
            if cur_action in ("加仓", "大幅加仓") and prior_act in ("建仓", "加仓", "大幅加仓"):
                prior_label = "建仓" if prior_act == "建仓" else "加仓"
                text = f"继{prev_quarter_label}{prior_label}后，继续" + text
            elif cur_action == "减仓" and prior_act == "减仓":
                text = f"继{prev_quarter_label}减仓后，继续" + text

        categories[cat].append({
            "text": text,
            "value": entry["value"],
            "prev_value": entry["prev_value"],
            "is_closed": is_closed,
            "name": entry["name"],
            "ticker": entry["ticker"],
            "shares": entry["shares"],
            "prev_shares": entry["prev_shares"],
            "chg_shares": entry["chg_shares"],
        })

    # Sort: active positions by value desc, closed by prev_value desc at bottom
    report_lines = []
    for cat_name in ["China Book", "AI/Semi/US SaaS", "Other US Book"]:
        items = categories[cat_name]
        if not items:
            continue
        active = sorted([i for i in items if not i["is_closed"]], key=lambda x: -x["value"])
        closed = sorted([i for i in items if i["is_closed"]], key=lambda x: -x["prev_value"])
        report_lines.append(f"\n**{cat_name}**")
        for item in active + closed:
            report_lines.append(f"- {item['text']}")

    return {
        "report_text": "\n".join(report_lines),
        "categories": {k: [i["text"] for i in v] for k, v in categories.items() if v},
        "total_positions": sum(len(v) for v in categories.values()),
        "filtered_biotech": len(comparison_rows) - len(merged) if exclude_biotech else 0,
    }


def _safe_int(v) -> int:
    try:
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").strip()
        f = float(v)
        if pd.isna(f):
            return 0
        return int(f)
    except (ValueError, TypeError):
        return 0


def _safe_float(v) -> float:
    try:
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").strip()
        f = float(v)
        if pd.isna(f):
            return 0.0
        return f
    except (ValueError, TypeError):
        return 0.0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_summary(args):
    configure_identity()
    company = _resolve_institution(args.institution, auto_learn=_auto_learn(args))
    offset = _get_offset(args, company)
    thirteenf = _retry(lambda: company.get_filings(form="13F-HR")[offset].obj())

    data = {
        "manager": thirteenf.management_company_name,
        "report_period": str(thirteenf.report_period),
        "holdings": int(thirteenf.total_holdings),
        "total_value": float(thirteenf.total_value),
        "signer": str(thirteenf.signer),
    }
    _emit({"ok": True, "institution": args.institution, "offset": offset, "summary": data}, _as_json(args))


def cmd_top(args):
    configure_identity()
    company = _resolve_institution(args.institution, auto_learn=_auto_learn(args))
    offset = _get_offset(args, company)
    thirteenf = _retry(lambda: company.get_filings(form="13F-HR")[offset].obj())
    holdings = thirteenf.holdings
    top_n = holdings.nlargest(args.limit, "Value")

    rows = []
    for _, row in top_n.iterrows():
        rows.append({
            "ticker": str(row.get("Ticker", "")),
            "issuer": str(row.get("Issuer", "")),
            "shares": _safe_int(row.get("SharesPrnAmount", 0)),
            "value": _safe_float(row.get("Value", 0)),
        })
    _emit({
        "ok": True, "institution": args.institution, "offset": offset,
        "report_period": str(thirteenf.report_period),
        "rows": rows,
    }, _as_json(args))


# Hillhouse 等同体系多个 13F 主体：按 CUSIP 合并后再出报告（SKILL.md §11 / Hillhouse 合并规则）
_DEFAULT_MERGES = {"1762304": ["0002016939"]}  # HHLR Advisors + Evergreen Quality Fund GP


def _rows_from_comparison(comparison) -> list:
    try:
        df = getattr(comparison, "data", None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.to_dict(orient="records")
        if hasattr(comparison, "to_pandas"):
            df2 = comparison.to_pandas()
            if isinstance(df2, pd.DataFrame) and not df2.empty:
                return df2.to_dict(orient="records")
    except Exception:
        pass
    return []


def _compare_rows_for_period(institution: str, period: str, auto_learn: bool) -> tuple:
    """取另一主体同一报告期的 compare rows；找不到同期申报时返回空列表。"""
    company = _resolve_institution(institution, auto_learn=auto_learn)
    filings = company.get_filings(form="13F-HR")
    for i in range(min(len(filings), 8)):
        try:
            tf = _retry(lambda i=i: filings[i].obj())
        except Exception:
            continue
        if str(tf.report_period) == period:
            comp = _retry(lambda: tf.compare_holdings())
            return (_rows_from_comparison(comp) if comp is not None else []), tf.management_company_name
    return [], None


def _num(v) -> float:
    try:
        f = float(v)
        return 0.0 if f != f else f
    except (TypeError, ValueError):
        return 0.0


def _merge_rows_by_cusip(*row_sets) -> list:
    """按 CUSIP 加总 Shares/Value/PrevShares/PrevValue，再重算变动、百分比和 Status。"""
    merged: dict = {}
    for rows in row_sets:
        for r in rows:
            key = str(r.get("Cusip") or r.get("cusip") or "").strip() or f"{r.get('Ticker')}|{r.get('Issuer')}"
            m = merged.setdefault(key, {**r, "Shares": 0.0, "Value": 0.0, "PrevShares": 0.0, "PrevValue": 0.0})
            for f in ("Shares", "Value", "PrevShares", "PrevValue"):
                m[f] += _num(r.get(f))
            if not m.get("Ticker") and r.get("Ticker"):
                m["Ticker"] = r.get("Ticker")
    nan = float("nan")
    out = []
    for m in merged.values():
        cur, prev = m["Shares"], m["PrevShares"]
        if cur > 0 and prev <= 0:
            m.update({"Status": "NEW", "PrevShares": nan, "PrevValue": nan, "ShareChange": nan, "ShareChangePct": nan,
                      "ValueChange": nan, "ValueChangePct": nan})
        elif cur <= 0 and prev > 0:
            m.update({"Status": "CLOSED", "Shares": nan, "Value": nan, "ShareChange": nan, "ShareChangePct": nan,
                      "ValueChange": nan, "ValueChangePct": nan})
        elif cur > 0:
            chg = cur - prev
            m.update({"ShareChange": chg, "ShareChangePct": chg / prev * 100,
                      "ValueChange": m["Value"] - m["PrevValue"],
                      "ValueChangePct": (m["Value"] - m["PrevValue"]) / m["PrevValue"] * 100 if m["PrevValue"] else nan,
                      "Status": "INCREASED" if chg > 0 else "DECREASED" if chg < 0 else "UNCHANGED"})
        else:
            continue
        out.append(m)
    return out


def _merge_targets(args, company) -> list:
    if getattr(args, "no_merge", False):
        return []
    explicit = [x.strip() for x in (getattr(args, "merge_with", "") or "").split(",") if x.strip()]
    if explicit:
        return explicit
    cik = str(getattr(company, "cik", "") or "").lstrip("0")
    return list(_DEFAULT_MERGES.get(cik, []))


def cmd_compare(args):
    configure_identity()
    auto_learn = _auto_learn(args)
    company = _resolve_institution(args.institution, auto_learn=auto_learn)
    offset = _get_offset(args, company)
    thirteenf = _retry(lambda: company.get_filings(form="13F-HR")[offset].obj())
    comparison = _retry(lambda: thirteenf.compare_holdings())

    # Extract rows (stable path: HoldingsComparison.data is a DataFrame)
    rows = _rows_from_comparison(comparison) if comparison is not None else []

    merge_targets = _merge_targets(args, company)
    merged_with = []
    for other in merge_targets:
        other_rows, other_name = _compare_rows_for_period(other, str(thirteenf.report_period), auto_learn)
        merged_with.append({
            "institution": other,
            "manager": other_name,
            "rows": len(other_rows),
            "note": None if other_rows else "该主体没有同一报告期的 13F，未合并",
        })
        if other_rows:
            rows = _merge_rows_by_cusip(rows, other_rows)

    result = {
        "ok": True,
        "institution": args.institution,
        "offset": offset,
        "merged_with": merged_with,
        "manager": thirteenf.management_company_name,
        "base_period": str(thirteenf.report_period),
        "auto_learn": auto_learn,
        "rows": rows,
        "raw_text": str(comparison),
    }

    # Fetch prior quarter comparison for trend context.
    prior_actions = {}
    prev_quarter_label = ""
    try:
        cur_period = str(thirteenf.report_period)
        filings = company.get_filings(form="13F-HR")
        prev_offset = offset + 1
        scanned = 0
        while prev_offset < len(filings) and scanned < 5:
            scanned += 1
            try:
                prev_thirteenf = _retry(lambda po=prev_offset: filings[po].obj())
            except Exception:
                prev_offset += 1
                continue
            cand_period = str(prev_thirteenf.report_period)
            if cand_period >= cur_period:
                prev_offset += 1
                continue
            prev_comparison = None
            try:
                prev_comparison = _retry(lambda: prev_thirteenf.compare_holdings())
            except Exception:
                prev_comparison = None
            if prev_comparison is None:
                prev_offset += 1
                continue
            prev_quarter_label = _quarter_label(cand_period)
            prev_rows = _rows_from_comparison(prev_comparison)
            for other in merge_targets:
                other_prev, _ = _compare_rows_for_period(other, cand_period, auto_learn)
                if other_prev:
                    prev_rows = _merge_rows_by_cusip(prev_rows, other_prev)
            if prev_rows:
                prior_actions = _extract_actions(prev_rows, exclude_biotech=not args.include_biotech)
            break
    except Exception:
        pass

    # Build report (also triggers auto-learn), only expose formatted text in cn mode
    if rows and (args.format == "cn" or auto_learn):
        report = _build_report(
            rows,
            exclude_biotech=not args.include_biotech,
            auto_learn=auto_learn,
            prior_actions=prior_actions,
            prev_quarter_label=prev_quarter_label,
        )
        if args.format == "cn":
            result["formatted_report"] = report["report_text"]
            result["categories"] = report["categories"]
            result["total_positions"] = report["total_positions"]
            result["filtered_biotech"] = report["filtered_biotech"]

    _emit(result, _as_json(args))


def cmd_search(args):
    """Search institution by name."""
    query = args.query.lower().strip()
    matches = []
    for name, cik in _INSTITUTION_MAP.items():
        if name.startswith("_"):
            continue
        if query in name.lower():
            matches.append({"name": name, "cik": cik})

    if matches:
        _emit({"ok": True, "query": args.query, "matches": matches}, _as_json(args))
    else:
        _emit({
            "ok": False,
            "query": args.query,
            "error": "No matching institution found.",
            "hint": "Try a shorter name, or use CIK directly.",
        }, _as_json(args))


def cmd_map_show(args):
    map_type = args.type
    if map_type == "classification":
        payload = _CLASSIFICATION
    elif map_type == "institution":
        payload = _INSTITUTION_MAP
    elif map_type == "merge":
        payload = _MERGE_RULES
    elif map_type == "alias":
        payload = _TICKER_ALIASES
    else:
        payload = {
            "classification": _CLASSIFICATION,
            "institution": _INSTITUTION_MAP,
            "merge": _MERGE_RULES,
            "ticker_aliases": _TICKER_ALIASES,
        }

    if args.key:
        k = args.key.strip()
        ku = k.upper()
        kl = k.lower()
        out = {
            "classification": {
                "china_book": ku in set(_CLASSIFICATION.get("china_book", [])),
                "ai_semi_saas": ku in set(_CLASSIFICATION.get("ai_semi_saas", [])),
                "other_us_book": ku in set(_CLASSIFICATION.get("other_us_book", [])),
            },
            "institution": _INSTITUTION_MAP.get(kl),
            "merge": _MERGE_RULES.get(ku),
            "alias": _TICKER_ALIASES.get(ku),
        }
        _emit({"ok": True, "key": k, "result": out}, _as_json(args))
        return

    _emit({"ok": True, "type": map_type, "data": payload}, _as_json(args))


def cmd_map_propose(args):
    if not _WRITES_ENABLED:
        _emit({
            "ok": False,
            "error": "--workspace-dir is required for map-propose (read-only mode).",
            "hint": "Pass --workspace-dir <workspace> to persist patches under <workspace>/state/dalton-13f-tracker/.",
        }, _as_json(args))
        return

    ops = _parse_map_instruction(args.instruction)
    if not ops:
        _emit({
            "ok": False,
            "error": "No valid operations parsed from instruction.",
            "hint": "示例：'把 CAIFY 从 AI 改到 Other'；'hhlr -> 0001762304'；'merge GOOG,GOOGL -> Alphabet'",
        }, _as_json(args))
        return

    store = _load_patch_store()
    # Random suffix: two processes proposing in the same second must not share an id,
    # or the later save would replace the earlier proposal under the id it returned.
    patch_id = f"p{int(time.time())}_{uuid.uuid4().hex[:8]}"
    patch = {
        "id": patch_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "instruction": args.instruction,
        "operations": ops,
        "status": "proposed",
    }
    store.setdefault("patches", []).append(patch)
    _save_patch_store(store)

    _emit({"ok": True, "patch_id": patch_id, "operations": ops}, _as_json(args))


def cmd_map_apply(args):
    if not _WRITES_ENABLED:
        _emit({
            "ok": False,
            "error": "--workspace-dir is required for map-apply (read-only mode).",
            "hint": "Pass --workspace-dir <workspace> to apply patches under <workspace>/state/dalton-13f-tracker/.",
        }, _as_json(args))
        return

    store = _load_patch_store()
    patches = store.get("patches", [])
    target = None
    for p in patches:
        if p.get("id") == args.patch_id:
            target = p
            break

    if not target:
        _emit({"ok": False, "error": f"Patch not found: {args.patch_id}"}, _as_json(args))
        return

    if target.get("status") == "applied":
        _emit({
            "ok": True,
            "patch_id": args.patch_id,
            "status": "already_applied",
            "applied": target.get("applied", []),
        }, _as_json(args))
        return

    applied = _apply_ops(target.get("operations", []), source="manual-patch")
    target["status"] = "applied"
    target["applied"] = applied
    target["applied_at"] = datetime.now().isoformat(timespec="seconds")
    _save_patch_store(store)

    _emit({"ok": True, "patch_id": args.patch_id, "status": "applied", "applied": applied}, _as_json(args))


def cmd_self_test(args):
    _identity, identity_source = configure_identity()
    checks = [
        {
            "name": "env.SEC_IDENTITY",
            "ok": identity_source != "default",
            "source": identity_source,
        },
        {"name": "classification.json", "ok": bool(_CLASSIFICATION)},
        {"name": "merge_rules.json", "ok": bool(_MERGE_RULES)},
        {"name": "institution_map.json", "ok": bool(_INSTITUTION_MAP)},
        {"name": "name_to_ticker.json", "ok": bool(_NAME_TO_TICKER)},
        {"name": "ticker_aliases.json", "ok": isinstance(_TICKER_ALIASES, dict)},
    ]

    try:
        company = _resolve_institution(args.institution, auto_learn=False)
        checks.append({"name": "institution.lookup", "ok": True})
        thirteenf = _retry(lambda: company.get_filings(form="13F-HR")[0].obj())
        checks.append({"name": "13f.latest", "ok": True, "period": str(thirteenf.report_period)})
    except Exception as e:
        checks.append({"name": "institution/13f", "ok": False, "error": str(e), "hint": _error_hint(str(e))})

    ok = all(c.get("ok") for c in checks)
    _emit({"ok": ok, "checks": checks}, _as_json(args))


def _as_json(args) -> bool:
    return bool(getattr(args, "json", False))


def _auto_learn(args) -> bool:
    val = getattr(args, "auto_learn", "true")
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _get_offset(args, company=None) -> int:
    """Get offset from args, supporting both --offset and --quarter."""
    if hasattr(args, "quarter") and args.quarter:
        if company is None:
            raise RuntimeError("Company required for --quarter resolution")
        return _quarter_to_offset(args.quarter, company)
    return getattr(args, "offset", 0)


# Initialize module-level read-only state at import so library callers work without CLI.
_init_state(None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="SEC 13F Analyzer")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--workspace-dir", default=None, help="Workspace root for state/dalton-13f-tracker overlay")
    sub = parser.add_subparsers(dest="command", required=True)

    # Common subparser flags (SUPPRESS so pre-subcommand flags are not overwritten)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")
    common.add_argument("--workspace-dir", default=argparse.SUPPRESS, help="Workspace root for state/dalton-13f-tracker overlay")

    # Shared institution/quarter args
    parent = argparse.ArgumentParser(add_help=False, parents=[common])
    parent.add_argument("--institution", required=True,
                        help="Ticker, CIK, or name (e.g., 'HHLR', '0001762304', 'berkshire')")
    parent.add_argument("--offset", type=int, default=0,
                        help="0=latest quarter, 1=previous, etc.")
    parent.add_argument("--quarter", type=str, default="",
                        help="Quarter string e.g. '25Q4', '2025Q3' (alternative to --offset)")
    parent.add_argument("--auto-learn", choices=["true", "false"], default="true",
                        help="Auto-update classification/institution/merge overlay when new info appears (default: true)")

    # summary
    p = sub.add_parser("summary", parents=[parent])
    p.set_defaults(func=cmd_summary)

    # top
    p = sub.add_parser("top", parents=[parent])
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_top)

    # compare
    p = sub.add_parser("compare", parents=[parent])
    p.add_argument("--format", choices=["raw", "cn"], default="cn",
                   help="Output format: 'raw'=data only, 'cn'=auto-formatted Chinese report (default)")
    p.add_argument("--include-biotech", action="store_true",
                   help="Include biotech companies (excluded by default)")
    p.add_argument("--merge-with", default="",
                   help="Comma-separated institutions merged by CUSIP for the same report period "
                        "(HHLR auto-merges Evergreen 0002016939)")
    p.add_argument("--no-merge", action="store_true", help="Disable default merges (e.g. HHLR alone)")
    p.set_defaults(func=cmd_compare)

    # search
    p = sub.add_parser("search", parents=[common])
    p.add_argument("--query", required=True, help="Institution name to search")
    p.set_defaults(func=cmd_search)

    # map-show
    p = sub.add_parser("map-show", parents=[common])
    p.add_argument("--type", choices=["classification", "institution", "merge", "alias", "all"], default="all")
    p.add_argument("--key", default="", help="Optional key to inspect (ticker/alias)")
    p.set_defaults(func=cmd_map_show)

    # map-propose
    p = sub.add_parser("map-propose", parents=[common])
    p.add_argument("--instruction", required=True,
                   help="Natural language instruction, e.g. '把 CAIFY 从 AI 改到 Other' or 'merge GOOG,GOOGL -> Alphabet'")
    p.set_defaults(func=cmd_map_propose)

    # map-apply
    p = sub.add_parser("map-apply", parents=[common])
    p.add_argument("--patch-id", required=True)
    p.set_defaults(func=cmd_map_apply)

    # self-test
    p = sub.add_parser("self-test", parents=[common])
    p.add_argument("--institution", default="0001067983")
    p.set_defaults(func=cmd_self_test)

    args = parser.parse_args()
    ws_dir = getattr(args, "workspace_dir", None)
    _init_state(ws_dir)
    if not ws_dir:
        print(
            "INFO: --workspace-dir not set; reading built-in mappings only (writes disabled).",
            file=sys.stderr,
        )
    configure_identity(warn_default=True)
    try:
        args.func(args)
    except Exception as e:
        _emit({"ok": False, "error": str(e), "hint": _error_hint(str(e))}, _as_json(args))
        sys.exit(1)


if __name__ == "__main__":
    main()
