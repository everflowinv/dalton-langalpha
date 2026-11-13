#!/usr/bin/env python3
"""Initialize or upgrade a Dalton company coverage workspace skeleton.

Depends on ``jsonschema`` and ``referencing`` (Draft 2020-12). Schema files
live in ``references/schemas`` beside this skill, so a sandbox that only
receives the skill directory can still validate ``data/company.json``.
Host scripts may import this module
(``scripts/dalton/init_company_workspace.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

TEMPLATE_VERSION = "0.1"

MANAGED_BLOCK_START = "<!-- dalton:summary:start (由 knowledge_update 维护，勿手改) -->"
MANAGED_BLOCK_END = "<!-- dalton:summary:end -->"
EMPTY_MANAGED_BLOCK = f"\n{MANAGED_BLOCK_START}\n（知识层尚未建立）\n{MANAGED_BLOCK_END}\n"

# Default starter agent.md seeded by LangAlpha (src/server/services/computer_manager/_types.py)
SEEDED_AGENT_MD = """\
# Workspace Notes

<!--
This is a starter template. Replace these comments with real content
as you work. The system prompt has full guidelines on what to maintain.
-->

## Thread Index

## Key Findings

## File Index
"""

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
REPO_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_REPO_COVERAGE_YAML = REPO_ROOT / "config" / "dalton" / "coverage.yaml"

BUILTIN_TICKER_CIKS: dict[str, str] = {
    "META": "0001326801",
    "MSFT": "0000789019",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "GOOG": "0001652044",
    "AAPL": "0000320193",
    "NVDA": "0001045810",
    "TSLA": "0001318605",
    "AVGO": "0001730168",
    "ORCL": "0001341439",
    "CRM": "0001108524",
    "NFLX": "0001065280",
    "AMD": "0000002488",
    "INTC": "0000050863",
    "TSM": "0001046179",
    "BABA": "0001577552",
    "PDD": "0001737806",
    "JD": "0001549802",
    "BIDU": "0001329099",
    "DASH": "0001794589",
    "UBER": "0001543151",
    "SNOW": "0001640147",
    "PLTR": "0001321655",
    "SHOP": "0001594805",
    "SPOT": "0001639920",
    "APP": "0001751008",
    "CRWD": "0001535527",
    "PANW": "0001327567",
    "NOW": "0001373715",
}

TEMPLATE_FILE_MAP = {
    "agent.md": "agent.md.tmpl",
    "data/consensus/README.md": "data_consensus_README.md",
    "model/README.md": "model_README.md",
    "research/README.md": "research_README.md",
    "drafts/README.md": "drafts_README.md",
    "published/README.md": "published_README.md",
    "sources/README.md": "sources_README.md",
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_ticker_cik(ticker: str, entry: dict[str, Any] | None = None) -> str | None:
    """Resolve 10-digit SEC CIK from coverage entry or built-in map."""
    norm = ticker.strip().upper()
    if entry:
        raw = entry.get("cik") or (entry.get("identifiers") or {}).get("cik")
        if raw:
            s = str(raw).strip()
            if s.upper().startswith("CIK"):
                s = s[3:].strip()
            return s.lstrip("0").zfill(10) if s else None
    builtin = BUILTIN_TICKER_CIKS.get(norm)
    return builtin.zfill(10) if builtin else None


EXCHANGE_PENDING_NOTE = "交易所待核"


class CompanyDocError(ValueError):
    """Raised when ``data/company.json`` does not satisfy the company schema."""


class CompanySchemaUnavailable(Exception):
    """Raised when the bundled schema or jsonschema cannot be loaded.

    ``main`` turns this into ``company_schema_unavailable`` and writes nothing.
    """


# Shipped next to the skill. A synced sandbox does not contain ``src/``.
SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "references" / "schemas"
_BUNDLED_SCHEMA_NAMES = ("common", "company")


def infer_market(ticker: str, entry: dict[str, Any]) -> str:
    """Market code from the coverage entry, otherwise the ticker suffix."""
    raw = entry.get("market")
    if raw:
        return str(raw)
    if ticker.endswith(".HK"):
        return "HK"
    if ticker.endswith((".SS", ".SZ")):
        return "CN"
    return "US"


def resolve_exchange(ticker: str, entry: dict[str, Any], market: str) -> tuple[str, bool]:
    """Return ``(exchange, pending)``.

    Hong Kong, Shanghai and Shenzhen listings stay inferred from the market or
    the ticker suffix. A listing with no exchange in ``entry`` uses the schema
    value ``OTHER`` and ``pending`` is true.
    """
    raw = entry.get("exchange")
    if raw:
        return str(raw), False
    if market == "HK":
        return "HKEX", False
    if ticker.endswith(".SS"):
        return "SSE", False
    if ticker.endswith(".SZ"):
        return "SZSE", False
    return "OTHER", True


def _import_jsonschema():
    """Import Draft 2020-12 pieces. Failure refuses to write ``company.json``."""
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except Exception as exc:  # noqa: BLE001 - 沙箱缺少 jsonschema 时拒绝写出 company.json
        raise CompanySchemaUnavailable(
            "jsonschema import failed: " + str(exc)
        ) from exc
    return Draft202012Validator, FormatChecker, Registry, Resource


def _load_bundled_company_schema() -> tuple[dict[str, Any], Any]:
    """Load skill-local company/common schemas and a Draft 2020-12 validator.

    Registry keys are each file's ``$id``, the same resolution
    ``_load_schemas_and_registry`` uses for ``common#/$defs/...`` references.
    """
    Draft202012Validator, FormatChecker, Registry, Resource = _import_jsonschema()
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Any]] = []
    for name in _BUNDLED_SCHEMA_NAMES:
        path = SCHEMAS_DIR / f"{name}.schema.json"
        if not path.is_file():
            raise CompanySchemaUnavailable(
                f"missing schema file {name}.schema.json"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompanySchemaUnavailable(
                f"schema file {name}.schema.json is unreadable: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise CompanySchemaUnavailable(
                f"schema file {name}.schema.json is not an object"
            )
        try:
            Draft202012Validator.check_schema(raw)
        except Exception as exc:  # noqa: BLE001 - schema 自身不合法时拒绝写出 company.json
            raise CompanySchemaUnavailable(
                f"schema file {name}.schema.json failed draft 2020-12 check: {exc}"
            ) from exc
        schemas[name] = raw
        uri = raw.get("$id") or f"https://dalton.local/schemas/{name}"
        try:
            resources.append((str(uri), Resource.from_contents(raw)))
        except Exception as exc:  # noqa: BLE001 - e.g. no $schema to pick a draft; refuse to write
            raise CompanySchemaUnavailable(
                f"schema file {name}.schema.json cannot be registered: {exc}"
            ) from exc
    try:
        registry = Registry().with_resources(resources)
        validator = Draft202012Validator(
            schemas["company"],
            registry=registry,
            format_checker=FormatChecker(),
        )
    except Exception as exc:  # noqa: BLE001 - validator setup failure means no trustworthy check
        raise CompanySchemaUnavailable(f"schema validator setup failed: {exc}") from exc
    return schemas["company"], validator


def _one_liner_max_length() -> int:
    """``one_liner`` maxLength from the skill-local company schema."""
    company, _validator = _load_bundled_company_schema()
    props = company.get("properties") if isinstance(company, dict) else None
    one = props.get("one_liner") if isinstance(props, dict) else None
    max_len = one.get("maxLength") if isinstance(one, dict) else None
    if isinstance(max_len, int) and max_len > 0:
        return max_len
    raise CompanySchemaUnavailable("company schema is missing one_liner.maxLength")


def _company_one_liner(
    name: str,
    ticker: str,
    tier_display: str,
    *,
    exchange_pending: bool,
) -> str:
    """One-line description. A missing US exchange keeps the pending note."""
    suffix = f"（{EXCHANGE_PENDING_NOTE}）" if exchange_pending else ""
    limit = _one_liner_max_length()
    base = f"{name} ({ticker}) — {tier_display} coverage"
    if len(base) + len(suffix) <= limit:
        return base + suffix
    room = limit - len(suffix)
    if room <= 0:
        return suffix[:limit]
    return base[:room] + suffix


def _company_doc_problems(doc: dict[str, Any]) -> list[str]:
    """Validate ``doc`` with the skill-local Draft 2020-12 company schema."""
    _company, validator = _load_bundled_company_schema()
    try:
        errors = sorted(validator.iter_errors(doc), key=lambda err: list(err.path))
    except Exception as exc:  # noqa: BLE001 - $ref 解析失败时拒绝写出 company.json
        raise CompanySchemaUnavailable(
            "company schema could not be applied: " + str(exc)
        ) from exc
    problems: list[str] = []
    for err in errors:
        path = "/" + "/".join(str(part) for part in err.path) if err.path else "/"
        problems.append(f"{path}: {err.message}")
    return problems


def validate_company_doc(doc: dict[str, Any]) -> None:
    """Raise ``CompanyDocError`` when ``doc`` fails the company schema."""
    problems = _company_doc_problems(doc)
    if problems:
        raise CompanyDocError("; ".join(problems))


def render(
    ticker: str,
    coverage_entry: dict[str, Any] | None = None,
    template_version: str = TEMPLATE_VERSION,
    *,
    init_date: str | None = None,
) -> dict[str, str]:
    """Render all workspace skeleton files for `ticker`."""
    _ = template_version  # reserved for version-specific template branching
    norm_ticker = ticker.strip().upper()
    entry = coverage_entry or {}

    name = str(entry.get("name") or norm_ticker)
    tier_raw = str(entry.get("tier") or "watch").lower()
    tier_display = "Core" if tier_raw == "core" else ("Watch" if tier_raw == "watch" else tier_raw)
    market = infer_market(norm_ticker, entry)
    coverage_stage = str(entry.get("coverage_stage") or "none")
    as_of = str(entry.get("as_of") or "2026-10-09")
    cik = resolve_ticker_cik(norm_ticker, entry)

    peers_val = entry.get("peers")
    peers_list: list[str] = []
    if isinstance(peers_val, list):
        peers_list = [str(p).strip() for p in peers_val if str(p).strip()]
        peers_str = "、".join(peers_list) if peers_list else "无"
    elif isinstance(peers_val, str) and peers_val.strip():
        peers_list = [p.strip() for p in peers_val.split(",") if p.strip()]
        peers_str = peers_val.strip()
    else:
        peers_str = "无"

    rendered_date = init_date or date.today().isoformat()

    out: dict[str, str] = {}
    for relpath, tmpl_name in TEMPLATE_FILE_MAP.items():
        tmpl_path = TEMPLATES_DIR / tmpl_name
        raw = tmpl_path.read_text(encoding="utf-8")
        if relpath == "agent.md":
            content = raw.format(
                name=name,
                ticker=norm_ticker,
                tier=tier_display,
                peers=peers_str,
                market=market,
                coverage_stage=coverage_stage,
                as_of=as_of,
                date=rendered_date,
            )
        else:
            content = raw
        out[relpath] = content

    exchange, exchange_pending = resolve_exchange(norm_ticker, entry, market)
    stage_schema = coverage_stage if coverage_stage in ("onboarding", "initial", "full", "maintenance", "paused") else "onboarding"
    identifiers: dict[str, str] = {}
    if cik:
        identifiers["cik"] = cik
    company_doc: dict[str, Any] = {
        "schema_version": 1,
        "ticker": norm_ticker,
        "exchange": exchange,
        "name": name,
        "coverage_tier": "core" if tier_raw == "core" else "watch",
        "coverage_stage": stage_schema,
        "identifiers": identifiers,
        "peers": peers_list,
        "one_liner": _company_one_liner(
            name,
            norm_ticker,
            tier_display,
            exchange_pending=exchange_pending,
        ),
        "covered_since": rendered_date,
    }
    validate_company_doc(company_doc)
    out["data/company.json"] = json.dumps(company_doc, ensure_ascii=False, indent=2) + "\n"
    return out


def classify_file_action(
    relpath: str,
    target_content: str,
    current_content: str | None,
    recorded_sha: str | None,
    recorded_template_version: str | None,
    template_version: str = TEMPLATE_VERSION,
) -> tuple[str, str | None, str]:
    """Decide what action to take for a single skeleton file.

    Returns `(status, content_to_write_or_None, manifest_sha_to_record)`:
    - status in `{"written", "unchanged", "updated", "managed_block_appended", "skipped_modified"}`
    """
    target_sha = sha256_text(target_content)

    # 1. File does not exist yet
    if current_content is None:
        return "written", target_content, target_sha

    current_sha = sha256_text(current_content)

    # 2. Special case: agent.md is still LangAlpha's unmodified default seed
    if relpath == "agent.md" and current_content.strip() == SEEDED_AGENT_MD.strip():
        return "written", target_content, target_sha

    # 3. File matches the hash recorded in .dalton/init.json (analyst has not modified it)
    if recorded_sha is not None and current_sha == recorded_sha:
        if current_sha == target_sha and recorded_template_version == template_version:
            return "unchanged", None, target_sha
        return "updated", target_content, target_sha

    # 4. File was never in manifest, or its hash differs from manifest
    # If content happens to match target_content byte-for-byte, treat as unchanged
    if current_sha == target_sha:
        return "unchanged", None, target_sha

    # 5. Special case: modified agent.md missing the managed summary block markers
    if relpath == "agent.md" and "<!-- dalton:summary:start" not in current_content:
        appended = current_content.rstrip("\n") + "\n" + EMPTY_MANAGED_BLOCK
        # Keep recorded_sha as target_sha (not appended sha) so subsequent runs see it as modified
        # and have the managed block present -> skipped_modified.
        return "managed_block_appended", appended, recorded_sha or target_sha

    # 6. Analyst-modified file -> never overwrite
    return "skipped_modified", None, recorded_sha or target_sha


def apply(
    root: Path | str,
    files: dict[str, str],
    manifest: dict[str, Any] | None = None,
    *,
    template_version: str = TEMPLATE_VERSION,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotently apply rendered skeleton `files` onto directory `root`."""
    root_path = Path(root)
    manifest_path = root_path / ".dalton" / "init.json"

    existing_manifest: dict[str, Any] = {}
    if manifest is not None:
        existing_manifest = dict(manifest)
    elif manifest_path.is_file():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            existing_manifest = {}

    recorded_version = existing_manifest.get("template_version")
    recorded_files: dict[str, dict[str, str]] = dict(existing_manifest.get("files") or {})
    created_at = existing_manifest.get("created_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")

    new_manifest_files: dict[str, dict[str, str]] = dict(recorded_files)

    report: dict[str, Any] = {
        "root": str(root_path),
        "template_version": template_version,
        "dry_run": dry_run,
        "written": [],
        "updated": [],
        "unchanged": [],
        "managed_block_appended": [],
        "skipped_modified": [],
        "writes_count": 0,
    }

    for relpath, target_content in files.items():
        fpath = root_path / relpath
        current_content = fpath.read_text(encoding="utf-8") if fpath.is_file() else None
        rec_sha = (recorded_files.get(relpath) or {}).get("sha256")

        status, content_to_write, sha_for_manifest = classify_file_action(
            relpath=relpath,
            target_content=target_content,
            current_content=current_content,
            recorded_sha=rec_sha,
            recorded_template_version=recorded_version,
            template_version=template_version,
        )
        report[status].append(relpath)
        new_manifest_files[relpath] = {"sha256": sha_for_manifest}

        if content_to_write is not None:
            report["writes_count"] += 1
            if not dry_run:
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content_to_write, encoding="utf-8")

    new_manifest = {
        "template_version": template_version,
        "created_at": created_at,
        "files": new_manifest_files,
    }
    serialized_manifest = json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n"
    current_manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else None

    if current_manifest_text != serialized_manifest:
        report["writes_count"] += 1
        report["manifest_written"] = True
        if not dry_run:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(serialized_manifest, encoding="utf-8")
    else:
        report["manifest_written"] = False

    report["manifest"] = new_manifest
    return report


