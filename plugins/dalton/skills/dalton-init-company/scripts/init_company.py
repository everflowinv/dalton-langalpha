#!/usr/bin/env python3
"""Initialize or upgrade a Dalton company coverage workspace skeleton.

Standard library only. Can be executed directly inside a sandbox workspace or
imported by host-side scripts (`scripts/dalton/init_company_workspace.py`).
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
    market = str(entry.get("market") or ("HK" if norm_ticker.endswith(".HK") else ("CN" if norm_ticker.endswith((".SS", ".SZ")) else "US")))
    coverage_stage = str(entry.get("coverage_stage") or "none")
    as_of = str(entry.get("as_of") or "2026-10-09")

    peers_val = entry.get("peers")
    if isinstance(peers_val, list):
        peers_str = "、".join(str(p) for p in peers_val) if peers_val else "无"
    elif isinstance(peers_val, str) and peers_val.strip():
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
    if coverage_path is None or not coverage_path.is_file():
        return None
    text = coverage_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text) or {}
        as_of = doc.get("as_of", "2026-10-09")
        for comp in doc.get("companies") or []:
            if str(comp.get("ticker", "")).upper() == ticker.upper():
                entry = dict(comp)
                entry.setdefault("as_of", as_of)
                return entry
    except ImportError:
        pass
    return None


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

    files = render(args.ticker, entry, TEMPLATE_VERSION, init_date=init_date)
    result = apply(args.root, files, template_version=TEMPLATE_VERSION, dry_run=not args.apply)
    output = {k: v for k, v in result.items() if k != "manifest"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
