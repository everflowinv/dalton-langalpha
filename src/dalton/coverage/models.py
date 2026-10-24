"""Data models, lossless YAML <-> DB row conversion, and ledger validation for per-user coverage."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Mapping

import yaml

VALID_TIERS = frozenset({"core", "watch"})
VALID_SOURCES = frozenset({"seed", "chat", "ui", "group"})
MAX_MIRROR_LEDGER_BYTES = 1024 * 1024  # 1 MB

DEDICATED_COMPANY_KEYS: tuple[str, ...] = (
    "ticker",
    "name",
    "tier",
    "market",
    "peer_group",
    "peers",
    "aliases",
    "earnings",
)

CANONICAL_COMPANY_KEY_ORDER: tuple[str, ...] = (
    "ticker",
    "name",
    "tier",
    "market",
    "peer_group",
    "sheet_section",
    "peers",
    "etfs",
    "aliases",
    "earnings",
    "workspace",
    "coverage_stage",
    "note",
)

DEFAULT_TIER_MEANINGS: dict[str, str] = {
    "core": "每周五源复盘、每日跟踪、财报前瞻和点评全链",
    "watch": "只看异动；财报只做快讯",
}


def infer_market(ticker: str) -> str:
    sym = str(ticker or "").strip().upper()
    if sym.endswith(".HK"):
        return "HK"
    if sym.endswith((".SS", ".SZ", ".SH")):
        return "CN"
    return "US"


def normalize_tier(tier: str | None, default: str = "watch") -> str:
    val = str(tier or default).strip().lower()
    if val not in VALID_TIERS:
        raise ValueError(f"Invalid tier {tier!r}; must be one of {sorted(VALID_TIERS)}")
    return val


def normalize_source(source: str | None, default: str = "chat") -> str:
    val = str(source or default).strip().lower()
    if val not in VALID_SOURCES:
        raise ValueError(f"Invalid source {source!r}; must be one of {sorted(VALID_SOURCES)}")
    return val


def _ensure_json_obj(val: Any, default: Any) -> Any:
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val


def extract_row_group_ids(row: Mapping[str, Any]) -> list[str]:
    """Return deduplicated list of group UUIDs that `row` belongs to."""
    out: list[str] = []
    extra = _ensure_json_obj(row.get("extra"), {})
    if isinstance(extra, Mapping):
        raw_list = extra.get("group_ids")
        if isinstance(raw_list, (list, tuple)):
            for item in raw_list:
                gid = str(item or "").strip()
                if gid and gid not in out:
                    out.append(gid)
    primary = str(row.get("group_id") or "").strip()
    if primary and primary not in out:
        out.insert(0, primary)
    return out


def extract_row_stale_group_ids(row: Mapping[str, Any]) -> list[str]:
    """Return deduplicated list of group UUIDs for which `row` is marked stale.

    Reads `extra.group_stale_ids` when present. For legacy rows that only have boolean
    `extra.group_stale = true`, interprets that flag as the row's primary group (`group_id`
    or first entry of `group_ids`) no longer matching criteria.
    """
    out: list[str] = []
    extra = _ensure_json_obj(row.get("extra"), {})
    if not isinstance(extra, Mapping):
        return out
    raw_stale = extra.get("group_stale_ids")
    if isinstance(raw_stale, (list, tuple)):
        for item in raw_stale:
            gid = str(item or "").strip()
            if gid and gid not in out:
                out.append(gid)
        return out
    if bool(extra.get("group_stale")):
        primary = str(row.get("group_id") or "").strip()
        if not primary:
            gids = extract_row_group_ids(row)
            primary = gids[0] if gids else ""
        if primary and primary not in out:
            out.append(primary)
    return out


def company_to_row(
    company: Mapping[str, Any],
    *,
    user_id: str,
    source: str = "seed",
    group_id: str | None = None,
    group_ids: list[str] | tuple[str, ...] | None = None,
    group_stale_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Convert a `coverage.yaml` company dict into a `dalton_coverage` row dict losslessly."""
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id must not be empty")
    raw_ticker = str(company.get("ticker") or "").strip().upper()
    if not raw_ticker:
        raise ValueError("company ticker must not be empty")

    tier = normalize_tier(company.get("tier"), default="watch")
    src = normalize_source(source, default="seed")
    name = str(company.get("name") if company.get("name") is not None else "")
    market = str(company.get("market") if company.get("market") is not None else infer_market(raw_ticker))
    peer_group = str(company.get("peer_group") if company.get("peer_group") is not None else "")

    raw_peers = company.get("peers")
    peers = list(raw_peers) if isinstance(raw_peers, (list, tuple)) else []

    raw_aliases = company.get("aliases")
    aliases = dict(raw_aliases) if isinstance(raw_aliases, Mapping) else {}

    raw_earnings = company.get("earnings")
    earnings = dict(raw_earnings) if isinstance(raw_earnings, Mapping) else {}

    extra: dict[str, Any] = {
        k: v for k, v in company.items() if k not in DEDICATED_COMPANY_KEYS
    }

    merged_gids: list[str] = []
    if isinstance(extra.get("group_ids"), (list, tuple)):
        for g in extra["group_ids"]:
            gs = str(g or "").strip()
            if gs and gs not in merged_gids:
                merged_gids.append(gs)
    if group_ids:
        for g in group_ids:
            gs = str(g or "").strip()
            if gs and gs not in merged_gids:
                merged_gids.append(gs)
    if group_id:
        gs = str(group_id).strip()
        if gs and gs not in merged_gids:
            merged_gids.append(gs)

    if merged_gids:
        extra["group_ids"] = merged_gids
        eff_group_id: str | None = merged_gids[0]
    else:
        extra.pop("group_ids", None)
        eff_group_id = None

    merged_stale_gids: list[str] = []
    if group_stale_ids is not None:
        for g in group_stale_ids:
            gs = str(g or "").strip()
            if gs and gs not in merged_stale_gids and (not merged_gids or gs in merged_gids):
                merged_stale_gids.append(gs)
    elif isinstance(extra.get("group_stale_ids"), (list, tuple)):
        for g in extra["group_stale_ids"]:
            gs = str(g or "").strip()
            if gs and gs not in merged_stale_gids and (not merged_gids or gs in merged_gids):
                merged_stale_gids.append(gs)
    elif bool(extra.get("group_stale")) and eff_group_id:
        merged_stale_gids.append(eff_group_id)

    extra.pop("group_stale", None)
    if merged_stale_gids and merged_gids:
        extra["group_stale_ids"] = [g for g in merged_stale_gids if g in merged_gids]
        if not extra["group_stale_ids"]:
            extra.pop("group_stale_ids", None)
    else:
        extra.pop("group_stale_ids", None)

    return {
        "user_id": uid,
        "ticker": raw_ticker,
        "name": name,
        "tier": tier,
        "market": market,
        "peer_group": peer_group,
        "peers": peers,
        "aliases": aliases,
        "earnings": earnings,
        "extra": extra,
        "source": src,
        "group_id": eff_group_id,
    }