def load_coverage_entry(coverage_path: Path | None, ticker: str) -> dict[str, Any] | None:
    """Load a single ticker's entry from `coverage.yaml` (using PyYAML if available, or stdlib fallback)."""
    eff_path = coverage_path
    if (eff_path is None or not eff_path.is_file()) and DEFAULT_REPO_COVERAGE_YAML.is_file():
        eff_path = DEFAULT_REPO_COVERAGE_YAML
    if eff_path is None or not eff_path.is_file():
        cik = resolve_ticker_cik(ticker)
        return {"ticker": ticker.upper(), "cik": cik} if cik else None
    text = eff_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text) or {}
        as_of = doc.get("as_of", "2026-10-09")
        for comp in doc.get("companies") or []:
            if str(comp.get("ticker", "")).upper() == ticker.upper():
                entry = dict(comp)
                entry.setdefault("as_of", as_of)
                cik = resolve_ticker_cik(ticker, entry)
                if cik:
                    entry.setdefault("cik", cik)
                return entry
    except ImportError:
        pass
    cik = resolve_ticker_cik(ticker)
    return {"ticker": ticker.upper(), "cik": cik} if cik else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize Dalton company coverage workspace skeleton")
    parser.add_argument("--ticker", required=True, help="Canonical company ticker (e.g. META)")
    parser.add_argument("--root", type=Path, default=Path("."), help="Workspace root directory")
    parser.add_argument(
        "--coverage",
        type=Path,
        default=Path("../Home/dalton/coverage.yaml"),
        help="Path to coverage.yaml",
    )
    parser.add_argument("--name", help="Override company display name")
    parser.add_argument("--tier", help="Override tier (core|watch)")
    parser.add_argument("--market", help="Override market (US|HK|CN)")
    parser.add_argument("--cik", help="Override 10-digit SEC CIK")
    parser.add_argument("--peers", help="Comma-separated peer tickers override")
    parser.add_argument("--coverage-stage", help="Override coverage_stage")
    parser.add_argument("--apply", action="store_true", help="Write files to disk (default is dry-run)")
    args = parser.parse_args(argv)

    entry = load_coverage_entry(args.coverage, args.ticker) or {}
    if args.name:
        entry["name"] = args.name
    if args.tier:
        entry["tier"] = args.tier
    if args.market:
        entry["market"] = args.market
    if args.cik:
        entry["cik"] = str(args.cik).strip().lstrip("0").zfill(10)
    if args.peers is not None:
        entry["peers"] = [p.strip() for p in args.peers.split(",") if p.strip()]
    if args.coverage_stage:
        entry["coverage_stage"] = args.coverage_stage

    # Preserve created_at date on re-runs so agent.md's initialization date does not drift across days
    init_date: str | None = None
    manifest_file = args.root / ".dalton" / "init.json"
    if manifest_file.is_file():
        try:
            existing_m = json.loads(manifest_file.read_text(encoding="utf-8"))
            created = str(existing_m.get("created_at") or "")
            if len(created) >= 10:
                init_date = created[:10]
        except Exception:
            pass

    try:
        files = render(args.ticker, entry, TEMPLATE_VERSION, init_date=init_date)
    except CompanySchemaUnavailable as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "company_schema_unavailable",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1
    except CompanyDocError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "company_schema_invalid",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1
    result = apply(args.root, files, template_version=TEMPLATE_VERSION, dry_run=not args.apply)
    output = {k: v for k, v in result.items() if k != "manifest"}
    norm_ticker = args.ticker.strip().upper()
    _, exchange_pending = resolve_exchange(
        norm_ticker, entry, infer_market(norm_ticker, entry)
    )
    if exchange_pending:
        output["notes"] = [EXCHANGE_PENDING_NOTE]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

