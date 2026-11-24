#!/usr/bin/env python3
"""13F Holdings Analyzer — deterministic formatting, auto-classification, biotech filter."""
from __future__ import annotations

import argparse
import copy
import fcntl
import importlib.util
import inspect
import json
import math
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pandas as pd

# Lazy-loaded so offline callers (e.g. audit_13f_run.py) never trigger edgartools cache init.
Company: Any = None
set_identity: Any = None


def _ensure_set_identity() -> None:
    global set_identity
    if set_identity is None:
        from edgar import set_identity as _set_identity

        set_identity = _set_identity


def _ensure_company() -> None:
    global Company
    if Company is None:
        from edgar import Company as _Company

        Company = _Company

pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 2000)
pd.set_option("display.max_colwidth", None)

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_SUBDIR = Path("state") / "dalton-13f-tracker"
DEFAULT_EDGAR_IDENTITY = "DaltonResearch sec-contact@example.com"

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
_VERIFIED_TICKERS: dict[str, Any] = {}
_DESCRIPTORS_DATA: dict[str, Any] = {}
_MAJOR_BLUECHIPS: set[str] = set()
_FAMILIAR_NAMES: set[str] = set()
_DESCRIPTOR_MAP: dict[str, str] = {}
_ADDED_DESCRIPTORS_BY_Q: dict[str, list[str]] = {}

_CHINA_TICKERS: set[str] = set()
_AI_SEMI_TICKERS: set[str] = set()
_OTHER_US_TICKERS: set[str] = set()
_AMBIGUOUS_NAME_TICKERS: set[str] = set()
_AMBIGUOUS_NAME_KEYS: set[str] = set()
_SPONSOR_FUND_TICKERS: dict[str, set[str]] = {}


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
    _ensure_set_identity()
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
    global _CLASSIFICATION, _MERGE_RULES, _INSTITUTION_MAP, _NAME_TO_TICKER, _TICKER_ALIASES, _VERIFIED_TICKERS
    global _DESCRIPTORS_DATA, _MAJOR_BLUECHIPS, _FAMILIAR_NAMES, _DESCRIPTOR_MAP, _ADDED_DESCRIPTORS_BY_Q

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
    raw_aliases = merged_dicts["ticker_aliases.json"]
    _TICKER_ALIASES = {
        k: v
        for k, v in raw_aliases.items()
        if k == "_comment"
        or not (_derivative_ticker_kind(str(k)) and not _derivative_ticker_kind(str(v)))
    }
    verified = _read_json_file(SCRIPT_DIR / "verified_tickers.json")
    if _STATE_DIR is not None:
        ws_verified = _read_json_file(_STATE_DIR / "verified_tickers.json")
        if isinstance(ws_verified, dict):
            verified.update(ws_verified)
    _VERIFIED_TICKERS = verified

    builtin_desc = _read_json_file(SCRIPT_DIR / "descriptors.json")
    ws_desc = _read_json_file(_STATE_DIR / "descriptors.json") if _STATE_DIR is not None else {}

    def _upper_set(raw_list: Any) -> set[str]:
        if not isinstance(raw_list, list):
            return set()
        return {str(t).upper().strip() for t in raw_list if str(t).strip()}

    raw_remove = ws_desc.get("remove")
    remove_both = _upper_set(raw_remove) if isinstance(raw_remove, list) else set()
    remove_map = raw_remove if isinstance(raw_remove, dict) else {}
    remove_major = remove_both | _upper_set(remove_map.get("major_bluechips")) | _upper_set(ws_desc.get("remove_major_bluechips"))
    remove_familiar = remove_both | _upper_set(remove_map.get("familiar_names")) | _upper_set(ws_desc.get("remove_familiar_names"))

    major = (_upper_set(builtin_desc.get("major_bluechips")) - remove_major) | _upper_set(ws_desc.get("major_bluechips"))
    familiar = (_upper_set(builtin_desc.get("familiar_names")) - remove_familiar) | _upper_set(ws_desc.get("familiar_names"))
    desc_map: dict[str, str] = {}
    for src_map in (builtin_desc.get("descriptors") or {}, ws_desc.get("descriptors") or {}):
        if isinstance(src_map, dict):
            for k, v in src_map.items():
                tk = str(k).upper().strip()
                if not tk:
                    continue
                if v is None or not str(v).strip():
                    desc_map.pop(tk, None)
                else:
                    desc_map[tk] = str(v).strip()
    added_by_q: dict[str, list[str]] = {}
    raw_added = ws_desc.get("_added_by_quarter")
    if isinstance(raw_added, dict):
        for qk, tlist in raw_added.items():
            if isinstance(tlist, list):
                added_by_q[str(qk).upper().strip()] = sorted(
                    {str(x).upper().strip() for x in tlist if str(x).strip()}
                )
    _MAJOR_BLUECHIPS = major
    _FAMILIAR_NAMES = familiar
    _DESCRIPTOR_MAP = desc_map
    _ADDED_DESCRIPTORS_BY_Q = added_by_q
    _DESCRIPTORS_DATA = {
        "major_bluechips": sorted(major),
        "familiar_names": sorted(familiar),
        "descriptors": dict(desc_map),
        "_added_by_quarter": dict(added_by_q),
    }

    _CHINA_TICKERS.clear()
    _CHINA_TICKERS.update(_CLASSIFICATION.get("china_book", []))
    _AI_SEMI_TICKERS.clear()
    _AI_SEMI_TICKERS.update(_CLASSIFICATION.get("ai_semi_saas", []))
    _OTHER_US_TICKERS.clear()
    _OTHER_US_TICKERS.update(_CLASSIFICATION.get("other_us_book", []))
    _AMBIGUOUS_NAME_KEYS.clear()
    _rebuild_name_lookup_index()


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
    "semiconductor", "nvidia", "microsoft", "alphabet", "meta", "amazon",
    "software", "cloud", "analytics", "saas", "tsm", "broadcom", "adobe",
    "artificial intelligence", "machine learning", "hynix", "cerebras",
    "seagate technology", "western digital", "sandisk", "micron technology", "optoelectronics",
    "quantum computing", "neocloud", "gas turbine", "fuel cell", "fuelcell",
    "power grid", "grid infrastructure", "switchgear", "data center power",
]

_UMBRELLA_TRUST_RE = re.compile(
    r"^(?:"
    r"ISHARES(?:\s+(?:TR|TRUST|INC))?|"
    r"VANECK\s+ETF(?:\s+(?:TR|TRUST))?|"
    r"(?:SELECT\s+SECTOR\s+)?SPDR\s+(?:SER|SERIES)\s+(?:TR|TRUST)|"
    r"SPDR\s+(?:SERIES|INDEX(?:\s+SHS\s+FDS)?)|"
    r"SELECT\s+SECTOR(?:\s+SPDR\s+TR)?|"
    r"VANGUARD\s+(?:ADMIRAL|BD\s+INDEX\s+FDS|INDEX(?:\s+FDS)?|SCOTTSDALE(?:\s+FDS)?|"
    r"SPECIALIZED(?:\s+FUNDS)?|STAR(?:\s+FDS)?|TAX-MANAGED(?:\s+FDS)?|WHITEHALL\s+FDS|WORLD(?:\s+FD)?)|"
    r"PROSHARES(?:\s+(?:TR|TRUST)(?:\s+II)?)?|"
    r"FIDELITY\s+COVINGTON(?:\s+TRUST)?|"
    r"KRANESHARES\s+TRUST|"
    r"GLOBAL\s+X(?:\s+FDS)?|"
    r"WISDOMTREE(?:\s+(?:TR|TRUST))?|"
    r"ARK\s+ETF(?:\s+(?:TR|TRUST))?|"
    r"ALPS\s+ETF(?:\s+(?:TR|TRUST))?|"
    r"DBX\s+ETF(?:\s+(?:TR|TRUST))?|"
    r"AMERICAN\s+CENTY(?:\s+ETF\s+TR)?|"
    r"AMPLIFY\s+(?:ETF(?:\s+TR)?|COMMODITY(?:\s+TRUST)?)|"
    r"BONDBLOXX\s+ETF(?:\s+TRUST)?|"
    r"ETF\s+SER(?:IES)?(?:\s+SOLUTIONS)?|"
    r"FIRST\s+TR(?:UST)?|"
    r"GOLDMAN\s+SACHS\s+ETF\s+TR|"
    r"INVESCO\s+(?:EXCHANGE\s+TRADED\s+FD\s+T|ACTVELY|TR)|"
    r"J\s*P\s*MORGAN\s+EXCHANGE\s+TRADED\s+F|"
    r"LISTED\s+FDS(?:\s+TR)?|"
    r"SCHWAB\s+STRATEGIC(?:\s+TR)?|"
    r"SSGA\s+ACTIVE(?:\s+ETF\s+TR)?|"
    r"TIDAL\s+(?:ETF\s+)?TR|"
    r"ELEVATION\s+SERIES|"
    r"IPATH\s+SERIES"
    r")$"
)

_ISSUER_STOPWORDS = frozenset({
    "INC", "INCORPORATED", "CORP", "CORPORATION", "LTD", "LIMITED", "PLC",
    "CO", "COMPANY", "LLC", "LP", "GROUP", "GRP", "HOLDING", "HOLDINGS",
    "HLDG", "HLDGS", "HLDNGS", "SA", "NV", "SPA", "AG", "DEL", "NEW",
    "TR", "TRUST", "ETF", "FD", "FDS", "FUNDS", "SER", "SERIES",
    "THE", "OF", "AND", "FOR", "&",
})


def _issuer_stem_tokens(name: str) -> set[str]:
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", (name or "").upper()).strip()
    return {
        w[:4]
        for w in cleaned.split()
        if w not in _ISSUER_STOPWORDS and len(w) >= 2
    }


_FUND_TYPE_WORDS = frozenset({"FDS", "FUNDS", "TRUST", "TR", "ETF"})
_UMBRELLA_SERIES_WORDS = frozenset({
    "FDS", "FUNDS", "SER", "SERIES", "ADMIRAL", "SCOTTSDALE", "SPECIALIZED",
    "TAX-MANAGED", "WHITEHALL", "COVINGTON", "STRATEGIC", "EXCHANGE", "TRADED",
    "LISTED", "ACTIVE", "ACTVELY", "SELECT", "SECTOR", "WORLD", "STAR",
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "LTD", "LP", "LLC",
    "I", "II", "III", "IV", "T", "F", "FD", "FUND",
})


def _is_umbrella_issuer(issuer_upper: str) -> bool:
    norm = re.sub(r"[\.,]+", " ", (issuer_upper or "").upper())
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return False
    if _UMBRELLA_TRUST_RE.match(norm):
        return True
    toks = norm.split()
    if not (set(toks) & _FUND_TYPE_WORDS):
        return False
    rem = [t for t in toks if t not in (_FUND_TYPE_WORDS | _UMBRELLA_SERIES_WORDS)]
    is_plural_series = bool(set(toks) & {"FDS", "FUNDS", "SER", "SERIES"})
    if not rem:
        return True
    if len(rem) == 1 and len(_SPONSOR_FUND_TICKERS.get(rem[0], ())) >= 3:
        return True
    if is_plural_series and len(rem) <= 2 and len(_SPONSOR_FUND_TICKERS.get(toks[0], ())) >= 3:
        return True
    return False


_DERIV_PARENT_TICKERS: set[str] = set()


def _rebuild_name_lookup_index() -> None:
    """Index sponsor fund counts, derivative parents, and multi-issuer tickers."""
    _AMBIGUOUS_NAME_TICKERS.clear()
    _SPONSOR_FUND_TICKERS.clear()
    _DERIV_PARENT_TICKERS.clear()
    for tk in _CHINA_TICKERS | _AI_SEMI_TICKERS | _OTHER_US_TICKERS | set(_MERGE_RULES):
        t = _clean_ticker(tk)
        if not t or t.startswith("_"):
            continue
        m = (
            re.match(r"^([A-Z]{1,5})[.\-/](?:W|WS|WW|WT|R|RT|RTS)$", t)
            or re.match(r"^([A-Z]{3,})(?:WS|WW|WT|RT|RTS)$", t)
            or re.match(r"^([A-Z]{4,})(?:W|R)$", t)
        )
        if m:
            _DERIV_PARENT_TICKERS.add(m.group(1))
    for name, tk in _NAME_TO_TICKER.items():
        if not name or name.startswith("_"):
            continue
        t = _clean_ticker(tk)
        if not t:
            continue
        toks = re.sub(r"[\.,]+", " ", name.upper()).split()
        if toks and (set(toks) & _FUND_TYPE_WORDS):
            _SPONSOR_FUND_TICKERS.setdefault(toks[0], set()).add(t)

    by_ticker: dict[str, list[set[str]]] = {}
    for name, tk in _NAME_TO_TICKER.items():
        if not name or name.startswith("_") or _is_umbrella_issuer(name):
            continue
        t = _clean_ticker(tk)
        if not t:
            continue
        stems = _issuer_stem_tokens(name)
        if stems:
            by_ticker.setdefault(t, []).append(stems)
    for t, stem_groups in by_ticker.items():
        if len(stem_groups) < 2:
            continue
        base = stem_groups[0]
        if any(not (g & base) for g in stem_groups[1:]):
            _AMBIGUOUS_NAME_TICKERS.add(t)


def _lookup_ticker_by_issuer(issuer_upper: str) -> str:
    """Reverse-lookup equity ticker by issuer only when non-umbrella and uniquely mapped."""
    key = (issuer_upper or "").upper().strip()
    if not key or key in _AMBIGUOUS_NAME_KEYS or _is_umbrella_issuer(key):
        return ""
    cand = _clean_ticker(_NAME_TO_TICKER.get(key, ""))
    if not cand:
        return ""
    if _detect_security_kind(cand, key, "") != "equity":
        return ""
    if cand in _AMBIGUOUS_NAME_TICKERS:
        # Allow only when candidate's canonical name explicitly matches the queried issuer
        canon = _MERGE_RULES.get(cand, "")
        canon_stems = _issuer_stem_tokens(canon)
        iss_stems = _issuer_stem_tokens(key)
        if not canon_stems or not (canon_stems & iss_stems):
            return ""
    return cand


def _clean_ticker(raw) -> str:
    """Normalize SEC ticker; treat missing values (None/NaN/pd.NA/blank) as '', preserving real ticker 'NAN'."""
    if raw is None:
        return ""
    if isinstance(raw, float) and math.isnan(raw):
        return ""
    try:
        if pd.isna(raw):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(raw).strip()
    if not s or s == "<NA>":
        return ""
    return s.upper()


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
    t = _clean_ticker(ticker)
    if not t or len(t) > 6 or not _is_valid_ticker(t):
        return
    if _is_umbrella_issuer((issuer or "").upper().strip()):
        return
    if _detect_security_kind(t, issuer, "") != "equity":
        return
    if len(t) >= 5 and re.search(r"PR[A-Z]$", t):
        return
    changed = False
    for name in (issuer, short_name):
        key = (name or "").upper().strip()
        if not key or key == t or len(key) <= 1 or _is_umbrella_issuer(key):
            continue
        existing = _NAME_TO_TICKER.get(key)
        if existing is None:
            _NAME_TO_TICKER[key] = t
            changed = True
        elif existing != t and name == issuer:
            _AMBIGUOUS_NAME_KEYS.add(key)
    if changed:
        _rebuild_name_lookup_index()
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
    if _derivative_ticker_kind(s) and not _derivative_ticker_kind(c):
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
    _AMBIGUOUS_NAME_KEYS.clear()
    _rebuild_name_lookup_index()
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
    if not t or not c or _derivative_ticker_kind(t):
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
        _ensure_company()
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
def _ledger_entry_matches(ticker: str, cusip_norm: str = "", issuer: str = "") -> bool:
    """Verify row (CUSIP, issuer) matches verified_tickers entry for ticker (prevents ticker reuse mixups)."""
    t = _clean_ticker(ticker)
    if not t:
        return True
    vt = _VERIFIED_TICKERS.get(t)
    if not isinstance(vt, dict):
        return True

    row_cusip = (cusip_norm or "").strip().upper()
    raw_vt_cusip = vt.get("cusip")
    if raw_vt_cusip and row_cusip:
        if isinstance(raw_vt_cusip, (list, tuple, set)):
            allowed_cusips = {str(c or "").strip().upper() for c in raw_vt_cusip if str(c or "").strip()}
        else:
            allowed_cusips = {str(raw_vt_cusip).strip().upper()}
        if allowed_cusips and row_cusip not in allowed_cusips:
            return False
        if allowed_cusips:
            # Same CUSIP is the same security; issuers rename (QIFU TECHNOLOGY ->
            # QFIN HOLDINGS), so the name is consulted only when no CUSIP decides.
            return True

    row_issuer = (issuer or "").strip()
    sample_issuer = str(vt.get("sample_issuer") or "").strip()
    if row_issuer and sample_issuer:
        row_stems = _issuer_stem_tokens(row_issuer)
        sample_stems = _issuer_stem_tokens(sample_issuer)
        canon_stems = _issuer_stem_tokens(str(vt.get("canonical_name") or ""))
        if row_stems and sample_stems and not (row_stems & (sample_stems | canon_stems)):
            return False
    return True


def _classify(
    ticker: str,
    issuer: str = "",
    auto_learn: bool = True,
    allow_issuer_lookup: bool = True,
    cusip: str = "",
) -> str:
    t = _clean_ticker(ticker)
    if not t and issuer and allow_issuer_lookup:
        t = _lookup_ticker_by_issuer(issuer)

    cusip_norm = (cusip or "").strip().upper()
    if t and not _ledger_entry_matches(t, cusip_norm, issuer):
        return _infer_category_fallback("", issuer)

    raw_deriv_kind = _derivative_ticker_kind(t)
    # 1) Resolve ticker alias from ticker_aliases.json BEFORE common fallback, but never map derivative -> common
    alias_val = _clean_ticker(_TICKER_ALIASES.get(t, t)) or t
    resolved = t if (raw_deriv_kind and not _derivative_ticker_kind(alias_val)) else alias_val

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

    # 4) Auto-detect bucket/alias from issuer name; NEVER learn derivative -> common ticker aliases
    if t and t not in all_classified:
        candidate = _lookup_ticker_by_issuer(issuer) if (auto_learn or raw_deriv_kind) else ""
        if candidate and candidate != t and candidate in all_classified:
            cat = (
                "China Book" if candidate in _CHINA_TICKERS
                else "AI/Semi/US SaaS" if candidate in _AI_SEMI_TICKERS
                else "Other US Book"
            )
            if raw_deriv_kind:
                if auto_learn:
                    _learn_classification(t, cat, source="auto-deriv-issuer")
                return cat
            if auto_learn:
                _learn_ticker_alias(t, candidate, source="auto-issuer-match")
                return cat

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
    "BANK OF AMER": "Bank of America",
    "BANK OF AMER CORP": "Bank of America",
}


_SEC_ABBREV_MAP = {
    "MATLS": "Materials", "INSTRS": "Instruments",
    "PWR": "Power", "FINL": "Financial", "SVCS": "Services",
    "MKTS": "Markets", "MKT": "Market", "PMTS": "Payments",
    "PETE": "Petroleum", "INDL": "Industrial", "CTZNS": "Citizens",
    "FMRS": "Farmers", "ENTMT": "Entertainment", "MNG": "Mining",
    "MTR": "Motor", "PAC": "Pacific", "TRANSN": "Transportation",
    "INS": "Insurance", "SYS": "Systems", "INTL": "International",
    "HLDNGS": "Holdings", "HLDGS": "Holdings", "HLDG": "Holding",
}