def row_to_company(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a `dalton_coverage` DB row back into a `coverage.yaml` company dict losslessly."""
    ticker = str(row.get("ticker") or "").strip().upper()
    name = row.get("name") if row.get("name") is not None else ticker
    tier = normalize_tier(row.get("tier"), default="watch")
    market = row.get("market") if row.get("market") is not None else infer_market(ticker)
    peer_group = row.get("peer_group") if row.get("peer_group") is not None else ""
    peers = list(_ensure_json_obj(row.get("peers"), []))
    aliases = dict(_ensure_json_obj(row.get("aliases"), {}))
    earnings = dict(_ensure_json_obj(row.get("earnings"), {}))
    extra = dict(_ensure_json_obj(row.get("extra"), {}))

    dedicated_map: dict[str, Any] = {
        "ticker": ticker,
        "name": name,
        "tier": tier,
        "market": market,
        "peer_group": peer_group,
        "peers": peers,
        "aliases": aliases,
        "earnings": earnings,
    }

    result: dict[str, Any] = {}
    for key in CANONICAL_COMPANY_KEY_ORDER:
        if key in dedicated_map:
            result[key] = dedicated_map[key]
        elif key in extra:
            result[key] = extra[key]

    for key, val in extra.items():
        if (
            key not in result
            and key not in dedicated_map
            and key not in ("group_ids", "group_stale_ids", "group_stale")
            and not key.startswith("_")
        ):
            result[key] = val

    return result


def make_new_company_dict(
    ticker: str,
    *,
    name: str | None = None,
    tier: str = "watch",
    market: str | None = None,
    peer_group: str = "",
    has_active_workspace: bool = False,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a complete `coverage.yaml`-compatible company dict for newly added tickers."""
    sym = str(ticker or "").strip().upper()
    norm_tier = normalize_tier(tier, default="watch")
    display_name = str(name or sym).strip() or sym
    resolved_market = str(market or infer_market(sym)).strip() or infer_market(sym)

    search_names: list[str] = []
    for candidate in (display_name, sym):
        if candidate and candidate not in search_names:
            search_names.append(candidate)

    ws_status = "active" if has_active_workspace else ("planned" if resolved_market == "US" else "none")
    company: dict[str, Any] = {
        "ticker": sym,
        "name": display_name,
        "tier": norm_tier,
        "market": resolved_market,
        "peer_group": peer_group,
        "sheet_section": "Core" if norm_tier == "core" else "Watch",
        "peers": [],
        "etfs": [],
        "aliases": {
            "search_names": search_names,
            "adr": None,
            "earnings_tracker": sym,
        },
        "earnings": {
            "group": "A" if norm_tier == "core" else "B",
            "ir_url": None,
        },
        "workspace": {
            "name": sym,
            "status": ws_status,
        },
        "coverage_stage": "none",
        "note": None,
    }
    if extra_fields:
        for k, v in extra_fields.items():
            if k not in DEDICATED_COMPANY_KEYS:
                company[k] = v
    return company


def build_coverage_doc(
    companies: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    generated_at: str | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble a full `coverage.yaml` document dictionary from a list of company dicts."""
    core_count = sum(1 for c in companies if str(c.get("tier") or "").lower() == "core")
    watch_count = sum(1 for c in companies if str(c.get("tier") or "").lower() == "watch")
    return {
        "schema_version": 1,
        "as_of": as_of or date.today().isoformat(),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": list(sources) if sources is not None else [],
        "tiers": {
            "core": {
                "meaning": DEFAULT_TIER_MEANINGS["core"],
                "count": core_count,
            },
            "watch": {
                "meaning": DEFAULT_TIER_MEANINGS["watch"],
                "count": watch_count,
            },
        },
        "companies": companies,
    }


def dump_coverage_yaml(doc: dict[str, Any]) -> str:
    """Serialize a coverage document dictionary to canonical YAML text."""
    return yaml.safe_dump(
        doc,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )


def validate_earnings_calendar_ledger(
    ledger: Any,
    *,
    raw_bytes_len: int | None = None,
) -> dict[str, Any]:
    """Validate and normalize an `earnings-calendar.json` ledger payload (max 1 MB)."""
    if not isinstance(ledger, Mapping):
        raise ValueError("Ledger must be a JSON object")

    serialized = json.dumps(ledger, ensure_ascii=False).encode("utf-8")
    byte_count = raw_bytes_len if raw_bytes_len is not None else len(serialized)
    if byte_count > MAX_MIRROR_LEDGER_BYTES or len(serialized) > MAX_MIRROR_LEDGER_BYTES:
        raise ValueError(
            f"Ledger payload exceeds maximum allowed size of 1 MB ({MAX_MIRROR_LEDGER_BYTES} bytes)"
        )

    entries = ledger.get("entries")
    if entries is None or not isinstance(entries, Mapping):
        raise ValueError("Ledger 'entries' field is required and must be a JSON object")

    valid_statuses = {"confirmed", "pending", "reported", "scan_failed"}
    normalized_entries: dict[str, dict[str, Any]] = {}
    for raw_ticker, raw_entry in entries.items():
        ticker = str(raw_ticker or "").strip().upper()
        if not ticker:
            raise ValueError("Ledger entry key must be a non-empty ticker")
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"Ledger entry for {ticker} must be a JSON object")
        status = raw_entry.get("status")
        if status is not None and str(status) not in valid_statuses:
            raise ValueError(
                f"Invalid status {status!r} for {ticker}; expected one of {sorted(valid_statuses)}"
            )
        for date_field in ("date", "tentative_date"):
            d_val = raw_entry.get(date_field)
            if d_val is not None and d_val != "":
                if not isinstance(d_val, str) or len(d_val) < 10:
                    raise ValueError(
                        f"Invalid {date_field} {d_val!r} for {ticker}; expected YYYY-MM-DD"
                    )
                try:
                    date.fromisoformat(d_val[:10])
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid {date_field} {d_val!r} for {ticker}; expected YYYY-MM-DD"
                    ) from exc
        normalized_entries[ticker] = dict(raw_entry)

    reported = ledger.get("reported", [])
    if not isinstance(reported, list):
        raise ValueError("Ledger 'reported' field must be a list")
    normalized_reported: list[dict[str, Any]] = []
    for item in reported:
        if not isinstance(item, Mapping):
            raise ValueError("Each item in 'reported' must be a JSON object")
        normalized_reported.append(dict(item))

    updated = ledger.get("updated")
    if updated is not None and not isinstance(updated, str):
        raise ValueError("Ledger 'updated' field must be a string")

    out: dict[str, Any] = {
        "updated": updated or date.today().isoformat(),
        "entries": normalized_entries,
        "reported": normalized_reported,
    }
    for k, v in ledger.items():
        if k not in out:
            out[k] = v
    return out