# SEC 28-char Issuer field truncations that are strict prefixes of corporate/tech suffixes
# and never standalone English words (kept separate from real words like 'tech' or 'limit').
_TRUNCATED_SUFFIX_PREFIXES = frozenset({
    "pl", "limite", "holdin", "hldng", "hldngs",
    "incorp", "incorpo", "incorpor", "incorpora", "incorporat", "incorporate",
    "corpor", "corpora", "corporat", "corporati", "corporatio", "compan",
    "techn", "techno", "technol", "technolo", "technolog", "technologi", "technologie",
    "internation", "softwar",
})


def _canonical_name_for_ticker(ticker: str, cusip_norm: str = "", issuer: str = "") -> str:
    """Return canonical display name from merge_rules.json or verified_tickers.json when identity matches."""
    t = _clean_ticker(ticker)
    if not t or not _ledger_entry_matches(t, cusip_norm, issuer):
        return ""
    mr_val = str(_MERGE_RULES.get(t) or "").strip()
    if mr_val and not mr_val.startswith("_"):
        return mr_val
    vt_entry = _VERIFIED_TICKERS.get(t)
    if isinstance(vt_entry, dict):
        vt_name = str(vt_entry.get("canonical_name") or "").strip()
        if vt_name and not vt_name.startswith("_"):
            return vt_name
    return ""


def _short_name(issuer: str, ticker: str, allow_issuer_lookup: bool = True, cusip: str = "") -> str:
    """Get clean, non-truncated display name."""
    t = _clean_ticker(ticker)
    issuer_upper = (issuer or "").upper().strip()
    if not t and issuer_upper and allow_issuer_lookup:
        t = _lookup_ticker_by_issuer(issuer_upper)

    cusip_norm = (cusip or "").strip().upper()
    ledger_match = _ledger_entry_matches(t, cusip_norm, issuer) if t else True

    canonical = _canonical_name_for_ticker(t, cusip_norm, issuer) if ledger_match else ""
    if canonical:
        return canonical

    if ledger_match and issuer_upper in _SHORT_NAMES:
        return _SHORT_NAMES[issuer_upper]

    raw = (issuer or "").strip()
    if raw:
        raw = re.sub(r"\s*\(THE\)\s*$", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\bEXCHANGE TRADED FUN$", "EXCHANGE TRADED FUNDS", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"\bN\.V\.?(?=\s|$)", "NV", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\bS\.A\.?(?=\s|$)", "SA", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\bS\.P\.A\.?(?=\s|$)", "SPA", raw, flags=re.IGNORECASE)
        cleaned = re.sub(r"[\.,]", " ", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        tokens = cleaned.split(" ")

        strip_set = {
            "inc", "incorporated", "corp", "corporation", "ltd", "limited", "limite",
            "plc", "pl", "co", "company", "llc", "lp", "group", "holding", "holdings",
            "hldg", "hldgs", "hldngs", "sa", "nv", "spa", "ag", "del", "new",
            "cayman", "mauritius", "luxembourg",
        }
        connectors = {"of", "&", "and", "the", "for", "de", "la"}
        while tokens:
            low = tokens[-1].lower()
            if (
                low in strip_set
                or (low == "in" and len(tokens) >= 3)
                or (len(tokens) >= 2 and low in _TRUNCATED_SUFFIX_PREFIXES)
            ):
                tokens.pop()
            else:
                break
        while len(tokens) > 1 and tokens[-1].lower() in connectors:
            tokens.pop()

        if [tok.upper() for tok in tokens] == ["BANK", "OF", "AMER"]:
            return "Bank of America"

        expanded = [_SEC_ABBREV_MAP.get(tok.upper(), tok) for tok in tokens]
        if expanded:
            # Keep full cleaned corporate name up to 4 tokens; never cut after connectors/initials
            keep = min(len(expanded), 4)
            while keep < len(expanded) and (
                expanded[keep - 1].lower() in connectors or len(expanded[keep - 1]) == 1
            ):
                keep += 1
            short_tokens = expanded[:keep]
            out_words = []
            for idx_w, w in enumerate(short_tokens):
                if idx_w > 0 and w.lower() in {"of", "and", "the", "for"}:
                    out_words.append(w.lower())
                elif w == "&":
                    out_words.append("&")
                else:
                    out_words.append(w.title())
            res_name = " ".join(out_words)
            if t and (not ledger_match or _is_umbrella_issuer(issuer_upper)):
                return f"{res_name} ({t})"
            return res_name

    return t or issuer or "Unknown"


# ---------------------------------------------------------------------------
# Value formatting (#1 Step 3)
# ---------------------------------------------------------------------------
def _format_value(value: float) -> str:
    """Format value in Chinese convention with a single ROUND_HALF_UP pass.

    Unit threshold uses raw USD: values >= 99.5M render in 亿美元 (1 decimal)
    so values rounding to 10000万 (e.g. 9960万) promote cleanly to 1.0亿美元,
    while values < 99.5M round once (ROUND_HALF_UP) to nearest million in 万美元.
    """
    raw = float(value or 0.0)
    if not math.isfinite(raw) or raw <= 0.0:
        return "0万美元"
    actual_dec = Decimal(str(raw))
    if actual_dec >= Decimal("99500000"):
        yi = (actual_dec / Decimal("100000000")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if yi < Decimal("1.0"):
            yi = Decimal("1.0")
        return f"{yi}亿美元"
    millions = int((actual_dec / Decimal("1000000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if millions == 0:
        millions = 1
    return f"{millions * 100}万美元"


# ---------------------------------------------------------------------------
# Change template (#1 Step 4)
# ---------------------------------------------------------------------------
def _pct_change_display(prev_shares: int, chg_shares: int) -> int:
    raw_pct = Decimal(abs(chg_shares) * 100) / Decimal(prev_shares)
    return int(raw_pct.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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
        abs_chg = abs(chg_shares)
        if abs_chg * 100 <= prev_shares * 2:
            return f"{name}仓位几乎不变，{val_str}"
        if chg_shares > 0:
            if chg_shares * 100 > prev_shares * 50:
                prev_val_str = _format_value(prev_value)
                return f"大幅加仓{name}至{val_str}，之前持仓{prev_val_str}"
            pct = _pct_change_display(prev_shares, chg_shares)
            return f"加仓{name} {pct}%至{val_str}"
        pct = _pct_change_display(prev_shares, chg_shares)
        return f"减仓{name} {pct}%至{val_str}"

    return f"{name}仓位不变，{val_str}"


def _detect_action(prev_shares: int, chg_shares: int) -> str:
    """Classify a position change using exact unrounded share ratios."""
    shares = prev_shares + chg_shares
    if prev_shares == 0 and chg_shares > 0:
        return "建仓"
    if prev_shares > 0 and shares == 0:
        return "清仓"
    if chg_shares == 0 and prev_shares > 0:
        return "不变"
    if prev_shares > 0:
        if abs(chg_shares) * 100 <= prev_shares * 2:
            return "几乎不变"
        if chg_shares > 0:
            return "大幅加仓" if chg_shares * 100 > prev_shares * 50 else "加仓"
        return "减仓"
    return "不变"


def _derivative_ticker_kind(ticker: str) -> str:
    """Return 'warrant'/'right'/'unit' when SEC ticker has a derivative suffix, else ''."""
    t = _clean_ticker(ticker)
    if not t:
        return ""
    if (
        re.match(r"^[A-Z]{1,5}[.\-/](?:W|WS|WW|WT)$", t)
        or re.match(r"^[A-Z]{3,}(?:WS|WW|WT)$", t)
        or (len(t) >= 5 and t.endswith("W"))
    ):
        return "warrant"
    if (
        re.match(r"^[A-Z]{1,5}[.\-/](?:R|RT|RTS)$", t)
        or re.match(r"^[A-Z]{3,}(?:RT|RTS)$", t)
        or (len(t) >= 5 and t.endswith("R"))
    ):
        return "right"
    if re.match(r"^[A-Z]{1,5}[.\-/]U$", t) or (len(t) >= 5 and t.endswith("U")):
        return "unit"
    return ""


def _detect_security_kind(ticker: str, issuer: str, cusip: str, title_of_class: str = "") -> str:
    """Classify 13F row instrument kind so equity never merges with CB/warrant/right/unit."""
    c = (cusip or "").strip()
    if len(c) >= 8 and any(ch.isalpha() for ch in c[6:8]):
        return "cb"

    t_kind = _derivative_ticker_kind(ticker)
    if t_kind:
        return t_kind

    text = f"{title_of_class or ''} {issuer or ''}".upper()
    if re.search(r"(?:\*W\s+EXP|\bWARRANTS?\b|\bWTS?\b)", text):
        return "warrant"
    if re.search(r"(?:\bRIGHTS?\b|\bRTS?\b)", text):
        return "right"
    if re.search(r"\bUNITS?\b", text):
        return "unit"
    return "equity"


def _apply_security_kind_to_merge_key(merge_key: str, sec_kind: str) -> str:
    if sec_kind == "cb":
        return merge_key if merge_key.endswith(" CB") else f"{merge_key} CB"
    if sec_kind == "warrant":
        return merge_key if "warrant" in merge_key.lower() else f"{merge_key} Warrants"
    if sec_kind == "right":
        return merge_key if "right" in merge_key.lower() else f"{merge_key} Rights"
    if sec_kind == "unit":
        return merge_key if "unit" in merge_key.lower() else f"{merge_key} Units"
    return merge_key


def _format_unresolved_cusip_name(name: str, cusip_norm: str) -> str:
    if not cusip_norm:
        return name
    marker = f"（CUSIP {cusip_norm}）"
    return name if marker in name else f"{name}{marker}"


_CUSIP_TICKER_DB: dict[str, str] | None = None


def _lookup_ticker_by_cusip(cusip_norm: str) -> str:
    """Lookup uppercase CUSIP in edgartools bundled ct.pq without triggering edgar cache init."""
    global _CUSIP_TICKER_DB
    c = (cusip_norm or "").strip().upper()
    if not c:
        return ""
    if _CUSIP_TICKER_DB is None:
        db: dict[str, str] = {}
        try:
            spec = importlib.util.find_spec("edgar")
            if spec and spec.origin:
                ct_path = Path(spec.origin).parent / "reference" / "data" / "ct.pq"
                if ct_path.is_file():
                    df = pd.read_parquet(ct_path)
                    dedup = df[~df["Cusip"].duplicated(keep="first")]
                    for raw_c, raw_t in zip(dedup["Cusip"], dedup["Ticker"]):
                        cn = str(raw_c or "").strip().upper()
                        tk = _clean_ticker(raw_t)
                        if cn and tk and cn not in db:
                            db[cn] = tk
        except Exception:
            db = {}
        _CUSIP_TICKER_DB = db
    return _CUSIP_TICKER_DB.get(c, "")


def _resolve_alias_safely(ticker: str) -> str:
    """Resolve ticker alias while refusing derivative -> common stock replacements."""
    t = _clean_ticker(ticker)
    if not t:
        return ""
    if _derivative_ticker_kind(t):
        return t
    return _clean_ticker(_TICKER_ALIASES.get(t, t)) or t


def _iter_period_rows(*row_sets):
    """Yield (period_str, rows) for raw row lists or (period_str, row_list) tuples."""
    for entry in row_sets:
        if isinstance(entry, tuple) and len(entry) == 2:
            yield str(entry[0] or ""), entry[1] or []
        else:
            yield "", entry or []


def _row_is_active_holding(row: dict) -> bool:
    """Return True if the row represents an actually held current position (shares > 0 and not CLOSED)."""
    status = str(row.get("Status") or "").upper().strip()
    if status == "CLOSED":
        return False
    for key in ("Shares", "SharesPrnAmount", "shares"):
        if key in row and row[key] is not None:
            raw_v = row[key]
            try:
                if float(raw_v) != float(raw_v):  # NaN -> CLOSED
                    return False
            except (TypeError, ValueError):
                pass
            return _safe_int(raw_v) > 0
    return True


def _resolve_cusip_tickers(*row_sets) -> dict[str, str]:
    """Resolve one order-independent canonical ticker per normalized uppercase CUSIP."""
    active_explicit: dict[str, list[tuple[tuple[int, int, str], str]]] = {}
    fallback_explicit: dict[str, list[tuple[tuple[int, int, str], str]]] = {}
    active_ticker_cusips: dict[str, set[str]] = {}
    all_ticker_cusips: dict[str, set[str]] = {}
    issuer_cusips: dict[str, set[str]] = {}
    all_cusips: set[str] = set()
    active_cusips: set[str] = set()

    for rep_period, rows in _iter_period_rows(*row_sets):
        p_rank = _period_sort_key(rep_period)
        for row in rows:
            cusip_norm = str(row.get("Cusip") or row.get("cusip") or "").strip().upper()
            if not cusip_norm:
                continue
            all_cusips.add(cusip_norm)
            is_active = _row_is_active_holding(row)
            if is_active:
                active_cusips.add(cusip_norm)
            issuer = str(row.get("Issuer") or row.get("issuer") or "").strip()
            issuer_up = issuer.upper()
            if issuer_up:
                issuer_cusips.setdefault(issuer_up, set()).add(cusip_norm)

            raw_t = _clean_ticker(row.get("Ticker") if row.get("Ticker") is not None else row.get("ticker"))
            if raw_t:
                resolved_t = _resolve_alias_safely(raw_t)
                row_p = str(row.get("ReportPeriod") or row.get("report_period") or "").strip()
                eff_p = _period_sort_key(row_p) if row_p else p_rank
                rank = (-eff_p, 0 if resolved_t in _VERIFIED_TICKERS else 1, resolved_t)
                if is_active:
                    active_explicit.setdefault(cusip_norm, []).append((rank, resolved_t))
                    active_ticker_cusips.setdefault(resolved_t, set()).add(cusip_norm)
                else:
                    fallback_explicit.setdefault(cusip_norm, []).append((rank, resolved_t))
                all_ticker_cusips.setdefault(resolved_t, set()).add(cusip_norm)

    # Closed rows (Shares==0) must never participate in ticker resolution when any active holding exists for that CUSIP
    cusip_to_ticker: dict[str, str] = {}
    for c in sorted(all_cusips):
        cands = active_explicit.get(c)
        if not cands and c not in active_cusips:
            cands = fallback_explicit.get(c)
        if cands:
            cusip_to_ticker[c] = min(cands, key=lambda x: x[0])[1]

    cusip_reps = _resolve_cusip_representatives(*row_sets)

    # Uppercase CUSIP lookup (recovers tickers missed when filings use lowercase CUSIPs like 92189f676)
    for cusip_norm in sorted(all_cusips):
        if cusip_norm in cusip_to_ticker:
            continue
        cusip_t = _lookup_ticker_by_cusip(cusip_norm)
        if cusip_t:
            resolved_t = _resolve_alias_safely(cusip_t)
            cusip_to_ticker[cusip_norm] = resolved_t
            all_ticker_cusips.setdefault(resolved_t, set()).add(cusip_norm)

    for cusip_norm in sorted(all_cusips):
        if cusip_norm in cusip_to_ticker:
            continue
        issuer, title = cusip_reps.get(cusip_norm, ("", ""))
        if not issuer:
            continue
        issuer_up = issuer.upper().strip()
        if len(issuer_cusips.get(issuer_up, set())) > 1:
            continue
        if _detect_security_kind("", issuer, cusip_norm, title) != "equity":
            continue
        cand = _lookup_ticker_by_issuer(issuer_up)
        if not cand or cand in _DERIV_PARENT_TICKERS:
            continue
        used_other = (active_ticker_cusips if active_cusips else all_ticker_cusips).get(cand, set()) - {cusip_norm}
        if used_other:
            continue
        cusip_to_ticker[cusip_norm] = cand

    return cusip_to_ticker


def _resolve_cusip_ledger_matches(
    cusip_map: dict[str, str],
    *row_sets,
) -> dict[tuple[str, str], bool]:
    """Compute one uniform ledger_match per (uppercase CUSIP, resolved ticker) across all rows.

    When verified_tickers has a CUSIP for the ticker, only CUSIP decides.
    When verified_tickers has no CUSIP, any matching issuer on that CUSIP marks the CUSIP
    as matching the ledger so differing issuer spellings on the same CUSIP never split keys.
    """
    cusip_issuers: dict[tuple[str, str], set[str]] = {}
    for _rep_p, rows in _iter_period_rows(*row_sets):
        for row in rows:
            cusip_norm = str(row.get("Cusip") or row.get("cusip") or "").strip().upper()
            if not cusip_norm:
                continue
            issuer = str(row.get("Issuer") or row.get("issuer") or "").strip()
            explicit_t = _clean_ticker(row.get("Ticker") if row.get("Ticker") is not None else row.get("ticker"))
            raw_deriv = _derivative_ticker_kind(explicit_t)
            explicit_res = explicit_t if raw_deriv else _resolve_alias_safely(explicit_t)
            resolved_t = cusip_map.get(cusip_norm, "") or explicit_res
            if raw_deriv:
                resolved_t = explicit_t
            if resolved_t:
                cusip_issuers.setdefault((cusip_norm, resolved_t), set()).add(issuer)

    out: dict[tuple[str, str], bool] = {}
    for (cusip_norm, resolved_t), issuers in cusip_issuers.items():
        vt = _VERIFIED_TICKERS.get(resolved_t)
        if isinstance(vt, dict) and vt.get("cusip"):
            out[(cusip_norm, resolved_t)] = _ledger_entry_matches(resolved_t, cusip_norm, "")
        else:
            out[(cusip_norm, resolved_t)] = any(
                _ledger_entry_matches(resolved_t, cusip_norm, iss) for iss in issuers
            )
    return out


def _period_sort_key(period: str) -> int:
    digits = "".join(ch for ch in str(period or "") if ch.isdigit())
    return int(digits[:8].ljust(8, "0")) if digits else 0


def _resolve_cusip_representatives(*row_sets) -> dict[str, tuple[str, str]]:
    """Pick one order-independent (issuer, title) representative per CUSIP.

    Order priority: active holding (shares > 0) before CLOSED, then latest filing period,
    then non-empty issuer, then lexicographical issuer/title order.
    """
    best: dict[str, tuple[tuple[int, int, int, str, str], tuple[str, str]]] = {}
    for rep_period, rows in _iter_period_rows(*row_sets):
        p_rank = _period_sort_key(rep_period)
        for row in rows:
            cusip_norm = str(row.get("Cusip") or row.get("cusip") or "").strip().upper()
            if not cusip_norm:
                continue
            explicit_t = _clean_ticker(row.get("Ticker") if row.get("Ticker") is not None else row.get("ticker"))
            if _derivative_ticker_kind(explicit_t):
                continue
            is_active = _row_is_active_holding(row)
            issuer = str(row.get("Issuer") or row.get("issuer") or "").strip()
            title = str(row.get("TitleOfClass") or row.get("Class") or row.get("title_of_class") or "").strip()
            row_p = str(row.get("ReportPeriod") or row.get("report_period") or "").strip()
            eff_p = _period_sort_key(row_p) if row_p else p_rank
            rank = (0 if is_active else 1, -eff_p, 0 if issuer else 1, issuer.upper(), title.upper())
            if cusip_norm not in best or rank < best[cusip_norm][0]:
                best[cusip_norm] = (rank, (issuer, title))
    return {c: val for c, (_rk, val) in best.items()}


def _row_identity_keys(
    row: dict,
    cusip_map: dict[str, str],
    cusip_ledger_map: dict[tuple[str, str], bool] | None = None,
    cusip_reps: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Compute unified CUSIP-first identity, merge key, action key, and display name for one row."""
    cusip_norm = str(row.get("Cusip") or row.get("cusip") or "").strip().upper()
    issuer = str(row.get("Issuer") or row.get("issuer") or "")
    title = str(row.get("TitleOfClass") or row.get("Class") or row.get("title_of_class") or "")
    explicit_t = _clean_ticker(row.get("Ticker") if row.get("Ticker") is not None else row.get("ticker"))
    raw_deriv_kind = _derivative_ticker_kind(explicit_t)
    explicit_resolved = explicit_t if raw_deriv_kind else _resolve_alias_safely(explicit_t)
    resolved_t = (cusip_map.get(cusip_norm, "") if cusip_norm else "") or explicit_resolved
    if raw_deriv_kind:
        resolved_t = explicit_t

    rep_issuer, rep_title = issuer, title
    if not raw_deriv_kind and cusip_reps and cusip_norm in cusip_reps:
        rep_issuer, rep_title = cusip_reps[cusip_norm]

    if resolved_t:
        sec_kind = _detect_security_kind(explicit_t or resolved_t, rep_issuer, cusip_norm, rep_title)
        if cusip_ledger_map is not None and cusip_norm and (cusip_norm, resolved_t) in cusip_ledger_map:
            ledger_match = cusip_ledger_map[(cusip_norm, resolved_t)]
        else:
            ledger_match = _ledger_entry_matches(resolved_t, cusip_norm, rep_issuer)
        short = _short_name(rep_issuer, resolved_t, cusip=cusip_norm)
        if ledger_match:
            canon_group = str(_MERGE_RULES.get(resolved_t) or (_MERGE_RULES.get(explicit_t) if explicit_t else "") or "").strip()
            if canon_group.startswith("_"):
                canon_group = ""
            canonical_display = _canonical_name_for_ticker(resolved_t, cusip_norm, rep_issuer) or (
                _canonical_name_for_ticker(explicit_t, cusip_norm, rep_issuer) if explicit_t else ""
            )
            base_display = canonical_display or short
            bucket_key: tuple[str, ...] = (
                ("canon", canon_group, sec_kind) if canon_group else ("ticker", resolved_t, sec_kind)
            )
            action_key: Any = ("canon", canon_group, sec_kind) if canon_group else ("ticker", resolved_t, sec_kind)
        else:
            base_display = short
            bucket_key = (
                ("cusip", cusip_norm, sec_kind)
                if cusip_norm
                else ("ticker_issuer", resolved_t, rep_issuer.upper().strip(), sec_kind)
            )
            action_key = ("cusip", cusip_norm) if cusip_norm else bucket_key
        display_name = _apply_security_kind_to_merge_key(base_display, sec_kind)
        return {
            "cusip": cusip_norm,
            "issuer": issuer,
            "rep_issuer": rep_issuer,
            "explicit_t": explicit_t,
            "resolved_t": resolved_t,
            "ledger_match": ledger_match,
            "sec_kind": sec_kind,
            "short": short,
            "display_name": display_name,
            "bucket_key": bucket_key,
            "action_key": action_key,
        }

    sec_kind = _detect_security_kind("", rep_issuer, cusip_norm, rep_title)
    short = _short_name(rep_issuer, "", allow_issuer_lookup=False)
    kind_name = _apply_security_kind_to_merge_key(short, sec_kind)
    display_name = _format_unresolved_cusip_name(kind_name, cusip_norm)
    bucket_key = ("cusip", cusip_norm, sec_kind) if cusip_norm else ("raw", rep_issuer, sec_kind)
    action_key = ("cusip", cusip_norm) if cusip_norm else ("raw", rep_issuer, sec_kind)
    return {
        "cusip": cusip_norm,
        "issuer": issuer,
        "rep_issuer": rep_issuer,
        "explicit_t": explicit_t,
        "resolved_t": "",
        "ledger_match": True,
        "sec_kind": sec_kind,
        "short": short,
        "display_name": display_name,
        "bucket_key": bucket_key,
        "action_key": action_key,
    }


def _extract_actions(
    comparison_rows: list,
    exclude_biotech: bool = True,
    cusip_ticker_map: dict[str, str] | None = None,
    cusip_ledger_map: dict[tuple[str, str], bool] | None = None,
    cusip_reps: dict[str, tuple[str, str]] | None = None,
) -> dict:
    """Extract per-CUSIP action from comparison rows (never shares action across distinct CUSIPs)."""
    cusip_map = cusip_ticker_map if cusip_ticker_map is not None else _resolve_cusip_tickers(comparison_rows)
    ledger_map = (
        cusip_ledger_map
        if cusip_ledger_map is not None
        else _resolve_cusip_ledger_matches(cusip_map, comparison_rows)
    )
    reps = cusip_reps if cusip_reps is not None else _resolve_cusip_representatives(comparison_rows)
    by_cusip: dict[tuple[str, str], dict[str, Any]] = {}
    for row in comparison_rows:
        ident = _row_identity_keys(row, cusip_map, ledger_map, reps)
        if exclude_biotech and _is_biotech(ident["resolved_t"] or ident["explicit_t"], ident.get("rep_issuer") or ident["issuer"]):
            continue
        c = ident["cusip"]
        if not c:
            continue
        ck = (c, ident["sec_kind"])
        if ck not in by_cusip:
            by_cusip[ck] = {
                "cusip": c,
                "sec_kind": ident["sec_kind"],
                "shares": 0,
                "prev_shares": 0,
                "chg_shares": 0,
            }
        entry = by_cusip[ck]
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
    for e in by_cusip.values():
        e["chg_shares"] = e["shares"] - e["prev_shares"]
        act = _detect_action(e["prev_shares"], e["chg_shares"])
        result[("cusip", e["cusip"], e["sec_kind"])] = act
        result.setdefault(("cusip", e["cusip"]), act)
        result[("cusip_shares", e["cusip"], e["sec_kind"])] = f"{e['shares']},{e['prev_shares']}"
    return result


# ---------------------------------------------------------------------------
# Core: build formatted report (#1)
# ---------------------------------------------------------------------------
def _trend_prefix_for(cur_action: str, prior_act: str, prev_quarter_label: str) -> str:
    if not prior_act or not prev_quarter_label:
        return ""
    if cur_action in ("加仓", "大幅加仓") and prior_act in ("建仓", "加仓", "大幅加仓"):
        prior_label = "建仓" if prior_act == "建仓" else "加仓"
        return f"继{prev_quarter_label}{prior_label}后，继续"
    if cur_action == "减仓" and prior_act == "减仓":
        return f"继{prev_quarter_label}减仓后，继续"
    return ""


def _prepare_fund_items(
    comparison_rows: list,
    exclude_biotech: bool = True,
    auto_learn: bool = True,
    prior_actions=None,
    prev_quarter_label: str = "",
    cusip_ticker_map: dict[str, str] | None = None,
    cusip_ledger_map: dict[tuple[str, str], bool] | None = None,
    cusip_reps: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Merge comparison rows into per-security items with classification and actions."""
    cusip_map = cusip_ticker_map if cusip_ticker_map is not None else _resolve_cusip_tickers(comparison_rows)
    ledger_map = (
        cusip_ledger_map
        if cusip_ledger_map is not None
        else _resolve_cusip_ledger_matches(cusip_map, comparison_rows)
    )
    reps = cusip_reps if cusip_reps is not None else _resolve_cusip_representatives(comparison_rows)

    if auto_learn:
        issuer_groups = {}
        for row in comparison_rows:
            ident = _row_identity_keys(row, cusip_map, ledger_map, reps)
            rep_iss = ident.get("rep_issuer") or ident["issuer"]
            if (
                not ident["resolved_t"]
                or not rep_iss
                or ident["sec_kind"] != "equity"
                or _is_umbrella_issuer(rep_iss.upper().strip())
            ):
                continue
            if "class" in rep_iss.lower():
                base = re.sub(r"\bclass\s+[a-z0-9]+\b", "", rep_iss, flags=re.IGNORECASE)
                base = re.sub(r"\s+", " ", base).strip()
                issuer_groups.setdefault(base, set()).add(ident["resolved_t"])

        for base, tickers in issuer_groups.items():
            if len(tickers) >= 2:
                canonical = _short_name(base, "")
                for t in tickers:
                    _learn_merge_rule(t, canonical, source="auto-class-share")

    merged = {}
    for row in comparison_rows:
        ident = _row_identity_keys(row, cusip_map, ledger_map, reps)
        ticker = ident["resolved_t"]
        issuer = ident.get("rep_issuer") or ident["issuer"]

        if exclude_biotech and _is_biotech(ticker or ident["explicit_t"], issuer):
            continue

        bucket_key = ident["bucket_key"]

        if ident["explicit_t"] and ident["ledger_match"] and auto_learn and ident["sec_kind"] == "equity":
            _learn_name_to_ticker(ident["explicit_t"], issuer, ident["short"])

        if bucket_key not in merged:
            merged[bucket_key] = {
                "bucket_key": bucket_key,
                "name": ident["display_name"],
                "action_key": ident["action_key"],
                "ticker": ticker,
                "all_tickers": set(),
                "cusip": ident["cusip"],
                "cusips": set(),
                "issuer": issuer,
                "sec_kind": ident["sec_kind"],
                "shares": 0, "value": 0.0,
                "prev_shares": 0, "prev_value": 0.0,
                "chg_shares": 0,
                "category": _classify(
                    ticker,
                    issuer,
                    auto_learn=auto_learn,
                    allow_issuer_lookup=(bool(ticker) or ident["sec_kind"] == "cb"),
                    cusip=("" if (not ticker and ident["sec_kind"] == "cb") else ident["cusip"]),
                ),
            }

        entry = merged[bucket_key]
        if ident["cusip"]:
            entry["cusips"].add(ident["cusip"])
        if ticker:
            entry["all_tickers"].add(ticker)
        if ident["explicit_t"]:
            entry["all_tickers"].add(ident["explicit_t"])
        shares = _safe_int(row.get("SharesPrnAmount") or row.get("Shares") or row.get("shares") or 0)
        value = _safe_float(row.get("Value") or row.get("value") or 0)
        chg = _safe_int(row.get("ShareChange") or row.get("Chg") or row.get("chg") or row.get("Change") or 0)

        entry["shares"] += shares
        entry["value"] += value
        entry["chg_shares"] += chg

        status = str(row.get("Status") or "").upper().strip()
        row_prev_shares = 0
        if status != "NEW":
            prev_shares_explicit = _safe_int(row.get("PrevShares") or row.get("Prev Shares") or row.get("prev_shares") or 0)
            if prev_shares_explicit > 0:
                row_prev_shares = prev_shares_explicit
            elif shares > 0 or chg != 0:
                row_prev_shares = max(0, shares - chg)
        entry["prev_shares"] += row_prev_shares

        # Estimate missing PrevValue strictly for the current row so sibling rows keep reported values
        prev_val = _safe_float(row.get("PrevValue") or row.get("Prev Value") or row.get("prev_value") or 0)
        if prev_val > 0:
            entry["prev_value"] += prev_val
        elif row_prev_shares > 0 and shares > 0 and status != "NEW":
            entry["prev_value"] += value * (row_prev_shares / max(shares, 1))

    tickers_by_display: dict[str, set[str]] = {}
    for entry in merged.values():
        if entry["ticker"]:
            tickers_by_display.setdefault(entry["name"], set()).add(entry["ticker"])
    for entry in merged.values():
        tk = entry["ticker"]
        if tk and len(tickers_by_display.get(entry["name"], set())) > 1:
            suffix = f" ({tk})"
            if not entry["name"].endswith(suffix):
                entry["name"] = f"{entry['name']}{suffix}"

    categories: dict[str, list[dict[str, Any]]] = {"China Book": [], "AI/Semi/US SaaS": [], "Other US Book": []}
    for entry in merged.values():
        entry["chg_shares"] = entry["shares"] - entry["prev_shares"]
        cat = entry["category"]
        if cat not in categories:
            cat = "Other US Book"
        cur_action = _detect_action(entry["prev_shares"], entry["chg_shares"])
        is_closed = entry["shares"] == 0 and entry["prev_shares"] > 0
        prior_act = ""
        if prior_actions and entry["cusips"]:
            # Single CUSIP matches by CUSIP directly; multi-CUSIP merged items recompute prior action from summed shares
            sorted_cusips = sorted(entry["cusips"])
            if len(sorted_cusips) == 1:
                c = sorted_cusips[0]
                for key in (("cusip", c, entry["sec_kind"]), ("cusip", c)):
                    prior_act = prior_actions.get(key, "")
                    if not prior_act and isinstance(key, tuple):
                        prior_act = prior_actions.get("|".join(str(x) for x in key), "")
                    if prior_act:
                        break
            else:
                sum_prev_cur = 0
                sum_prev_prev = 0
                found_shares = False
                for c in sorted_cusips:
                    raw_sp = prior_actions.get(("cusip_shares", c, entry["sec_kind"]), "")
                    if not raw_sp:
                        raw_sp = prior_actions.get(f"cusip_shares|{c}|{entry['sec_kind']}", "")
                    if raw_sp and "," in str(raw_sp):
                        s_str, p_str = str(raw_sp).split(",", 1)
                        sum_prev_cur += int(s_str)
                        sum_prev_prev += int(p_str)
                        found_shares = True
                if found_shares:
                    prior_act = _detect_action(sum_prev_prev, sum_prev_cur - sum_prev_prev)
        trend_prefix = _trend_prefix_for(cur_action, prior_act, prev_quarter_label)
        base_text = _change_template(
            entry["name"], entry["value"],
            entry["prev_shares"], entry["chg_shares"],
            entry["prev_value"],
        )
        categories[cat].append({
            "bucket_key": entry["bucket_key"],
            "action_key": entry["action_key"],
            "text": trend_prefix + base_text,
            "trend_prefix": trend_prefix,
            "action": cur_action,
            "value": entry["value"],
            "prev_value": entry["prev_value"],
            "is_closed": is_closed,
            "name": entry["name"],
            "ticker": entry["ticker"],
            "all_tickers": sorted(entry["all_tickers"]),
            "cusip": entry["cusip"],
            "cusips": sorted(entry["cusips"]),
            "issuer": entry["issuer"],
            "sec_kind": entry["sec_kind"],
            "category": cat,
            "shares": entry["shares"],
            "prev_shares": entry["prev_shares"],
            "chg_shares": entry["chg_shares"],
        })

    return {
        "categories": categories,
        "merged_count": len(merged),
        "filtered_biotech": len(comparison_rows) - len(merged) if exclude_biotech else 0,
    }


def _build_report(comparison_rows: list, exclude_biotech: bool = True, auto_learn: bool = True,
                  prior_actions=None, prev_quarter_label: str = "",
                  cusip_ticker_map: dict[str, str] | None = None,
                  cusip_ledger_map: dict[tuple[str, str], bool] | None = None,
                  cusip_reps: dict[str, tuple[str, str]] | None = None) -> dict:
    """Process comparison rows into categorized, formatted report."""
    prepared = _prepare_fund_items(
        comparison_rows,
        exclude_biotech=exclude_biotech,
        auto_learn=auto_learn,
        prior_actions=prior_actions,
        prev_quarter_label=prev_quarter_label,
        cusip_ticker_map=cusip_ticker_map,
        cusip_ledger_map=cusip_ledger_map,
        cusip_reps=cusip_reps,
    )
    categories = prepared["categories"]

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
        "filtered_biotech": prepared["filtered_biotech"],
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


def _comparison_previous_period(
    comparison: Any,
    thirteenf: Any = None,
    filings: Any = None,
    offset: int = 0,
) -> str:
    """Resolve the actual previous filing report period used by compare_holdings()."""
    if comparison is not None:
        raw_prev = str(getattr(comparison, "previous_period", "") or "").strip()
        if raw_prev:
            return raw_prev
    prev_hr_fn = getattr(thirteenf, "previous_holding_report", None)
    if callable(prev_hr_fn):
        try:
            prev_hr = prev_hr_fn()
            rp = str(getattr(prev_hr, "report_period", "") or "").strip()
            if rp:
                return rp
        except Exception:
            pass
    cur_rp = str(getattr(thirteenf, "report_period", "") or "").strip()
    if filings is not None and offset + 1 < len(filings):
        try:
            nxt = _retry(lambda: filings[offset + 1].obj())
            cand = str(getattr(nxt, "report_period", "") or "").strip()
            if cand and (not cur_rp or cand < cur_rp):
                return cand
        except Exception:
            pass
    return ""


_MERGE_ENTITY_ALIASES = {
    "evergreen": "0002016939",
    "evergreen quality fund": "0002016939",
    "evergreen quality fund gp": "0002016939",
    "evergreen quality fund gp ltd": "0002016939",
}


def _resolve_merge_ref_cik(ref: str) -> str:
    """Resolve a merge target / merged_with reference to a canonical 10-digit CIK if known."""
    raw = str(ref or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low in _MERGE_ENTITY_ALIASES:
        return _norm_cik(_MERGE_ENTITY_ALIASES[low])
    inst_map_lower = {str(k).lower(): str(v) for k, v in _INSTITUTION_MAP.items() if isinstance(v, str)}
    if low in inst_map_lower:
        return _norm_cik(inst_map_lower[low])
    if re.fullmatch(r"(?:CIK\s*)?0*\d+", raw, flags=re.IGNORECASE):
        return _norm_cik(raw)
    return ""


def _table_to_records(tbl: Any) -> list[dict]:
    if tbl is None:
        return []
    if isinstance(tbl, list):
        return [dict(r) for r in tbl if isinstance(r, dict)]
    if isinstance(tbl, pd.DataFrame):
        return tbl.to_dict(orient="records") if not tbl.empty else []
    if hasattr(tbl, "to_pandas"):
        df = tbl.to_pandas()
        return df.to_dict(orient="records") if isinstance(df, pd.DataFrame) and not df.empty else []
    return []


_MISSING_ATTR = object()


def _aggregate_period_holdings_by_cusip(records: list[dict]) -> dict[str, dict[str, Any]]:
    """Aggregate one period's holdings into 1 summed row per CUSIP; ignore zero-share rows when picking rep if shares > 0 exist."""
    rep_meta: dict[str, tuple[tuple[int, int, str, str], dict[str, str]]] = {}
    totals: dict[str, dict[str, float]] = {}
    has_pos_shares: set[str] = set()

    for r in records:
        cusip_norm = str(r.get("Cusip") or r.get("cusip") or "").strip().upper()
        clean_t = _clean_ticker(r.get("Ticker") if r.get("Ticker") is not None else r.get("ticker"))
        issuer = str(r.get("Issuer") or r.get("issuer") or "").strip()
        key = cusip_norm or f"{clean_t}|{issuer}"
        shares = _num(r.get("SharesPrnAmount") if r.get("SharesPrnAmount") is not None else (r.get("Shares") if r.get("Shares") is not None else r.get("shares")))
        if shares > 0:
            has_pos_shares.add(key)

    for r in records:
        cusip_norm = str(r.get("Cusip") or r.get("cusip") or "").strip().upper()
        clean_t = _clean_ticker(r.get("Ticker") if r.get("Ticker") is not None else r.get("ticker"))
        issuer = str(r.get("Issuer") or r.get("issuer") or "").strip()
        cls_name = str(r.get("Class") or r.get("TitleOfClass") or r.get("title_of_class") or "").strip()
        put_call = str(r.get("PutCall") or r.get("put_call") or "").strip()
        key = cusip_norm or f"{clean_t}|{issuer}"
        shares = _num(r.get("SharesPrnAmount") if r.get("SharesPrnAmount") is not None else (r.get("Shares") if r.get("Shares") is not None else r.get("shares")))
        value = _num(r.get("Value") if r.get("Value") is not None else r.get("value"))

        if not (key in has_pos_shares and shares <= 0):
            rank = (1 if put_call else 0, 0 if clean_t else 1, issuer.upper(), cls_name.upper())
            if key not in rep_meta or rank < rep_meta[key][0]:
                rep_meta[key] = (rank, {
                    "cusip": cusip_norm or str(r.get("Cusip") or r.get("cusip") or "").strip(),
                    "ticker": clean_t,
                    "issuer": issuer,
                    "cls": cls_name,
                })
            elif not rep_meta[key][1]["ticker"] and clean_t:
                rep_meta[key][1]["ticker"] = clean_t

        if key not in totals:
            totals[key] = {"shares": 0.0, "value": 0.0}
        totals[key]["shares"] += shares
        totals[key]["value"] += value

    out: dict[str, dict[str, Any]] = {}
    for key, (_rk, meta) in rep_meta.items():
        out[key] = {**meta, **totals[key]}
    return out


def _align_period_holdings(cur_recs: list[dict], prev_recs: list[dict]) -> list[dict]:
    """Sum cur and prev holdings independently per CUSIP, then align 1-to-1 to prevent cartesian double-counting."""
    cur_map = _aggregate_period_holdings_by_cusip(cur_recs)
    prev_map = _aggregate_period_holdings_by_cusip(prev_recs)
    nan = float("nan")
    aligned: list[dict] = []
    for key in sorted(cur_map.keys() | prev_map.keys()):
        c = cur_map.get(key)
        p = prev_map.get(key)
        if c and not p:
            aligned.append({
                "Cusip": c["cusip"], "Ticker": c["ticker"], "Issuer": c["issuer"], "Class": c["cls"],
                "Shares": c["shares"], "Value": c["value"],
                "PrevShares": nan, "PrevValue": nan, "ShareChange": nan, "ShareChangePct": nan,
                "ValueChange": nan, "ValueChangePct": nan, "Status": "NEW",
            })
        elif p and not c:
            aligned.append({
                "Cusip": p["cusip"], "Ticker": p["ticker"], "Issuer": p["issuer"], "Class": p["cls"],
                "Shares": nan, "Value": nan,
                "PrevShares": p["shares"], "PrevValue": p["value"],
                "ShareChange": -p["shares"], "ShareChangePct": -100.0 if p["shares"] else nan,
                "ValueChange": -p["value"], "ValueChangePct": -100.0 if p["value"] else nan,
                "Status": "CLOSED",
            })
        elif c and p:
            chg_s = c["shares"] - p["shares"]
            chg_v = c["value"] - p["value"]
            aligned.append({
                "Cusip": c["cusip"], "Ticker": c["ticker"], "Issuer": c["issuer"], "Class": c["cls"],
                "Shares": c["shares"], "Value": c["value"],
                "PrevShares": p["shares"], "PrevValue": p["value"],
                "ShareChange": chg_s,
                "ShareChangePct": (chg_s / p["shares"] * 100.0) if p["shares"] else nan,
                "ValueChange": chg_v,
                "ValueChangePct": (chg_v / p["value"] * 100.0) if p["value"] else nan,
                "Status": "INCREASED" if chg_s > 0 else "DECREASED" if chg_s < 0 else "UNCHANGED",
            })
    return aligned


def _extract_period_comparison(
    thirteenf: Any,
    filings: Any = None,
    offset: int = 0,
) -> tuple[bool, list[dict], str, Any]:
    """Extract period comparison without edgartools CUSIP cartesian join inflation.

    Returns (has_comparison, rows, previous_period, raw_comparison_obj).
    """
    prev_fn = getattr(thirteenf, "previous_holding_report", None)
    if callable(prev_fn) and inspect.getattr_static(thirteenf, "holdings", _MISSING_ATTR) is not _MISSING_ATTR:
        cur_holdings = _retry(lambda: getattr(thirteenf, "holdings", None))
        if cur_holdings is None:
            return False, [], "", None
        prev_tf = _retry(lambda: prev_fn())
        if prev_tf is None:
            return False, [], "", None
        cur_recs = _table_to_records(cur_holdings)
        prev_holdings = _retry(lambda: getattr(prev_tf, "holdings", None))
        prev_recs = _table_to_records(prev_holdings)
        if not cur_recs or not prev_recs:
            return False, [], "", None
        prev_p = str(getattr(prev_tf, "report_period", "") or "").strip()
        return True, _align_period_holdings(cur_recs, prev_recs), prev_p, None

    comp = _retry(lambda: thirteenf.compare_holdings())
    if comp is None:
        return False, [], "", None
    prev_p = _comparison_previous_period(comp, thirteenf, filings, offset)
    return True, _rows_from_comparison(comp), prev_p, comp


def _compare_rows_for_period(institution: str, period: str, auto_learn: bool) -> tuple:
    """取另一主体同一报告期的 compare rows；找不到同期申报或对比为空时返回 has_comp=False。"""
    company = _resolve_institution(institution, auto_learn=auto_learn)
    comp_cik = _norm_cik(getattr(company, "cik", "") or "")
    filings = company.get_filings(form="13F-HR")
    for i in range(min(len(filings), 8)):
        try:
            tf = _retry(lambda i=i: filings[i].obj())
        except Exception:
            continue
        if str(tf.report_period) == period:
            has_comp, rows, prev_p, _comp = _extract_period_comparison(tf, filings, i)
            return (
                has_comp,
                rows if has_comp else [],
                tf.management_company_name,
                str(tf.report_period),
                prev_p,
                comp_cik,
            )
    return False, [], None, "", "", comp_cik


def _num(v) -> float:
    try:
        f = float(v)
        return 0.0 if f != f else f
    except (TypeError, ValueError):
        return 0.0


def _merge_rows_by_cusip(*row_sets) -> list:
    """按大写 CUSIP 先选代表行（仅由实际持仓行决定代码与简称），再加总 Shares/Value/PrevShares/PrevValue。"""
    rep_rows: dict[str, tuple[tuple[int, int, int, str, str], dict, str]] = {}
    active_ticker_cands: dict[str, list[tuple[tuple[int, int, str], str]]] = {}
    fallback_ticker_cands: dict[str, list[tuple[tuple[int, int, str], str]]] = {}
    active_keys: set[str] = set()
    raw_entries: list[tuple[str, str, dict]] = []

    for rep_period, rows in _iter_period_rows(*row_sets):
        p_rank = _period_sort_key(rep_period)
        for r in rows:
            cusip_norm = str(r.get("Cusip") or r.get("cusip") or "").strip().upper()
            r_clean_t = _clean_ticker(r.get("Ticker") if r.get("Ticker") is not None else r.get("ticker"))
            issuer = str(r.get("Issuer") or r.get("issuer") or "").strip()
            title = str(r.get("TitleOfClass") or r.get("Class") or r.get("title_of_class") or "").strip()
            key = cusip_norm or f"{r_clean_t}|{issuer}"
            is_active = _row_is_active_holding(r)
            if is_active:
                active_keys.add(key)
            row_p = str(r.get("ReportPeriod") or r.get("report_period") or "").strip() or rep_period
            eff_p = _period_sort_key(row_p) if row_p else p_rank
            rank = (0 if is_active else 1, -eff_p, 0 if issuer else 1, issuer.upper(), title.upper())
            if key not in rep_rows or rank < rep_rows[key][0]:
                rep_rows[key] = (rank, r, row_p)
            if r_clean_t:
                t_rank = (-eff_p, 0 if r_clean_t in _VERIFIED_TICKERS else 1, r_clean_t)
                if is_active:
                    active_ticker_cands.setdefault(key, []).append((t_rank, r_clean_t))
                else:
                    fallback_ticker_cands.setdefault(key, []).append((t_rank, r_clean_t))
            raw_entries.append((key, cusip_norm, r))

    merged: dict = {}
    for key, (_rk, rep_r, rep_p) in rep_rows.items():
        cusip_norm = str(rep_r.get("Cusip") or rep_r.get("cusip") or "").strip().upper()
        cands = active_ticker_cands.get(key)
        if not cands and key not in active_keys:
            cands = fallback_ticker_cands.get(key)
        chosen_t = min(cands, key=lambda x: x[0])[1] if cands else ""
        base_entry = {
            **rep_r,
            "Cusip": cusip_norm or str(rep_r.get("Cusip") or rep_r.get("cusip") or "").strip(),
            "Ticker": chosen_t,
            "Shares": 0.0,
            "Value": 0.0,
            "PrevShares": 0.0,
            "PrevValue": 0.0,
        }
        if rep_p:
            base_entry["ReportPeriod"] = rep_p
        merged[key] = base_entry

    for key, _cusip_norm, r in raw_entries:
        m = merged[key]
        status = str(r.get("Status") or "").upper().strip()
        cur_s = _num(r.get("Shares") if r.get("Shares") is not None else r.get("SharesPrnAmount"))
        chg_s = _num(
            r.get("ShareChange")
            if r.get("ShareChange") is not None
            else (r.get("Chg") if r.get("Chg") is not None else r.get("Change"))
        )
        prev_explicit = _num(
            r.get("PrevShares") if r.get("PrevShares") is not None else r.get("Prev Shares")
        )
        if status == "NEW":
            prev_s = 0.0
        elif prev_explicit > 0:
            prev_s = prev_explicit
        elif cur_s > 0 or chg_s != 0:
            prev_s = max(0.0, cur_s - chg_s)
        else:
            prev_s = 0.0
        m["Shares"] += cur_s
        m["PrevShares"] += prev_s
        m["Value"] += _num(r.get("Value"))
        m["PrevValue"] += _num(
            r.get("PrevValue") if r.get("PrevValue") is not None else r.get("Prev Value")
        )
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
        raw_targets = explicit
    else:
        cik = str(getattr(company, "cik", "") or "").lstrip("0")
        raw_targets = list(_DEFAULT_MERGES.get(cik, []))
    deduped: list[str] = []
    seen_ciks: set[str] = set()
    primary_cik = _norm_cik(getattr(company, "cik", "") or "")
    if primary_cik:
        seen_ciks.add(primary_cik)
    for t in raw_targets:
        c = _resolve_merge_ref_cik(t)
        key = c or t.lower()
        if key in seen_ciks:
            continue
        seen_ciks.add(key)
        # Always query by resolved canonical CIK so alias order never changes resolution
        deduped.append(c or t)
    return deduped


def cmd_compare(args):
    configure_identity()
    auto_learn = _auto_learn(args)
    company = _resolve_institution(args.institution, auto_learn=auto_learn)
    offset = _get_offset(args, company)
    thirteenf = _retry(lambda: company.get_filings(form="13F-HR")[offset].obj())
    try:
        main_filings = company.get_filings(form="13F-HR")
    except Exception:
        main_filings = None
    main_has_comp, rows, main_prev_period, comparison = _extract_period_comparison(thirteenf, main_filings, offset)
    cur_period_str = str(thirteenf.report_period)
    for r in rows:
        if not str(r.get("ReportPeriod") or r.get("report_period") or "").strip():
            r["ReportPeriod"] = cur_period_str

    raw_cik = str(getattr(company, "cik", "") or "").strip()
    digits_cik = "".join(ch for ch in raw_cik if ch.isdigit())
    norm_cik = (digits_cik.lstrip("0") or digits_cik).zfill(10) if digits_cik else ""

    merge_targets = _merge_targets(args, company)
    merged_with = []
    row_sets: list[Any] = [(cur_period_str, rows)]
    seen_merge_ciks: set[str] = {norm_cik} if norm_cik else set()
    merge_errors: list[str] = []
    if merge_targets:
        if not main_has_comp or not rows:
            merge_errors.append(f"主申报主体 {args.institution} 缺少 {cur_period_str} 报告期的持仓对比结果")
        elif not main_prev_period:
            merge_errors.append(f"主申报主体 {args.institution} 缺少上期报告期（previous_period 为空）")

    for other in merge_targets:
        other_has_comp, other_rows, other_name, other_base_period, other_prev_period, other_cik = _compare_rows_for_period(
            other, cur_period_str, auto_learn
        )
        dedup_k = other_cik or _resolve_merge_ref_cik(other) or other.strip().lower()
        if dedup_k in seen_merge_ciks:
            continue
        seen_merge_ciks.add(dedup_k)

        period_mismatch = False
        note = None
        if not other_has_comp or not other_rows:
            note = f"该主体没有 {cur_period_str} 报告期的 13F 对比结果，未合并"
            merge_errors.append(f"合并主体 {other}：{note}")
        elif other_base_period != cur_period_str:
            period_mismatch = True
            note = f"本期报告期不一致：{other_base_period} != {cur_period_str}"
            merge_errors.append(f"合并主体 {other}：{note}")
        elif not other_prev_period:
            period_mismatch = True
            note = "上期报告期为空，未合并"
            merge_errors.append(f"合并主体 {other}：{note}")
        elif main_prev_period and other_prev_period != main_prev_period:
            period_mismatch = True
            note = f"上期报告期不一致：{other_prev_period} != {main_prev_period}"
            merge_errors.append(f"合并主体 {other}：{note}")
        merged_with.append({
            "institution": other,
            "cik": other_cik,
            "manager": other_name,
            "base_period": other_base_period,
            "previous_period": other_prev_period,
            "rows": 0 if (period_mismatch or not other_has_comp) else len(other_rows),
            "note": note,
        })
        if other_has_comp and other_rows and not period_mismatch:
            for r in other_rows:
                if not str(r.get("ReportPeriod") or r.get("report_period") or "").strip():
                    r["ReportPeriod"] = other_base_period
            row_sets.append((other_base_period, other_rows))

    if merge_errors:
        _emit({
            "ok": False,
            "error": "；".join(merge_errors),
            "institution": args.institution,
            "cik": norm_cik,
            "offset": offset,
            "merged_with": merged_with,
            "manager": thirteenf.management_company_name,
            "base_period": cur_period_str,
            "previous_period": main_prev_period,
        }, _as_json(args))
        return

    if any(rs for _, rs in row_sets):
        rows = _merge_rows_by_cusip(*row_sets)
    for r in rows:
        if not str(r.get("ReportPeriod") or r.get("report_period") or "").strip():
            r["ReportPeriod"] = cur_period_str

    result = {
        "ok": True,
        "institution": args.institution,
        "cik": norm_cik,
        "offset": offset,
        "merged_with": merged_with,
        "manager": thirteenf.management_company_name,
        "base_period": cur_period_str,
        "previous_period": main_prev_period,
        "auto_learn": auto_learn,
        "rows": rows,
        "raw_text": str(comparison) if comparison is not None else "",
    }

    # Fetch prior quarter comparison ONLY for exact main_prev_period (never fall back to earlier quarters).
    prev_rows: list = []
    prior_actions = {}
    prev_quarter_label = ""
    if main_prev_period:
        try:
            filings = main_filings if main_filings is not None else company.get_filings(form="13F-HR")
            prev_offset = offset + 1
            scanned = 0
            while prev_offset < len(filings) and scanned < 5:
                scanned += 1
                try:
                    prev_thirteenf = _retry(lambda po=prev_offset: filings[po].obj())
                except Exception:
                    prev_offset += 1
                    continue
                cand_p = str(prev_thirteenf.report_period)
                if cand_p != main_prev_period:
                    prev_offset += 1
                    continue
                prev_has_comp, main_prev_rows, prev_prev_period, _prev_cmp = _extract_period_comparison(
                    prev_thirteenf, filings, prev_offset
                )
                if not prev_has_comp:
                    break
                for r in main_prev_rows:
                    if not str(r.get("ReportPeriod") or r.get("report_period") or "").strip():
                        r["ReportPeriod"] = cand_p
                prev_row_sets: list[Any] = [(cand_p, main_prev_rows)]
                seen_prev_ciks: set[str] = {norm_cik} if norm_cik else set()
                prev_merge_complete = True
                if merge_targets and not prev_prev_period:
                    prev_merge_complete = False
                for other in merge_targets:
                    if not prev_merge_complete:
                        break
                    other_prev_has_comp, other_prev, _onm, other_prev_base, other_prev_prev, other_prev_cik = _compare_rows_for_period(
                        other, cand_p, auto_learn
                    )
                    pk = other_prev_cik or _resolve_merge_ref_cik(other) or other.strip().lower()
                    if pk in seen_prev_ciks:
                        continue
                    seen_prev_ciks.add(pk)
                    if (
                        not other_prev_has_comp
                        or not other_prev
                        or other_prev_base != cand_p
                        or not other_prev_prev
                        or other_prev_prev != prev_prev_period
                    ):
                        prev_merge_complete = False
                        break
                    for r in other_prev:
                        if not str(r.get("ReportPeriod") or r.get("report_period") or "").strip():
                            r["ReportPeriod"] = cand_p
                    prev_row_sets.append((cand_p, other_prev))
                if prev_merge_complete and any(rs for _, rs in prev_row_sets):
                    prev_quarter_label = _quarter_label(cand_p)
                    prev_rows = _merge_rows_by_cusip(*prev_row_sets)
                else:
                    prev_quarter_label = ""
                    prev_rows = []
                break
        except Exception:
            pass

    cur_period_rows = (cur_period_str, rows)
    prev_period_rows = (main_prev_period, prev_rows)
    cusip_ticker_map = _resolve_cusip_tickers(cur_period_rows, prev_period_rows)
    cusip_ledger_map = _resolve_cusip_ledger_matches(cusip_ticker_map, cur_period_rows, prev_period_rows)
    cusip_reps = _resolve_cusip_representatives(cur_period_rows, prev_period_rows)
    for r in rows:
        if not _clean_ticker(r.get("Ticker") if r.get("Ticker") is not None else r.get("ticker")):
            cn = str(r.get("Cusip") or r.get("cusip") or "").strip().upper()
            if cn and cusip_ticker_map.get(cn):
                r["Ticker"] = cusip_ticker_map[cn]
    if prev_rows:
        prior_actions = _extract_actions(
            prev_rows,
            exclude_biotech=not args.include_biotech,
            cusip_ticker_map=cusip_ticker_map,
            cusip_ledger_map=cusip_ledger_map,
            cusip_reps=cusip_reps,
        )
    if prev_quarter_label:
        result["prev_quarter_label"] = prev_quarter_label
    if prior_actions:
        result["prior_actions"] = {
            "|".join(str(x) for x in k) if isinstance(k, tuple) else str(k): v
            for k, v in prior_actions.items()
        }

    # Build report (also triggers auto-learn), only expose formatted text in cn mode
    if rows and (args.format == "cn" or auto_learn):
        report = _build_report(
            rows,
            exclude_biotech=not args.include_biotech,
            auto_learn=auto_learn,
            prior_actions=prior_actions,
            prev_quarter_label=prev_quarter_label,
            cusip_ticker_map=cusip_ticker_map,
            cusip_ledger_map=cusip_ledger_map,
            cusip_reps=cusip_reps,
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


# ---------------------------------------------------------------------------
# V2 Batch Aggregation, Five-Section Rendering, Descriptors & Number Audit
# ---------------------------------------------------------------------------
_DEFAULT_GROUPS = [
    {"index": 1, "slug": "hillhouse", "group": "China Book", "display_name": "Hillhouse 高瓴（HHLR + Evergreen 合并）", "cik": "0001762304", "merge_with": ["0002016939"], "aliases": ["HHLR", "Hillhouse", "高瓴"]},
    {"index": 2, "slug": "first_beijing", "group": "China Book", "display_name": "First Beijing Investment", "cik": "0001701717", "merge_with": [], "aliases": ["First Beijing"]},
    {"index": 3, "slug": "aspex", "group": "China Book", "display_name": "Aspex Management", "cik": "0001768375", "merge_with": [], "aliases": ["Aspex"]},
    {"index": 4, "slug": "greenwoods", "group": "China Book", "display_name": "Greenwoods 景林（香港）", "cik": "0001848138", "merge_with": [], "aliases": ["Greenwoods", "景林"]},
    {"index": 5, "slug": "sequoia_china", "group": "China Book", "display_name": "Sequoia China 红杉中国（HCEP）", "cik": "0001822015", "merge_with": [], "aliases": ["Sequoia China", "红杉中国", "HCEP"]},
    {"index": 6, "slug": "capital_today", "group": "China Book", "display_name": "Capital Today 今日资本", "cik": "0001625502", "merge_with": [], "aliases": ["Capital Today", "今日资本"]},
    {"index": 7, "slug": "brilliance", "group": "China Book", "display_name": "Brilliance Asset Management", "cik": "0001665751", "merge_with": [], "aliases": ["Brilliance"]},
    {"index": 8, "slug": "yiheng", "group": "China Book", "display_name": "Yiheng Capital", "cik": "0001447228", "merge_with": [], "aliases": ["Yiheng", "易方", "易方资本"]},
    {"index": 9, "slug": "tiger_global", "group": "US Growth", "display_name": "Tiger Global Management", "cik": "0001167483", "merge_with": [], "aliases": ["Tiger", "Tiger Global"]},
    {"index": 10, "slug": "coatue", "group": "US Growth", "display_name": "Coatue Management", "cik": "0001135730", "merge_with": [], "aliases": ["Coatue"]},
    {"index": 11, "slug": "d1_capital", "group": "US Growth", "display_name": "D1 Capital Partners", "cik": "0001747057", "merge_with": [], "aliases": ["D1", "D1 Capital"]},
    {"index": 12, "slug": "altimeter", "group": "US Growth", "display_name": "Altimeter Capital Management", "cik": "0001541617", "merge_with": [], "aliases": ["Altimeter"]},
    {"index": 13, "slug": "whale_rock", "group": "US Growth", "display_name": "Whale Rock Capital Management", "cik": "0001387322", "merge_with": [], "aliases": ["Whale Rock"]},
    {"index": 14, "slug": "dragoneer", "group": "US Growth", "display_name": "Dragoneer Investment Group", "cik": "0001602189", "merge_with": [], "aliases": ["Dragoneer"]},
    {"index": 15, "slug": "lone_pine", "group": "US Growth", "display_name": "Lone Pine Capital", "cik": "0001061165", "merge_with": [], "aliases": ["Lone Pine"]},
    {"index": 16, "slug": "sequoia_us", "group": "US Growth", "display_name": "Sequoia US 红杉美国（SCGE）", "cik": "0001607841", "merge_with": [], "aliases": ["Sequoia US", "红杉美国", "SCGE"]},
    {"index": 17, "slug": "durable", "group": "US Growth", "display_name": "Durable Capital Partners", "cik": "0001798849", "merge_with": [], "aliases": ["Durable"]},
    {"index": 18, "slug": "berkshire", "group": "Value", "display_name": "Berkshire Hathaway 伯克希尔·哈撒韦", "cik": "0001067983", "merge_with": [], "aliases": ["Berkshire", "伯克希尔"]},
]

_GROUP_SECTION_HEADERS = (
    ("China Book", "## China Book 机构（8 组 / 9 申报主体）"),
    ("US Growth", "## US Growth 机构（9 组）"),
    ("Value", "## Value 机构（1 组）"),
)

_BUILTIN_STOCK_ALIASES: dict[str, list[str]] = {
    "PDD": ["拼多多"],
    "BABA": ["阿里", "阿里巴巴"],
    "VIPS": ["唯品会"],
    "ATAT": ["亚朵"],
    "HTHT": ["华住"],
    "TCOM": ["携程"],
    "FUTU": ["富途"],
    "EDU": ["新东方"],
    "BEKE": ["贝壳"],
    "BZ": ["BOSS直聘", "Boss直聘"],
    "BILI": ["哔哩哔哩", "B站"],
    "GLW": ["康宁"],
    "GOOGL": ["谷歌"],
    "AMZN": ["亚马逊"],
    "NVDA": ["英伟达"],
    "INTC": ["英特尔"],
    "AAPL": ["苹果"],
    "MSFT": ["微软"],
    "TSLA": ["特斯拉"],
    "TSM": ["台积电"],
    "MCO": ["Moody's", "穆迪"],
    "SPGI": ["S&P Global", "标普全球"],
    "LSCC": ["Lattice"],
    "Q": ["Qnity"],
    "MAIR": ["Madison Air"],
    "MPWR": ["MPWR", "Monolithic Power"],
    "TMO": ["Thermo Fisher"],
    "RVTY": ["Revvity"],
    "SGI": ["Somnigroup"],
    "MLM": ["Martin Marietta"],
    "COMP": ["Compass"],
    "VIAV": ["Viavi"],
    "SMTC": ["Semtech"],
    "SITM": ["SiTime"],
    "VICR": ["Vicor"],
    "WAB": ["Wabtec"],
    "BSP": ["Bending Spoons"],
    "SKHY": ["SK hynix"],
    "CBRS": ["Cerebras"],
    "MU": ["Micron"],
    "STX": ["Seagate"],
    "WDC": ["Western Digital"],
}


def _parse_quarter_tag(quarter_str: str) -> tuple[str, str, str, str]:
    """Parse '26Q3' or '2026Q3' -> (short_q, full_q, prev_short_q, expected_ym)."""
    m = re.match(r"^(\d{2,4})[Qq]([1-4])$", (quarter_str or "").strip())
    if not m:
        raise ValueError(f"Invalid quarter format: '{quarter_str}'. Expected e.g. '26Q3' or '2026Q3'.")
    year = int(m.group(1))
    if year < 100:
        year += 2000
    q = int(m.group(2))
    short_q = f"{year % 100:02d}Q{q}"
    full_q = f"{year}Q{q}"
    prev_y, prev_q = (year - 1, 4) if q == 1 else (year, q - 1)
    prev_short_q = f"{prev_y % 100:02d}Q{prev_q}"
    month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    return short_q, full_q, prev_short_q, f"{year}-{month:02d}"


def _batch_groups() -> list[dict[str, Any]]:
    raw = _INSTITUTION_MAP.get("_groups")
    if isinstance(raw, list) and len(raw) == 18:
        return [dict(g) for g in raw]
    return [dict(g) for g in _DEFAULT_GROUPS]


def _deserialize_prior_actions(raw_pa: Any) -> dict[Any, str]:
    if not isinstance(raw_pa, dict):
        return {}
    out: dict[Any, str] = {}
    for k, v in raw_pa.items():
        ks = str(k)
        out[ks] = str(v)
        if "|" in ks:
            out[tuple(ks.split("|"))] = str(v)
    return out


def _norm_cik(raw: Any) -> str:
    s = str(raw or "").strip()
    m = re.fullmatch(r"(?:CIK\s*)?0*(\d+)", s, flags=re.IGNORECASE)
    if m:
        return (m.group(1).lstrip("0") or "0").zfill(10)
    digits = "".join(ch for ch in s if ch.isdigit())
    return (digits.lstrip("0") or "0").zfill(10) if digits else s.lower()


def _resolve_doc_cik(d: dict[str, Any]) -> str:
    raw_cik = str(d.get("cik") or "").strip()
    if raw_cik:
        return _norm_cik(raw_cik)
    inst_map_lower = {str(k).lower(): v for k, v in _INSTITUTION_MAP.items() if isinstance(v, str)}
    raw_inst = str(d.get("institution") or "").strip()
    if raw_inst:
        mapped = inst_map_lower.get(raw_inst.lower())
        if mapped:
            return _norm_cik(mapped)
        if re.fullmatch(r"(?:CIK\s*)?0*\d+", raw_inst, flags=re.IGNORECASE):
            return _norm_cik(raw_inst)
    return ""


def _validate_doc_institution(d: dict[str, Any], g: dict[str, Any]) -> None:
    slug = str(g["slug"])
    expected_cik = _norm_cik(g.get("cik"))
    actual_cik = _resolve_doc_cik(d)
    if expected_cik and actual_cik != expected_cik:
        raise RuntimeError(
            f"Institution CIK mismatch for {slug}: expected '{g.get('cik')}', "
            f"got cik='{d.get('cik')}' institution='{d.get('institution')}'"
        )
    inst_map_lower = {str(k).lower(): v for k, v in _INSTITUTION_MAP.items() if isinstance(v, str)}
    raw_inst = str(d.get("institution") or "").strip()
    if raw_inst:
        mapped = inst_map_lower.get(raw_inst.lower())
        if mapped:
            inst_cik = _norm_cik(mapped)
        elif re.fullmatch(r"(?:CIK\s*)?0*\d+", raw_inst, flags=re.IGNORECASE):
            inst_cik = _norm_cik(raw_inst)
        else:
            inst_cik = ""
        if inst_cik:
            if expected_cik and inst_cik != expected_cik:
                raise RuntimeError(
                    f"Institution field mismatch for {slug}: expected '{g.get('cik')}', got institution='{raw_inst}'"
                )
        else:
            # Primary filer validation uses only slug / primary names / CIK map — never combined display aliases (e.g. Evergreen)
            allowed_primary = {
                slug.lower(),
                slug.replace("_", " ").lower(),
                *(str(n).strip().lower() for n in (g.get("primary_names") or []) if str(n).strip()),
                *([] if g.get("merge_with") else [str(g.get("display_name") or "").strip().lower()]),
            }
            if raw_inst.lower() not in allowed_primary:
                raise RuntimeError(
                    f"Institution field mismatch for {slug}: unrecognized primary institution='{raw_inst}'"
                )


def _load_batch_runs(
    data_dir: Path,
    quarter: str,
    *,
    include_biotech: bool = False,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    short_q, full_q, prev_short_q, expected_ym = _parse_quarter_tag(quarter)
    q_slug = short_q.lower()
    groups = _batch_groups()
    loaded_groups: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []

    for g in groups:
        idx = int(g["index"])
        slug = str(g["slug"])
        fpath = data_dir / f"{idx:02d}_{slug}_{q_slug}.json"
        if not fpath.is_file():
            raise FileNotFoundError(f"Missing group compare file: {fpath}")
        d = json.loads(fpath.read_text(encoding="utf-8"))
        if not d.get("ok"):
            raise RuntimeError(f"Group {slug} failed: {d.get('error')}")
        base_period = str(d.get("base_period") or "").strip()
        if not base_period or not base_period.startswith(expected_ym):
            raise RuntimeError(
                f"Period mismatch for {slug}: base_period='{base_period}' vs quarter '{short_q}' ({expected_ym}-*)"
            )
        doc_prev_period = str(d.get("previous_period") or "").strip()
        if not doc_prev_period:
            raise RuntimeError(f"Missing previous_period for {slug}")
        if not _quarter_label(doc_prev_period):
            raise RuntimeError(f"Invalid previous_period '{doc_prev_period}' for {slug}")
        _validate_doc_institution(d, g)
        expected_merges = [_norm_cik(c) for c in (g.get("merge_with") or []) if str(c).strip()]
        merged_with = d.get("merged_with")
        if expected_merges:
            if not isinstance(merged_with, list) or not merged_with:
                raise RuntimeError(f"Incomplete merge for {slug}: missing merged_with metadata")
            actual_merges: list[str] = []
            for mw in merged_with:
                if not isinstance(mw, dict) or int(mw.get("rows") or 0) <= 0:
                    raise RuntimeError(f"Incomplete merge for {slug}: merged_with entry has rows<=0")
                mw_base = str(mw.get("base_period") or "").strip()
                if not mw_base or mw_base != base_period:
                    raise RuntimeError(
                        f"Merge base_period mismatch for {slug}: '{mw_base}' != '{base_period}'"
                    )
                mw_prev = str(mw.get("previous_period") or "").strip()
                if not mw_prev or mw_prev != doc_prev_period:
                    raise RuntimeError(
                        f"Merge previous_period mismatch for {slug}: '{mw_prev}' != '{doc_prev_period}'"
                    )
                inst_ref = str(mw.get("institution") or "").strip()
                resolved_ref = _resolve_merge_ref_cik(inst_ref) or _norm_cik(inst_ref)
                actual_merges.append(resolved_ref)
            if len(actual_merges) != len(set(actual_merges)):
                raise RuntimeError(f"Duplicate merged_with CIK for {slug}: {actual_merges}")
            if sorted(actual_merges) != sorted(expected_merges):
                raise RuntimeError(
                    f"Merge entity mismatch for {slug}: expected {sorted(expected_merges)}, got {actual_merges}"
                )
        elif merged_with:
            raise RuntimeError(f"Unexpected merged_with for {slug}: expected empty, got {merged_with}")
        loaded_groups.append((g, fpath, d))

    # Build unified CUSIP -> ticker, ledger-match, and representative issuer/title maps across all 18 groups
    period_row_sets = [(str(dd.get("base_period") or ""), dd.get("rows") or []) for _g, _fp, dd in loaded_groups]
    global_cusip_map = _resolve_cusip_tickers(*period_row_sets)
    global_ledger_map = _resolve_cusip_ledger_matches(global_cusip_map, *period_row_sets)
    global_cusip_reps = _resolve_cusip_representatives(*period_row_sets)

    runs: list[dict[str, Any]] = []
    for g, fpath, d in loaded_groups:
        rows = d.get("rows") or []
        base_period = str(d.get("base_period") or "").strip()
        actual_prev_period = str(d.get("previous_period") or "").strip()
        rendered_prev_q = _quarter_label(actual_prev_period)
        total_val = sum(_num(r.get("Value")) for r in rows)
        prev_total_val = sum(_num(r.get("PrevValue")) for r in rows)
        prior_actions = _deserialize_prior_actions(d.get("prior_actions"))
        trend_prev_q = str(d.get("prev_quarter_label") or "").strip() or rendered_prev_q
        if trend_prev_q != rendered_prev_q:
            # The prior actions must come from the quarter this comparison
            # actually starts from; any other quarter gets no trend prefix.
            prior_actions = {}
        prepared = _prepare_fund_items(
            rows,
            exclude_biotech=not include_biotech,
            auto_learn=False,
            prior_actions=prior_actions,
            prev_quarter_label=trend_prev_q,
            cusip_ticker_map=global_cusip_map,
            cusip_ledger_map=global_ledger_map,
            cusip_reps=global_cusip_reps,
        )

        runs.append({
            **g,
            "file": str(fpath),
            "base_period": base_period,
            "previous_period": actual_prev_period,
            "cur_quarter": _quarter_label(base_period) or short_q,
            "prev_quarter": rendered_prev_q,
            "total_value_usd": total_val,
            "prev_total_value_usd": prev_total_val,
            "qoq_pct": ((total_val - prev_total_val) / prev_total_val * 100.0) if prev_total_val > 0 else None,
            "categories": prepared["categories"],
            "filtered_biotech": prepared["filtered_biotech"],
        })

    return short_q, full_q, prev_short_q, runs


def _canonical_group_for_item(item: dict[str, Any]) -> str:
    bk = item.get("bucket_key")
    if isinstance(bk, (tuple, list)) and len(bk) >= 2 and bk[0] == "canon":
        return str(bk[1])
    tk = _clean_ticker(item.get("ticker"))
    if tk:
        cg = str(_MERGE_RULES.get(tk) or "").strip()
        if cg and not cg.startswith("_"):
            return cg
    return ""


def _entity_tickers(item: dict[str, Any]) -> set[str]:
    tickers: set[str] = set()
    for t in [item.get("ticker"), *(item.get("all_tickers") or [])]:
        ct = _clean_ticker(t)
        if ct:
            tickers.add(ct)
            res = _resolve_alias_safely(ct)
            if res:
                tickers.add(res)
    canon = _canonical_group_for_item(item)
    if canon:
        for k, v in _MERGE_RULES.items():
            if v == canon and not k.startswith("_"):
                tickers.add(_clean_ticker(k))
    return {t for t in tickers if t}


def _is_major_bluechip(item: dict[str, Any]) -> bool:
    return bool(_entity_tickers(item) & _MAJOR_BLUECHIPS)


def _is_familiar(item: dict[str, Any]) -> bool:
    return bool(_entity_tickers(item) & (_MAJOR_BLUECHIPS | _FAMILIAR_NAMES))


def _lookup_descriptor(item: dict[str, Any]) -> str:
    tk = _clean_ticker(item.get("ticker"))
    ordered = [tk] if tk else []
    ordered.extend(sorted(_entity_tickers(item) - {tk}))
    for cand in ordered:
        if cand in _DESCRIPTOR_MAP:
            return _DESCRIPTOR_MAP[cand]
    return ""


def _entity_dedup_key(item: dict[str, Any]) -> str:
    canon = _canonical_group_for_item(item)
    if canon:
        return f"canon:{canon}"
    tk = _clean_ticker(item.get("ticker"))
    if tk:
        return f"ticker:{_resolve_alias_safely(tk) or tk}"
    return f"name:{item.get('name', '')}"


def _entity_cross_fund_key(item: dict[str, Any]) -> str:
    bk = item.get("bucket_key")
    if isinstance(bk, (tuple, list)):
        return "::".join(str(x) for x in bk)
    return _entity_dedup_key(item)


def _load_coverage_roster(
    workspace_dir: Path | None,
    coverage_path: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Read user Dalton portfolio & coverage list; never silently fall back to repo defaults."""
    candidates: list[Path] = []
    if coverage_path:
        candidates.append(Path(coverage_path))
    else:
        if workspace_dir is not None:
            candidates.extend([
                workspace_dir / "dalton" / "coverage.yaml",
                workspace_dir / "state" / "dalton-13f-tracker" / "coverage.yaml",
                workspace_dir / "state" / "dalton-13f-tracker" / "portfolio_coverage.json",
                workspace_dir.parent / "Home" / "dalton" / "coverage.yaml",
            ])
        candidates.extend([
            Path("dalton/coverage.yaml"),
            Path("../Home/dalton/coverage.yaml"),
        ])

    cov_file = next((p for p in candidates if p.is_file()), None)
    if cov_file is None:
        searched = ", ".join(str(p) for p in candidates)
        return [], None, f"未找到用户实盘与覆盖名单文件（已查找：{searched}）；请通过 dalton_coverage 或 Home/dalton/coverage.yaml 导出后用 --coverage 传入"

    try:
        text = cov_file.read_text(encoding="utf-8")
        import yaml  # type: ignore

        try:
            data = yaml.safe_load(text)
        except Exception:
            data = json.loads(text)
    except Exception as exc:
        return [], str(cov_file), f"覆盖名单读取或解析失败（{type(exc).__name__}）：{cov_file} ({exc})"

    if not isinstance(data, dict) or not isinstance(data.get("companies"), list):
        return [], str(cov_file), f"覆盖名单解析失败或缺少 companies 列表：{cov_file}"

    raw_companies = data["companies"]
    roster: list[dict[str, Any]] = []
    for c in raw_companies:
        if not isinstance(c, dict):
            return [], str(cov_file), f"覆盖名单存在非对象条目：{cov_file}"
        raw_t = c.get("ticker")
        if not isinstance(raw_t, str) or not raw_t.strip():
            return [], str(cov_file), f"覆盖名单条目缺少有效的字符串 ticker：{cov_file}"
        t = raw_t.strip().upper()
        raw_name = c.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            return [], str(cov_file), f"覆盖名单 {t} 的 name 字段必须为字符串：{cov_file}"
        raw_sec = c.get("sheet_section")
        if raw_sec is not None and not isinstance(raw_sec, str):
            return [], str(cov_file), f"覆盖名单 {t} 的 sheet_section 字段必须为字符串：{cov_file}"
        raw_tier = c.get("tier")
        if raw_tier is not None and not isinstance(raw_tier, str):
            return [], str(cov_file), f"覆盖名单 {t} 的 tier 字段必须为字符串：{cov_file}"
        raw_held = c.get("held")
        if raw_held is not None and not isinstance(raw_held, bool):
            return [], str(cov_file), f"覆盖名单 {t} 的 held 字段必须为布尔值：{cov_file}"
        raw_aliases = c.get("aliases")
        if raw_aliases is not None and not isinstance(raw_aliases, dict):
            return [], str(cov_file), f"覆盖名单 {t} 的 aliases 字段必须为对象：{cov_file}"
        aliases_obj = raw_aliases or {}
        raw_sn = aliases_obj.get("search_names")
        if raw_sn is not None and (not isinstance(raw_sn, list) or any(not isinstance(x, str) for x in raw_sn)):
            return [], str(cov_file), f"覆盖名单 {t} 的 aliases.search_names 字段必须为字符串列表：{cov_file}"
        raw_adr = aliases_obj.get("adr")
        if raw_adr is not None and not isinstance(raw_adr, str):
            return [], str(cov_file), f"覆盖名单 {t} 的 aliases.adr 字段必须为字符串：{cov_file}"

        sec = (raw_sec or "").strip()
        tier = (raw_tier or "").strip().lower()
        is_live = bool(raw_held) or sec == "Live Position"
        is_core = tier == "core"
        is_watch = tier == "watch"
        search_names = [x.strip() for x in (raw_sn or []) if x.strip()]
        adr = (raw_adr or "").strip().upper()
        match_tickers = {t, t.replace("-", ".")}
        if adr:
            match_tickers.add(adr)
        roster.append({
            "ticker": t,
            "match_tickers": sorted(match_tickers),
            "name": (raw_name or t).strip(),
            "tier": tier,
            "sheet_section": sec,
            "is_live_position": is_live,
            "is_core_coverage": is_core,
            "is_watch": is_watch,
            "search_names": search_names,
        })
    return roster, str(cov_file), None


def _compute_aggregate_payload(
    *,
    data_dir: Path,
    quarter: str,
    workspace_dir: Path | None = None,
    coverage_path: str | None = None,
    include_biotech: bool = False,
) -> dict[str, Any]:
    short_q, full_q, prev_short_q, runs = _load_batch_runs(
        data_dir, quarter, include_biotech=include_biotech
    )

    entities: dict[str, dict[str, Any]] = {}
    fund_summaries: list[dict[str, Any]] = []
    fund_large_sells: dict[str, list[dict[str, Any]]] = {}

    for run in runs:
        f_total = run["total_value_usd"]
        f_prev_total = run["prev_total_value_usd"]
        f_slug = run["slug"]
        large_sells: list[dict[str, Any]] = []

        for cat_name in ("China Book", "AI/Semi/US SaaS", "Other US Book"):
            for item in run["categories"].get(cat_name, []):
                shares = int(item["shares"])
                prev_shares = int(item["prev_shares"])
                chg_shares = int(item["chg_shares"])
                val = float(item["value"])
                prev_val = float(item["prev_value"])
                is_closed = bool(item["is_closed"])

                val_dec = Decimal(str(val)) if val > 0 else Decimal("0")
                prev_val_dec = Decimal(str(prev_val)) if prev_val > 0 else Decimal("0")
                f_total_dec = Decimal(str(f_total)) if f_total > 0 else Decimal("0")
                weight_pct = (val / f_total * 100.0) if (f_total > 0 and shares > 0) else 0.0
                pct_change = (chg_shares / prev_shares * 100.0) if prev_shares > 0 else None
                # Net buy/sell MUST use exact share delta * value / shares to avoid float boundary drift
                if prev_shares == 0 and shares > 0:
                    net_buy_dec = val_dec
                elif shares > 0:
                    net_buy_dec = (val_dec * Decimal(chg_shares)) / Decimal(shares)
                elif is_closed:
                    net_buy_dec = -prev_val_dec
                else:
                    net_buy_dec = Decimal("0")
                net_buy_usd = float(net_buy_dec)
                net_multiple = round(chg_shares / prev_shares, 2) if prev_shares > 0 else None

                holder_rec = {
                    "fund_index": run["index"],
                    "fund_slug": f_slug,
                    "fund_name": run["display_name"],
                    "fund_group": run["group"],
                    "action": item["action"],
                    "trend_prefix": item["trend_prefix"],
                    "is_closed": is_closed,
                    "shares": shares,
                    "prev_shares": prev_shares,
                    "chg_shares": chg_shares,
                    "end_value_usd": round(val, 2),
                    "end_value_fmt": _format_value(val) if shares > 0 else "0万美元",
                    "prev_value_usd": round(prev_val, 2),
                    "prev_value_fmt": _format_value(prev_val) if prev_val > 0 else "0万美元",
                    "weight_pct": round(weight_pct, 2),
                    "pct_change": round(pct_change, 1) if pct_change is not None else None,
                    "net_buy_usd": round(net_buy_usd, 2),
                    "net_buy_fmt": _format_value(abs(net_buy_usd)) if abs(net_buy_usd) > 0 else "0万美元",
                    "net_share_increase_multiple": net_multiple,
                    "share_multiple_definition": "净增倍数 = (期末股数 - 期初股数) / 期初股数",
                    "name": item["name"],
                    "ticker": item["ticker"],
                    "all_tickers": sorted(_entity_tickers(item)),
                    "sec_kind": item["sec_kind"],
                    "category": cat_name,
                    "_raw_item": item,
                    "_raw_end_value_dec": val_dec,
                    "_raw_f_total_dec": f_total_dec,
                    "_raw_net_buy_dec": net_buy_dec,
                }

                # Check same-fund large sells: closed prev >= 50M OR trim amount >= 100M
                is_large_closed = is_closed and prev_val_dec >= Decimal("50000000")
                is_large_trim = (
                    not is_closed
                    and chg_shares < 0
                    and shares > 0
                    and (val_dec * Decimal(abs(chg_shares))) >= Decimal("100000000") * Decimal(shares)
                )
                if is_large_closed or is_large_trim:
                    sell_amt = float(prev_val_dec if is_closed else abs(net_buy_dec))
                    large_sells.append({
                        "name": item["name"],
                        "ticker": item["ticker"],
                        "category": cat_name,
                        "action": item["action"],
                        "sell_amount_usd": round(sell_amt, 2),
                        "sell_amount_fmt": _format_value(sell_amt),
                        "prev_value_usd": round(prev_val, 2),
                        "prev_value_fmt": _format_value(prev_val),
                        "end_value_usd": round(val, 2),
                        "end_value_fmt": _format_value(val) if shares > 0 else "0万美元",
                        "pct_change": round(pct_change, 1) if pct_change is not None else None,
                    })

                ekey = _entity_cross_fund_key(item)
                if ekey not in entities:
                    entities[ekey] = {
                        "entity_key": ekey,
                        "name": item["name"],
                        "ticker": item["ticker"],
                        "all_tickers": set(_entity_tickers(item)),
                        "category": cat_name,
                        "sec_kind": item["sec_kind"],
                        "is_major_bluechip": _is_major_bluechip(item),
                        "is_familiar": _is_familiar(item),
                        "holders": [],
                    }
                else:
                    entities[ekey]["all_tickers"].update(_entity_tickers(item))
                entities[ekey]["holders"].append(holder_rec)

        large_sells.sort(key=lambda x: -x["sell_amount_usd"])
        fund_large_sells[f_slug] = large_sells
        fund_summaries.append({
            "index": run["index"],
            "slug": f_slug,
            "group": run["group"],
            "display_name": run["display_name"],
            "base_period": run["base_period"],
            "cur_quarter": run["cur_quarter"],
            "prev_quarter": run["prev_quarter"],
            "total_value_usd": round(f_total, 2),
            "total_value_fmt": _format_value(f_total),
            "prev_total_value_usd": round(f_prev_total, 2),
            "prev_total_value_fmt": _format_value(f_prev_total) if f_prev_total > 0 else "0万美元",
            "qoq_pct": round(run["qoq_pct"], 1) if run["qoq_pct"] is not None else None,
            "large_sells": large_sells,
        })

    # Build sector consensus matrix, coverage reconciliation, and new clue candidates
    sector_matrix: dict[str, list[dict[str, Any]]] = {
        "AI/Semi/US SaaS": [],
        "China Book": [],
        "Other US Book": [],
    }
    entity_records: list[dict[str, Any]] = []
    ticker_to_entities: dict[str, list[dict[str, Any]]] = {}
    new_clue_candidates: list[dict[str, Any]] = []

    for ent in entities.values():
        holders = ent["holders"]
        active_holders = sorted([h for h in holders if not h["is_closed"]], key=lambda x: -x["end_value_usd"])
        closed_holders = sorted([h for h in holders if h["is_closed"]], key=lambda x: -x["prev_value_usd"])
        ordered_holders = active_holders + closed_holders

        held_count = len(active_holders)
        closed_count = len(closed_holders)
        new_count = sum(1 for h in holders if h["action"] == "建仓")
        add_count = sum(1 for h in holders if h["action"] in ("加仓", "大幅加仓"))
        buy_count = new_count + add_count
        unchanged_count = sum(1 for h in holders if h["action"] in ("不变", "几乎不变"))
        trim_count = sum(1 for h in holders if h["action"] == "减仓")
        sell_count = trim_count + closed_count

        total_end = sum(h["end_value_usd"] for h in active_holders)
        total_prev = sum(h["prev_value_usd"] for h in holders)
        total_net_buy_active = sum(h["net_buy_usd"] for h in active_holders)
        closed_prev_total = sum(h["prev_value_usd"] for h in closed_holders)
        net_flow_all = total_net_buy_active - closed_prev_total

        active_moves = buy_count + sell_count
        divergence_score = round(min(buy_count, sell_count) / max(active_moves, 1), 2) if active_moves > 0 else 0.0
        is_divergent = buy_count > 0 and sell_count > 0
        if buy_count > 0 and sell_count == 0:
            consensus_dir = "一致偏多"
        elif sell_count > 0 and buy_count == 0:
            consensus_dir = "一致偏空"
        elif is_divergent:
            consensus_dir = "多空分歧"
        else:
            consensus_dir = "持平"

        clean_holders = [
            {k: v for k, v in h.items() if not k.startswith("_raw")}
            for h in ordered_holders
        ]

        rec = {
            "entity_key": ent["entity_key"],
            "name": ent["name"],
            "ticker": ent["ticker"],
            "all_tickers": sorted(ent["all_tickers"]),
            "category": ent["category"],
            "sec_kind": ent["sec_kind"],
            "is_major_bluechip": ent["is_major_bluechip"],
            "is_familiar": ent["is_familiar"],
            "held_count": held_count,
            "closed_count": closed_count,
            "new_count": new_count,
            "add_count": add_count,
            "buy_count": buy_count,
            "unchanged_count": unchanged_count,
            "trim_count": trim_count,
            "sell_count": sell_count,
            "total_end_value_usd": round(total_end, 2),
            "total_end_value_fmt": _format_value(total_end) if total_end > 0 else "0万美元",
            "total_prev_value_usd": round(total_prev, 2),
            "total_net_buy_active_usd": round(total_net_buy_active, 2),
            "closed_prev_value_usd": round(closed_prev_total, 2),
            "net_flow_usd": round(net_flow_all, 2),
            "divergence_score": divergence_score,
            "is_divergent": is_divergent,
            "consensus_direction": consensus_dir,
            "holders": clean_holders,
        }
        entity_records.append(rec)
        sector_matrix.setdefault(ent["category"], []).append(rec)
        for tk in ent["all_tickers"]:
            ticker_to_entities.setdefault(tk, []).append(rec)

        # New clue candidate pool check (non-major-bluechip only, exact Decimal cross-multiplication)
        if not ent["is_major_bluechip"]:
            triggers: list[dict[str, Any]] = []
            for h in active_holders:
                is_new = h["action"] == "建仓"
                end_dec: Decimal = h["_raw_end_value_dec"]
                f_tot_dec: Decimal = h["_raw_f_total_dec"]
                net_buy_dec: Decimal = h["_raw_net_buy_dec"]
                shares_i = int(h["shares"])
                prev_shares_i = int(h["prev_shares"])
                chg_shares_i = int(h["chg_shares"])
                pct_ok = is_new or (
                    chg_shares_i > 0
                    and prev_shares_i > 0
                    and (Decimal(chg_shares_i) * Decimal("100")) >= (Decimal(prev_shares_i) * Decimal("20"))
                )
                if not pct_ok:
                    continue
                if end_dec < Decimal("20000000"):
                    continue
                reasons: list[str] = []
                if f_tot_dec > 0 and (end_dec * Decimal("100")) >= (f_tot_dec * Decimal("2")):
                    reasons.append(f"权重{h['weight_pct']:.1f}%>=2%")
                net_buy_ok = (
                    (end_dec >= Decimal("150000000"))
                    if is_new
                    else (shares_i > 0 and (end_dec * Decimal(chg_shares_i)) >= (Decimal("150000000") * Decimal(shares_i)))
                )
                if net_buy_ok:
                    reasons.append(f"净增持{_format_value(float(net_buy_dec))}>=1.5亿")
                if not reasons:
                    continue
                h_clean = {k: v for k, v in h.items() if not k.startswith("_raw")}
                h_clean["trigger_reasons"] = reasons
                h_clean["same_fund_large_sells"] = fund_large_sells.get(h["fund_slug"], [])
                triggers.append(h_clean)

            if triggers:
                new_clue_candidates.append({
                    "name": ent["name"],
                    "ticker": ent["ticker"],
                    "all_tickers": sorted(ent["all_tickers"]),
                    "category": ent["category"],
                    "sec_kind": ent["sec_kind"],
                    "held_count": held_count,
                    "closed_count": closed_count,
                    "total_end_value_usd": round(total_end, 2),
                    "total_end_value_fmt": _format_value(total_end) if total_end > 0 else "0万美元",
                    "triggers": triggers,
                    "all_18_group_holdings": clean_holders,
                })

    for cat_list in sector_matrix.values():
        cat_list.sort(key=lambda x: (-x["held_count"], -x["total_end_value_usd"], x["name"]))
    new_clue_candidates.sort(
        key=lambda x: (
            -max(t["weight_pct"] for t in x["triggers"]),
            -max(t["net_buy_usd"] for t in x["triggers"]),
        )
    )

    # Portfolio & core coverage reconciliation
    roster, cov_source, cov_missing_reason = _load_coverage_roster(workspace_dir, coverage_path)
    coverage_missing = cov_missing_reason is not None
    net_add: list[dict[str, Any]] = []
    net_trim_or_flat: list[dict[str, Any]] = []
    seen_cov_entities: set[str] = set()

    for cov in roster:
        matched: list[dict[str, Any]] = []
        for mt in cov["match_tickers"]:
            for ent_rec in ticker_to_entities.get(mt, []):
                if ent_rec["sec_kind"] == "equity":
                    matched.append(ent_rec)
            if matched:
                break
        if not matched:
            continue
        # Prefer entity matching canonical equity
        ent_rec = matched[0]
        if ent_rec["entity_key"] in seen_cov_entities:
            continue
        seen_cov_entities.add(ent_rec["entity_key"])
        # Exclude 0 holdings (neither active nor closed)
        if ent_rec["held_count"] == 0 and ent_rec["closed_count"] == 0:
            continue
        entry = {
            "coverage_name": cov["name"],
            "coverage_ticker": cov["ticker"],
            "name": ent_rec["name"],
            "ticker": ent_rec["ticker"],
            "is_live_position": cov["is_live_position"],
            "is_core_coverage": cov["is_core_coverage"],
            "is_watch": cov["is_watch"],
            "held_count": ent_rec["held_count"],
            "closed_count": ent_rec["closed_count"],
            "buy_count": ent_rec["buy_count"],
            "sell_count": ent_rec["sell_count"],
            "unchanged_count": ent_rec["unchanged_count"],
            "total_end_value_usd": ent_rec["total_end_value_usd"],
            "total_end_value_fmt": ent_rec["total_end_value_fmt"],
            "net_flow_usd": ent_rec["net_flow_usd"],
            "net_flow_fmt": _format_value(abs(ent_rec["net_flow_usd"])) if abs(ent_rec["net_flow_usd"]) > 0 else "0万美元",
            "holders": ent_rec["holders"],
        }
        if ent_rec["net_flow_usd"] > 0:
            net_add.append(entry)
        else:
            net_trim_or_flat.append(entry)

    net_add.sort(key=lambda x: -x["net_flow_usd"])
    net_trim_or_flat.sort(key=lambda x: x["net_flow_usd"])

    return {
        "ok": True,
        "quarter": short_q,
        "full_quarter": full_q,
        "prev_quarter": prev_short_q,
        "coverage_source": cov_source,
        "coverage_missing": coverage_missing,
        "coverage_missing_reason": cov_missing_reason,
        "funds": fund_summaries,
        "sector_consensus_matrix": sector_matrix,
        "portfolio_and_coverage_reconciliation": {
            "coverage_source": cov_source,
            "coverage_missing": coverage_missing,
            "coverage_missing_reason": cov_missing_reason,
            "net_add": net_add,
            "net_trim_or_flat": net_trim_or_flat,
        },
        "new_clue_candidates": new_clue_candidates,
        "fund_large_sells": fund_large_sells,
    }


def cmd_aggregate(args) -> None:
    payload = _compute_aggregate_payload(
        data_dir=Path(args.data_dir),
        quarter=args.quarter,
        workspace_dir=_WORKSPACE_DIR,
        coverage_path=getattr(args, "coverage", None),
        include_biotech=bool(getattr(args, "include_biotech", False)),
    )
    if getattr(args, "out", None):
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _emit(payload, _as_json(args))


def _save_descriptors_overlay(items: dict[str, str], quarter: str) -> dict[str, Any]:
    """Persist drafted descriptors into <workspace>/state/dalton-13f-tracker/descriptors.json."""
    if not _WRITES_ENABLED or _STATE_DIR is None:
        raise RuntimeError("--workspace-dir is required to save descriptors")
    target = _STATE_DIR / "descriptors.json"
    if not _is_safe_write_target(target):
        raise RuntimeError(f"Refusing to write descriptors outside workspace: {target}")

    q_norm = quarter.upper().strip()
    added_tickers: list[str] = []
    with _state_lock():
        if not _is_safe_write_target(target):
            raise RuntimeError(f"Refusing to write descriptors outside workspace: {target}")
        disk = _read_json_file(target)
        disk_descs = dict(disk.get("descriptors") or {}) if isinstance(disk.get("descriptors"), dict) else {}
        disk_added_q = dict(disk.get("_added_by_quarter") or {}) if isinstance(disk.get("_added_by_quarter"), dict) else {}
        q_list = list(disk_added_q.get(q_norm) or [])

        for raw_t, raw_d in items.items():
            tk = str(raw_t).upper().strip()
            dv = str(raw_d).strip()
            if not tk or not dv:
                continue
            if not (4 <= len(dv) <= 24):
                raise ValueError(f"Descriptor for {tk} must be 4-24 chars (got {len(dv)}: '{dv}')")
            if disk_descs.get(tk) != dv:
                added_tickers.append(tk)
            disk_descs[tk] = dv
            if tk not in q_list:
                q_list.append(tk)

        disk_added_q[q_norm] = sorted(set(q_list))
        out_payload: dict[str, Any] = dict(disk)
        out_payload["descriptors"] = dict(sorted(disk_descs.items()))
        out_payload["_added_by_quarter"] = disk_added_q
        _atomic_write_json(target, out_payload)

    for tk, dv in items.items():
        if str(tk).strip() and str(dv).strip():
            _DESCRIPTOR_MAP[str(tk).upper().strip()] = str(dv).strip()
    for tk in added_tickers:
        _append_auto_learn_log("descriptor", tk, _DESCRIPTOR_MAP[tk], "model-draft", f"quarter={q_norm}")
    _ADDED_DESCRIPTORS_BY_Q[q_norm] = sorted(set(_ADDED_DESCRIPTORS_BY_Q.get(q_norm, [])) | set(items))
    return {
        "ok": True,
        "quarter": q_norm,
        "added_count": len(added_tickers),
        "quarter_total_added": len(_ADDED_DESCRIPTORS_BY_Q.get(q_norm, [])),
        "added": {tk: _DESCRIPTOR_MAP[tk] for tk in added_tickers},
        "summary_line": f"本季新增定语 {len(_ADDED_DESCRIPTORS_BY_Q.get(q_norm, []))} 条",
        "path": str(target),
    }


def cmd_descriptors_add(args) -> None:
    if not _WRITES_ENABLED:
        _emit({
            "ok": False,
            "error": "--workspace-dir is required for descriptors-add (read-only mode).",
            "hint": "Pass --workspace-dir <workspace> to persist descriptors under <workspace>/state/dalton-13f-tracker/.",
        }, _as_json(args))
        return

    short_q, _full_q, _prev_q, _ym = _parse_quarter_tag(args.quarter)
    if getattr(args, "items_file", None):
        raw_items = json.loads(Path(args.items_file).read_text(encoding="utf-8"))
    elif getattr(args, "items_json", None):
        raw_items = json.loads(args.items_json)
    else:
        raise ValueError("Provide either --items-json or --items-file")

    if not isinstance(raw_items, dict) or not raw_items:
        raise ValueError("Descriptor payload must be a non-empty JSON object {ticker: descriptor}")

    clean_items = {str(k).upper().strip(): str(v).strip() for k, v in raw_items.items() if str(k).strip()}
    res = _save_descriptors_overlay(clean_items, short_q)
    _emit(res, _as_json(args))


def _parse_annotated_tickers(raw: str | None) -> set[str]:
    out: set[str] = set()
    if not raw:
        return out
    tokens = [t.strip().upper() for t in re.split(r"[,;\s]+", raw) if t.strip()]
    for tk in tokens:
        out.add(tk)
        res = _resolve_alias_safely(tk)
        if res:
            out.add(res)
        canon = str(_MERGE_RULES.get(tk) or "").strip()
        if canon and not canon.startswith("_"):
            out.add(f"canon:{canon}")
            for k, v in _MERGE_RULES.items():
                if v == canon and not k.startswith("_"):
                    out.add(_clean_ticker(k))
        out.add(f"ticker:{res or tk}")
    return out


def cmd_render_batch(args) -> None:
    short_q, full_q, _prev_q, runs = _load_batch_runs(
        Path(args.data_dir),
        args.quarter,
        include_biotech=bool(getattr(args, "include_biotech", False)),
    )
    annotated_keys = _parse_annotated_tickers(getattr(args, "annotated_tickers", ""))
    seen_descriptor_keys: set[str] = set()
    seen_missing_keys: set[str] = set()
    missing_descriptors: list[dict[str, Any]] = []
    embedded_descriptors: list[dict[str, Any]] = []

    runs_by_group: dict[str, list[dict[str, Any]]] = {
        "China Book": [],
        "US Growth": [],
        "Value": [],
    }
    for run in runs:
        runs_by_group.setdefault(run["group"], []).append(run)

    section_blocks: list[str] = []
    for grp_key, grp_header in _GROUP_SECTION_HEADERS:
        grp_runs = runs_by_group.get(grp_key, [])
        lines: list[str] = [grp_header, ""]
        for run in grp_runs:
            val_str = _format_value(run["total_value_usd"])
            if run["qoq_pct"] is not None:
                sub = (
                    f"### {run['index']}. {run['display_name']} "
                    f"{run['cur_quarter']} vs {run['prev_quarter']}"
                    f"（13F 市值 {val_str}，环比 {run['qoq_pct']:+.1f}%）"
                )
            else:
                sub = (
                    f"### {run['index']}. {run['display_name']} "
                    f"{run['cur_quarter']} vs {run['prev_quarter']}（13F 市值 {val_str}）"
                )
            lines.append(sub)
            lines.append("")

            for cat_name in ("China Book", "AI/Semi/US SaaS", "Other US Book"):
                items = run["categories"].get(cat_name, [])
                if not items:
                    continue
                active = sorted([i for i in items if not i["is_closed"]], key=lambda x: -x["value"])
                closed = sorted([i for i in items if i["is_closed"]], key=lambda x: -x["prev_value"])
                lines.append(f"**{cat_name}**")
                for item in active + closed:
                    desc_to_embed = ""
                    action = item["action"]
                    val = float(item["value"])
                    tk = _clean_ticker(item["ticker"])
                    dedup_key = _entity_dedup_key(item)
                    if (
                        action in ("建仓", "大幅加仓")
                        and val >= 5_000_000.0
                        and item["sec_kind"] == "equity"
                        and tk
                        and not _is_familiar(item)
                        and dedup_key not in annotated_keys
                        and tk not in annotated_keys
                        and dedup_key not in seen_descriptor_keys
                    ):
                        desc = _lookup_descriptor(item)
                        if desc:
                            desc_to_embed = desc
                            seen_descriptor_keys.add(dedup_key)
                            embedded_descriptors.append({
                                "ticker": tk,
                                "name": item["name"],
                                "fund": run["slug"],
                                "descriptor": desc,
                            })
                        elif dedup_key not in seen_missing_keys:
                            seen_missing_keys.add(dedup_key)
                            missing_descriptors.append({
                                "ticker": tk,
                                "name": item["name"],
                                "issuer": item["issuer"],
                                "category": cat_name,
                                "fund": run["slug"],
                                "fund_name": run["display_name"],
                                "action": action,
                                "end_value_usd": round(val, 2),
                                "end_value_fmt": _format_value(val),
                            })

                    display_name = f"{desc_to_embed}{item['name']}" if desc_to_embed else item["name"]
                    bullet_body = _change_template(
                        display_name,
                        item["value"],
                        item["prev_shares"],
                        item["chg_shares"],
                        item["prev_value"],
                    )
                    lines.append(f"- {item['trend_prefix']}{bullet_body}")
                lines.append("")
        section_blocks.append("\n".join(lines).rstrip())

    sections_3_to_5 = "\n\n---\n\n".join(section_blocks) + "\n"
    title = f"# 13F Tracker_{full_q}"

    full_report: str | None = None
    chapters_file = getattr(args, "chapters_file", None)
    if chapters_file:
        ch_text = Path(chapters_file).read_text(encoding="utf-8").strip()
        m_title = re.match(r"^#\s*13F\s+Tracker_(\d{2,4}[Qq][1-4])\b", ch_text)
        if m_title:
            ch_short_q, ch_full_q, _p, _ym = _parse_quarter_tag(m_title.group(1))
            if ch_full_q != full_q:
                raise RuntimeError(
                    f"Chapters title quarter '{m_title.group(1)}' ({ch_full_q}) does not match --quarter '{short_q}' ({full_q})"
                )
        else:
            ch_text = f"{title}\n\n{ch_text}"
        full_report = f"{ch_text}\n\n---\n\n{sections_3_to_5}"

    if getattr(args, "out", None):
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(full_report if full_report is not None else sections_3_to_5, encoding="utf-8")

    q_added_count = len(_ADDED_DESCRIPTORS_BY_Q.get(short_q, []))
    result: dict[str, Any] = {
        "ok": True,
        "quarter": short_q,
        "full_quarter": full_q,
        "title": title,
        "status": "needs_descriptors" if missing_descriptors else "ok",
        "missing_descriptors": missing_descriptors,
        "missing_count": len(missing_descriptors),
        "embedded_descriptors": embedded_descriptors,
        "new_descriptors_count": q_added_count,
        "new_descriptors_line": f"本季新增定语 {q_added_count} 条",
        "sections_3_to_5": sections_3_to_5,
    }
    if full_report is not None:
        result["full_report"] = full_report
    _emit(result, _as_json(args))


def _strip_md_for_audit(text: str) -> str:
    """Remove Markdown emphasis and accidental backslash escapes before number audit."""
    s = text.replace("**", "")
    for ch in ("_", ".", "+", "-", "(", ")", "&"):
        s = s.replace("\\" + ch, ch)
    return s


def _build_audit_lookup_indices(
    agg: dict[str, Any],
    coverage_roster: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
    """Build stock & fund alias maps and normalized data indices from aggregate JSON."""
    fund_alias_to_slug: dict[str, str] = {}
    fund_by_slug: dict[str, dict[str, Any]] = {}
    for f in agg.get("funds", []):
        slug = str(f["slug"])
        fund_by_slug[slug] = f
        candidates = {
            slug,
            slug.replace("_", " "),
            str(f.get("display_name") or ""),
        }
        disp = str(f.get("display_name") or "")
        # Strip parenthetical notes like （HHLR + Evergreen 合并）
        candidates.add(re.sub(r"（[^）]*）", "", disp).strip())
        for g in _batch_groups():
            if g["slug"] == slug:
                candidates.update(g.get("aliases") or [])
        for c in list(candidates):
            cs = c.strip()
            if len(cs) >= 2:
                fund_alias_to_slug[cs.lower()] = slug
                # Also allow first 1-2 words for names like "Tiger Global Management" -> "Tiger Global" / "Tiger"
                parts = cs.split()
                if len(parts) >= 2 and len(parts[0]) >= 3:
                    fund_alias_to_slug.setdefault(" ".join(parts[:2]).lower(), slug)
                    if parts[0].lower() not in {"first", "capital", "lone", "whale", "china", "us"}:
                        fund_alias_to_slug.setdefault(parts[0].lower(), slug)
        # Explicit short aliases used in prose
        for short_k, target_slug in (
            ("高瓴", "hillhouse"), ("景林", "greenwoods"), ("红杉中国", "sequoia_china"),
            ("红杉美国", "sequoia_us"), ("今日资本", "capital_today"), ("伯克希尔", "berkshire"),
            ("易方", "yiheng"), ("易方资本", "yiheng"), ("tiger", "tiger_global"),
            ("d1", "d1_capital"), ("durable", "durable"), ("dragoneer", "dragoneer"),
            ("lone pine", "lone_pine"), ("whale rock", "whale_rock"), ("altimeter", "altimeter"),
            ("coatue", "coatue"), ("aspex", "aspex"), ("first beijing", "first_beijing"),
            ("brilliance", "brilliance"), ("yiheng", "yiheng"),
        ):
            fund_alias_to_slug[short_k.lower()] = target_slug

    stock_alias_to_key: dict[str, str] = {}
    stock_by_key: dict[str, dict[str, Any]] = {}
    cov_by_tk = {c["ticker"]: c for c in coverage_roster}

    for _cat, entries in (agg.get("sector_consensus_matrix") or {}).items():
        for ent in entries:
            ekey = ent["entity_key"]
            stock_by_key[ekey] = ent
            aliases = {ent["name"], ent["ticker"], *ent.get("all_tickers", [])}
            # Strip parenthetical suffix like " (SMH)" or " (Neutron Holdings)"
            base_nm = re.sub(r"\s*\([^)]*\)$", "", ent["name"]).strip()
            if len(base_nm) >= 2:
                aliases.add(base_nm)
            # First word if distinctive (>=4 chars and not generic)
            words = base_nm.split()
            if words and len(words[0]) >= 4 and words[0].lower() not in {"new", "applied", "united", "first", "liberty", "martin", "american", "bank", "the", "china", "super", "proshares", "spdr", "vaneck"}:
                aliases.add(words[0])
            for tk in ent.get("all_tickers", []):
                aliases.update(_BUILTIN_STOCK_ALIASES.get(tk, []))
                if tk in cov_by_tk:
                    aliases.add(cov_by_tk[tk]["name"])
                    aliases.update(cov_by_tk[tk].get("search_names") or [])
            for al in aliases:
                als = str(al or "").strip()
                if len(als) >= 2 and als.upper() not in {"ON", "IT", "AI", "US", "UK", "UP", "BE", "SO", "ALL", "NOW", "MAN", "ONE"}:
                    stock_alias_to_key[als.lower()] = ekey

    return fund_alias_to_slug, fund_by_slug, stock_alias_to_key, stock_by_key


def _find_mentions_in_text(text: str, alias_map: dict[str, str]) -> list[tuple[int, str]]:
    """Find longest-match entity mentions sorted by start offset."""
    low = text.lower()
    hits: list[tuple[int, int, str]] = []
    for al in sorted(alias_map, key=len, reverse=True):
        if len(al) < 2:
            continue
        is_ascii = bool(re.fullmatch(r"[a-z0-9.\-& ]+", al))
        if is_ascii:
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(al) + r"(?![a-z0-9])")
        else:
            pat = re.compile(re.escape(al))
        for m in pat.finditer(low):
            s, e = m.start(), m.end()
            if any(not (e <= hs or s >= he) for hs, he, _ in hits):
                continue
            hits.append((s, e, alias_map[al]))
    hits.sort(key=lambda x: x[0])
    return [(s, key) for s, _e, key in hits]


def _extract_chapters_1_2(report_text: str) -> str:
    marker = "## China Book 机构"
    idx = report_text.find(marker)
    return report_text[:idx] if idx != -1 else report_text


def _within_tol(claimed: float, actual: float, base_tol: float, is_approx: bool) -> bool:
    tol = max(base_tol, 0.06 * abs(claimed)) if is_approx else base_tol
    return abs(actual - claimed) <= tol


def cmd_verify_numbers(args) -> None:
    """Non-blocking numeric verification for Chapters 1-2 against aggregate data."""
    if getattr(args, "aggregate", None):
        agg = json.loads(Path(args.aggregate).read_text(encoding="utf-8"))
    elif getattr(args, "data_dir", None) and getattr(args, "quarter", None):
        agg = _compute_aggregate_payload(
            data_dir=Path(args.data_dir),
            quarter=args.quarter,
            workspace_dir=_WORKSPACE_DIR,
            coverage_path=getattr(args, "coverage", None),
        )
    else:
        raise ValueError("Provide --aggregate <json> or (--data-dir <dir> --quarter <YYQN>)")

    raw_text = Path(args.report_file).read_text(encoding="utf-8")
    ch12 = _strip_md_for_audit(_extract_chapters_1_2(raw_text))
    roster, _, _ = _load_coverage_roster(_WORKSPACE_DIR, getattr(args, "coverage", None))
    fund_al_map, _fund_by_slug, stock_al_map, stock_by_key = _build_audit_lookup_indices(agg, roster)

    # Index (stock_key, fund_slug) -> holder record
    holder_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for skey, ent in stock_by_key.items():
        for h in ent.get("holders", []):
            holder_lookup[(skey, h["fund_slug"])] = h

    mismatches: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    checked_count = 0
    matched_count = 0

    approx_re = re.compile(r"(近|约|超|逾|高达|超过)\s*$")
    range_pct_re = re.compile(r"(近|约|超|逾)?\s*([+-]?\d+(?:\.\d+)?)\s*%\s*至\s*([+-]?\d+(?:\.\d+)?)\s*%")
    num_pat = re.compile(
        r"(?:(近|约|超|逾|高达|超过)\s*)?"
        r"(?:"
        r"([+-]?\d+(?:\.\d+)?)\s*(亿美元|万美元|亿|万)(?:\s*美元)?"
        r"|([+-]?\d+(?:\.\d+)?)\s*%"
        r"|(\d+(?:\.\d+)?)\s*倍"
        r"|(\d+)\s*家(?:\s*(持有|买入|买|卖出|卖|加仓|减仓|清仓|不变|持仓|同步建仓))?"
        r")"
    )
    sum_cue_re = re.compile(r"(合计|两家|两票|三家|三票|共|总计)")

    for line_no, raw_line in enumerate(ch12.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Split line into sentences
        sentences = [s.strip() for s in re.split(r"[。；]", line) if s.strip()]
        for sent in sentences:
            sent_stocks = _find_mentions_in_text(sent, stock_al_map)
            sent_funds = _find_mentions_in_text(sent, fund_al_map)
            segments = [seg.strip() for seg in re.split(r"[，（）()——]", sent) if seg.strip()]

            active_stocks: list[str] = [sent_stocks[0][1]] if len(sent_stocks) == 1 else []
            active_funds: list[str] = [sent_funds[0][1]] if len(sent_funds) == 1 else []

            for seg in segments:
                sub_clauses = [c.strip() for c in seg.split("、") if c.strip()]
                pending_funds: list[str] = []
                for clause in sub_clauses:
                    c_stocks = [k for _p, k in _find_mentions_in_text(clause, stock_al_map)]
                    c_funds = [k for _p, k in _find_mentions_in_text(clause, fund_al_map)]
                    has_any_num = bool(range_pct_re.search(clause) or num_pat.search(clause))
                    if c_stocks:
                        active_stocks = c_stocks
                    if c_funds:
                        if has_any_num:
                            if pending_funds:
                                active_funds = list(dict.fromkeys([*pending_funds, *c_funds]))
                            else:
                                active_funds = c_funds
                            pending_funds = []
                        else:
                            pending_funds = list(dict.fromkeys([*pending_funds, *c_funds]))
                            active_funds = list(pending_funds)
                    elif has_any_num:
                        pending_funds = []

                    eff_stocks = c_stocks or active_stocks or ([k for _p, k in sent_stocks] if len(sent_stocks) > 1 else [])
                    eff_funds = active_funds

                    # Handle explicit percentage range like "30% 至 48%" or "-30% 至 -20%"
                    sell_cues = ("减仓", "减持", "削减", "砍仓", "砍掉", "清仓", "卖出", "抛售")
                    buy_cues = ("加仓", "增持", "加码", "建仓", "买入", "买进")
                    range_spans: list[tuple[int, int]] = []
                    for rm in range_pct_re.finditer(clause):
                        range_spans.append((rm.start(), rm.end()))
                        s_lo, s_hi = rm.group(2).strip(), rm.group(3).strip()
                        lo, hi = sorted((abs(float(s_lo)), abs(float(s_hi))))
                        is_appx = bool(rm.group(1))
                        r_ctx = clause[:rm.start()]
                        has_r_sell = any(w in r_ctx for w in sell_cues)
                        has_r_buy = any(w in r_ctx for w in buy_cues)
                        r_dir = -1 if (has_r_sell and not has_r_buy) else (1 if (has_r_buy and not has_r_sell) else 0)
                        has_pos_sign = s_lo.startswith("+") or s_hi.startswith("+")
                        has_neg_sign = s_lo.startswith("-") or s_hi.startswith("-")
                        if eff_stocks and eff_funds:
                            checked_count += 1
                            skey = eff_stocks[-1]
                            vals = [
                                (-100.0 if holder_lookup[(skey, fs)]["action"] == "清仓" else holder_lookup[(skey, fs)]["pct_change"])
                                for fs in eff_funds
                                if (skey, fs) in holder_lookup
                                and (holder_lookup[(skey, fs)]["pct_change"] is not None or holder_lookup[(skey, fs)]["action"] == "清仓")
                            ]
                            if (has_pos_sign and has_neg_sign) or (has_pos_sign and r_dir == -1):
                                mismatches.append({
                                    "line_no": line_no,
                                    "clause": clause,
                                    "claimed": f"{s_lo}%-{s_hi}%",
                                    "expected": [round(v, 1) for v in vals],
                                    "stock": stock_by_key[skey]["name"],
                                    "funds": eff_funds,
                                    "reason": "动作词与正号方向冲突",
                                })
                                continue
                            if has_neg_sign and r_dir == 1:
                                mismatches.append({
                                    "line_no": line_no,
                                    "clause": clause,
                                    "claimed": f"{s_lo}%-{s_hi}%",
                                    "expected": [round(v, 1) for v in vals],
                                    "stock": stock_by_key[skey]["name"],
                                    "funds": eff_funds,
                                    "reason": "动作词与负号方向冲突",
                                })
                                continue
                            eff_dir = r_dir or (1 if has_pos_sign else (-1 if has_neg_sign else 0))
                            lo_b = lo - (max(1.0, 0.06 * lo) if is_appx else 1.0)
                            hi_b = hi + (max(1.0, 0.06 * hi) if is_appx else 1.0)
                            if vals and all(
                                ((v < 0 and lo_b <= abs(v) <= hi_b) if eff_dir == -1 else (v > 0 and lo_b <= abs(v) <= hi_b) if eff_dir == 1 else (lo_b <= abs(v) <= hi_b))
                                for v in vals
                            ):
                                matched_count += 1
                            elif vals:
                                mismatches.append({
                                    "line_no": line_no,
                                    "clause": clause,
                                    "claimed": f"{s_lo}%-{s_hi}%",
                                    "expected": [round(v, 1) for v in vals],
                                    "stock": stock_by_key[skey]["name"],
                                    "funds": eff_funds,
                                    "reason": "幅度区间或方向不匹配",
                                })

                    last_match_end = 0
                    for m in num_pat.finditer(clause):
                        local_prefix = clause[last_match_end:m.start()]
                        last_match_end = m.end()
                        if any(rs <= m.start() < re_ for rs, re_ in range_spans):
                            continue
                        qual = m.group(1) or (approx_re.search(clause[:m.start()]).group(1) if approx_re.search(clause[:m.start()]) else "")
                        is_appx = bool(qual)
                        is_sum_ctx = bool(sum_cue_re.search(clause) or ("合计" in sent and ("+" in sent or "与" in sent)))

                        # 1) Amount (亿 / 万)
                        if m.group(2) is not None:
                            num = float(m.group(2))
                            unit = m.group(3)
                            claimed_usd = num * (1e8 if unit.startswith("亿") else 1e4)
                            base_tol = 12_000_000.0 if claimed_usd >= 100_000_000.0 else 1_500_000.0
                            prev_cues = ("前值", "之前", "上期", "期初", "原持仓", "清仓")
                            net_cues = ("净增", "掏出", "净买", "净减", "减持额")
                            end_cues = ("至", "期末", "持仓", "持有", "建仓", "新建")
                            if any(w in local_prefix for w in prev_cues):
                                period_mode = "prev"
                            elif any(w in local_prefix for w in net_cues):
                                period_mode = "net"
                            elif any(w in local_prefix for w in end_cues):
                                period_mode = "end"
                            elif any(w in clause for w in prev_cues):
                                period_mode = "prev"
                            elif any(w in clause for w in net_cues):
                                period_mode = "net"
                            else:
                                period_mode = "end"

                            checked_count += 1
                            cand_actuals: list[float] = []
                            holder_field = (
                                "prev_value_usd"
                                if period_mode == "prev"
                                else ("net_buy_usd" if period_mode == "net" else "end_value_usd")
                            )
                            stock_total_field = (
                                "total_prev_value_usd"
                                if period_mode == "prev"
                                else ("total_net_buy_active_usd" if period_mode == "net" else "total_end_value_usd")
                            )
                            is_abs_net_trim = period_mode == "net" and ("净减" in clause or "减持额" in clause)
                            # Collect BOTH named-funds sum AND all-holder total; match either without relying on breakdown cues
                            sum_funds = c_funds if len(c_funds) >= 2 else list(dict.fromkeys(k for _p, k in sent_funds)) or eff_funds
                            sum_stocks = c_stocks if len(c_stocks) >= 2 else list(dict.fromkeys(k for _p, k in sent_stocks)) or eff_stocks
                            if is_sum_ctx and len(sum_funds) >= 2 and len(sum_stocks) >= 1:
                                skey = sum_stocks[0]
                                s_sum = sum(
                                    (abs(holder_lookup[(skey, fs)]["net_buy_usd"]) if is_abs_net_trim else holder_lookup[(skey, fs)][holder_field])
                                    for fs in sum_funds if (skey, fs) in holder_lookup
                                )
                                if s_sum > 0:
                                    cand_actuals.append(s_sum)
                            if is_sum_ctx and len(sum_stocks) >= 2 and len(eff_funds) >= 1:
                                fs = eff_funds[-1]
                                s_sum = sum(
                                    (abs(holder_lookup[(sk, fs)]["net_buy_usd"]) if is_abs_net_trim else holder_lookup[(sk, fs)][holder_field])
                                    for sk in sum_stocks if (sk, fs) in holder_lookup
                                )
                                if s_sum > 0:
                                    cand_actuals.append(s_sum)
                                elif period_mode == "end":
                                    n_sum = sum(
                                        holder_lookup[(sk, fs)]["net_buy_usd"]
                                        for sk in sum_stocks if (sk, fs) in holder_lookup
                                    )
                                    if n_sum > 0:
                                        cand_actuals.append(n_sum)
                            if ("合计" in clause or "总" in clause) and len(sum_stocks) == 1 and not c_funds:
                                cand_actuals.append(stock_by_key[sum_stocks[0]][stock_total_field])
                            if not cand_actuals and eff_stocks and eff_funds:
                                skey = eff_stocks[-1] if len(c_stocks) else eff_stocks[0]
                                fs = eff_funds[-1]
                                h = holder_lookup.get((skey, fs))
                                if h:
                                    if period_mode == "prev":
                                        cand_actuals.append(h["prev_value_usd"])
                                    elif period_mode == "net":
                                        net_val = abs(h["net_buy_usd"]) if ("净减" in clause or "减持额" in clause) else h["net_buy_usd"]
                                        cand_actuals.append(net_val)
                                    else:
                                        cand_actuals.append(h["end_value_usd"])

                            if not cand_actuals:
                                unresolved.append({"line_no": line_no, "clause": clause, "claimed": m.group(0)})
                                continue
                            if any(_within_tol(claimed_usd, act, base_tol, is_appx) for act in cand_actuals):
                                matched_count += 1
                            else:
                                mismatches.append({
                                    "line_no": line_no,
                                    "clause": clause,
                                    "claimed": m.group(0),
                                    "expected": [_format_value(a) for a in cand_actuals],
                                    "stock": [stock_by_key[s]["name"] for s in eff_stocks if s in stock_by_key],
                                    "funds": eff_funds,
                                    "reason": f"金额与底表（{period_mode}）超出容差",
                                })
                            continue

                        # 2) Percentage (%)
                        if m.group(4) is not None:
                            raw_pct_str = m.group(4).strip()
                            pct_abs = abs(float(raw_pct_str))
                            is_weight = any(w in clause for w in ("占", "权重", "仓位", "组合", "全仓"))
                            checked_count += 1
                            cand_pcts: list[float] = []
                            claimed_val = pct_abs
                            if is_weight:
                                if is_sum_ctx and len(eff_stocks) >= 2 and eff_funds:
                                    fs = eff_funds[-1]
                                    w_sum = sum(
                                        holder_lookup[(sk, fs)]["weight_pct"]
                                        for sk in eff_stocks if (sk, fs) in holder_lookup
                                    )
                                    if w_sum > 0:
                                        cand_pcts.append(w_sum)
                                elif eff_stocks and eff_funds:
                                    skey = eff_stocks[-1] if len(c_stocks) else eff_stocks[0]
                                    fs = eff_funds[-1]
                                    h = holder_lookup.get((skey, fs))
                                    if h:
                                        cand_pcts.append(h["weight_pct"])
                            else:
                                # Action percentage carries direction (+ buy, - sell); explicit sign must also agree with action verbs
                                verb_ctx = local_prefix + " " + clause[m.end():]
                                has_sell = any(w in verb_ctx for w in sell_cues)
                                has_buy = any(w in verb_ctx for w in buy_cues)
                                if not (has_sell or has_buy):
                                    has_sell = any(w in clause for w in sell_cues)
                                    has_buy = any(w in clause for w in buy_cues)
                                if not (has_sell or has_buy):
                                    has_sell = any(w in sent for w in sell_cues)
                                    has_buy = any(w in sent for w in buy_cues)

                                if raw_pct_str.startswith("-"):
                                    claimed_val = -pct_abs
                                    if has_buy and not has_sell:
                                        mismatches.append({
                                            "line_no": line_no,
                                            "clause": clause,
                                            "claimed": m.group(0),
                                            "expected": [
                                                round(holder_lookup[(eff_stocks[-1] if len(c_stocks) else eff_stocks[0], eff_funds[-1])]["pct_change"], 2)
                                            ] if (eff_stocks and eff_funds and (eff_stocks[-1] if len(c_stocks) else eff_stocks[0], eff_funds[-1]) in holder_lookup and holder_lookup[(eff_stocks[-1] if len(c_stocks) else eff_stocks[0], eff_funds[-1])]["pct_change"] is not None) else [],
                                            "stock": [stock_by_key[s]["name"] for s in eff_stocks if s in stock_by_key],
                                            "funds": eff_funds,
                                            "reason": "动作词与负号方向冲突",
                                        })
                                        continue
                                elif raw_pct_str.startswith("+"):
                                    claimed_val = pct_abs
                                    if has_sell and not has_buy:
                                        mismatches.append({
                                            "line_no": line_no,
                                            "clause": clause,
                                            "claimed": m.group(0),
                                            "expected": [
                                                round(holder_lookup[(eff_stocks[-1] if len(c_stocks) else eff_stocks[0], eff_funds[-1])]["pct_change"], 2)
                                            ] if (eff_stocks and eff_funds and (eff_stocks[-1] if len(c_stocks) else eff_stocks[0], eff_funds[-1]) in holder_lookup and holder_lookup[(eff_stocks[-1] if len(c_stocks) else eff_stocks[0], eff_funds[-1])]["pct_change"] is not None) else [],
                                            "stock": [stock_by_key[s]["name"] for s in eff_stocks if s in stock_by_key],
                                            "funds": eff_funds,
                                            "reason": "动作词与正号方向冲突",
                                        })
                                        continue
                                else:
                                    if has_sell and not has_buy:
                                        claimed_val = -pct_abs
                                    elif has_buy and not has_sell:
                                        claimed_val = pct_abs
                                    else:
                                        claimed_val = pct_abs
                                if eff_stocks and eff_funds:
                                    skey = eff_stocks[-1] if len(c_stocks) else eff_stocks[0]
                                    fs = eff_funds[-1]
                                    h = holder_lookup.get((skey, fs))
                                    if h:
                                        if h["action"] == "清仓":
                                            cand_pcts.append(-100.0)
                                        elif h["pct_change"] is not None:
                                            cand_pcts.append(h["pct_change"])
                            if not cand_pcts:
                                unresolved.append({"line_no": line_no, "clause": clause, "claimed": m.group(0)})
                                continue
                            base_tol = 0.15 if is_weight else 1.0
                            if any(_within_tol(claimed_val, act, base_tol, is_appx) for act in cand_pcts):
                                matched_count += 1
                            else:
                                mismatches.append({
                                    "line_no": line_no,
                                    "clause": clause,
                                    "claimed": m.group(0),
                                    "expected": [round(a, 2) for a in cand_pcts],
                                    "stock": [stock_by_key[s]["name"] for s in eff_stocks if s in stock_by_key],
                                    "funds": eff_funds,
                                    "reason": "权重占比不匹配" if is_weight else "加减仓幅度或方向不匹配",
                                })
                            continue

                        # 3) Multiple (倍)
                        if m.group(5) is not None:
                            mult_val = float(m.group(5))
                            checked_count += 1
                            if eff_stocks and eff_funds:
                                skey = eff_stocks[-1] if len(c_stocks) else eff_stocks[0]
                                fs = eff_funds[-1]
                                h = holder_lookup.get((skey, fs))
                                act_mult = h.get("net_share_increase_multiple") if h else None
                                if act_mult is not None:
                                    if _within_tol(mult_val, act_mult, 0.25, is_appx):
                                        matched_count += 1
                                    else:
                                        mismatches.append({
                                            "line_no": line_no,
                                            "clause": clause,
                                            "claimed": m.group(0),
                                            "expected": act_mult,
                                            "stock": stock_by_key[skey]["name"],
                                            "funds": [fs],
                                            "reason": "股数净增倍数不匹配",
                                        })
                                    continue
                            unresolved.append({"line_no": line_no, "clause": clause, "claimed": m.group(0)})
                            continue

                        # 4) Count (家)
                        if m.group(6) is not None:
                            cnt_val = int(m.group(6))
                            role = m.group(7) or ""
                            checked_count += 1
                            if not eff_stocks:
                                unresolved.append({"line_no": line_no, "clause": clause, "claimed": m.group(0)})
                                continue
                            ent = stock_by_key[eff_stocks[-1]]
                            role_map = {
                                "持有": ent["held_count"],
                                "持仓": ent["held_count"],
                                "买入": ent["buy_count"],
                                "买": ent["buy_count"],
                                "同步建仓": ent["new_count"],
                                "加仓": ent["add_count"],
                                "卖出": ent["sell_count"],
                                "卖": ent["sell_count"],
                                "减仓": ent["trim_count"],
                                "清仓": ent["closed_count"],
                                "不变": ent["unchanged_count"],
                            }
                            if role in role_map:
                                expected_cnt = role_map[role]
                                if expected_cnt == cnt_val or (role == "买入" and ent["new_count"] == cnt_val):
                                    matched_count += 1
                                else:
                                    mismatches.append({
                                        "line_no": line_no,
                                        "clause": clause,
                                        "claimed": m.group(0),
                                        "expected": expected_cnt,
                                        "stock": ent["name"],
                                        "reason": f"机构家数（{role}）不匹配",
                                    })
                            else:
                                # Generic 家: match any of held/buy/new/closed
                                cands = {ent["held_count"], ent["buy_count"], ent["new_count"], ent["closed_count"]}
                                if cnt_val in cands:
                                    matched_count += 1
                                else:
                                    mismatches.append({
                                        "line_no": line_no,
                                        "clause": clause,
                                        "claimed": m.group(0),
                                        "expected": sorted(cands),
                                        "stock": ent["name"],
                                        "reason": "机构家数不匹配",
                                    })

    reminders = [
        f"L{mm['line_no']}: 「{mm['claimed']}」({mm['reason']})，底表参考={mm['expected']}，原分句：{mm['clause']}"
        for mm in mismatches
    ]
    _emit({
        "ok": True,
        "blocking": False,
        "checked_count": checked_count,
        "matched_count": matched_count,
        "mismatch_count": len(mismatches),
        "unresolved_count": len(unresolved),
        "mismatches": mismatches,
        "unresolved": unresolved,
        "reminders": reminders,
    }, _as_json(args))


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

    # aggregate (v2)
    p = sub.add_parser("aggregate", parents=[common])
    p.add_argument("--data-dir", required=True, help="Directory holding 01_hillhouse_*.json .. 18_berkshire_*.json")
    p.add_argument("--quarter", required=True, help="Quarter tag, e.g. 26Q3")
    p.add_argument("--coverage", default="", help="Optional path to coverage.yaml / portfolio_coverage.json")
    p.add_argument("--include-biotech", action="store_true", help="Include biotech rows in consensus/clues")
    p.add_argument("--out", default="", help="Optional output file path for aggregate JSON")
    p.set_defaults(func=cmd_aggregate)

    # render-batch (v2)
    p = sub.add_parser("render-batch", parents=[common])
    p.add_argument("--data-dir", required=True, help="Directory holding 01_hillhouse_*.json .. 18_berkshire_*.json")
    p.add_argument("--quarter", required=True, help="Quarter tag, e.g. 26Q3")
    p.add_argument("--annotated-tickers", default="", help="Comma-separated tickers already annotated with descriptors in Ch 1-2")
    p.add_argument("--chapters-file", default="", help="Optional markdown file containing Chapters 1-2 to assemble full 5-section report")
    p.add_argument("--include-biotech", action="store_true", help="Include biotech rows")
    p.add_argument("--out", default="", help="Optional output markdown file path")
    p.set_defaults(func=cmd_render_batch)

    # descriptors-add (v2)
    p = sub.add_parser("descriptors-add", parents=[common])
    p.add_argument("--quarter", required=True, help="Quarter tag, e.g. 26Q3")
    p.add_argument("--items-json", default="", help="JSON object {ticker: descriptor}")
    p.add_argument("--items-file", default="", help="Path to JSON file {ticker: descriptor}")
    p.set_defaults(func=cmd_descriptors_add)

    # verify-numbers (v2)
    p = sub.add_parser("verify-numbers", parents=[common])
    p.add_argument("--report-file", required=True, help="Path to markdown report containing Chapters 1-2")
    p.add_argument("--aggregate", default="", help="Path to aggregate JSON")
    p.add_argument("--data-dir", default="", help="Directory holding 18 compare JSONs (if --aggregate omitted)")
    p.add_argument("--quarter", default="", help="Quarter tag (if --aggregate omitted)")
    p.add_argument("--coverage", default="", help="Optional coverage.yaml path")
    p.set_defaults(func=cmd_verify_numbers)

    args = parser.parse_args()
    ws_dir = getattr(args, "workspace_dir", None)
    _init_state(ws_dir)
    if not ws_dir:
        print(
            "INFO: --workspace-dir not set; reading built-in mappings only (writes disabled).",
            file=sys.stderr,
        )
    if args.command in ("summary", "top", "compare", "self-test"):
        configure_identity(warn_default=True)
    try:
        args.func(args)
    except Exception as e:
        _emit({"ok": False, "error": str(e), "hint": _error_hint(str(e))}, _as_json(args))
        sys.exit(1)


if __name__ == "__main__":
    main()
