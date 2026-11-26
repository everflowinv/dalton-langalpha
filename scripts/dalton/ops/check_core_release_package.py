#!/usr/bin/env python3
"""Out-of-package Standalone Core release verification gate (§6.4b (2) #2).

Lives exclusively under `scripts/dalton/ops/` (physically excluded from Standalone Core
release exports via `DENY_PATHS`), keeping proprietary scan terms out of customer releases.

Enforces three gates:
  1. Zero case-insensitive proprietary-term hits across migrated public source directories
     (`src/dalton/research_host/` and the four public connector plugins).
  2. Exported Core release directory physically lacks all private & internal packaging paths
     (including `scripts/standalone/package_release.sh` and any `*.dalton.yaml`).
  3. Zero case-insensitive proprietary-term hits across migrated Core release artifacts
     (public host, 4 public plugins, `scripts/standalone/`, `deploy/standalone/`,
     `docker-compose.core.yml`, `.env.core.example`, `config.core.yaml`, `agent_config.core.yaml`),
     PLUS whole-export zero hits for newly migrated commercial host classes/domains.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Iterable

PROPRIETARY_SCAN_TERMS: tuple[str, ...] = (
    "guidepoint",
    "alphaengine",
    "researchdata_bridge",
    "everflowinv",
    "Proprietary",
    "clapi.guidepoint.io",
    "auth0.guidepoint.com",
    "LegacyBridgeAlphaEngineProxy",
    "GuidepointTokenManager",
    "GuidepointOwnerLock",
    "com.dalton.guidepoint-mcp-proxy",
)

WHOLE_PACKAGE_FORBIDDEN_COMMERCIAL_TERMS: tuple[str, ...] = (
    "clapi.guidepoint.io",
    "auth0.guidepoint.com",
    "LegacyBridgeAlphaEngineProxy",
    "GuidepointTokenManager",
    "GuidepointOwnerLock",
    "com.dalton.guidepoint-mcp-proxy",
)

PUBLIC_MIGRATED_SOURCE_DIRS: tuple[str, ...] = (
    "src/dalton/research_host",
    "plugins/dalton_company_wiki",
    "plugins/dalton_research_all",
    "plugins/dalton_sec",
    "plugins/dalton_macro",
)

EXPORT_FORBIDDEN_PATHS: tuple[str, ...] = (
    "plugins/dalton_data",
    "scripts/dalton/research_runtime",
    "scripts/dalton/researchdata_bridge.py",
    "scripts/dalton/ops",
    "scripts/dalton/launchd",
    "scripts/standalone/package_release.sh",
    "web/.env.dalton",
)

EXPORT_MIGRATED_TARGETS: tuple[str, ...] = (
    "src/dalton/research_host",
    "plugins/dalton_company_wiki",
    "plugins/dalton_research_all",
    "plugins/dalton_sec",
    "plugins/dalton_macro",
    "scripts/standalone",
    "deploy/standalone",
    "docker-compose.core.yml",
    ".env.core.example",
    "config.core.yaml",
    "agent_config.core.yaml",
)

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git", "node_modules", ".venv"})


@dataclass
class CheckReport:
    public_source_hits: list[dict[str, object]] = field(default_factory=list)
    forbidden_paths_present: list[str] = field(default_factory=list)
    export_migrated_hits: list[dict[str, object]] = field(default_factory=list)
    whole_export_commercial_hits: list[dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.public_source_hits
            or self.forbidden_paths_present
            or self.export_migrated_hits
            or self.whole_export_commercial_hits
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "public_source_hits": self.public_source_hits,
            "forbidden_paths_present": self.forbidden_paths_present,
            "export_migrated_hits": self.export_migrated_hits,
            "whole_export_commercial_hits": self.whole_export_commercial_hits,
        }


def _iter_text_files(root: Path, rel_targets: Iterable[str] | None = None) -> Iterable[Path]:
    if rel_targets is None:
        roots = [root]
    else:
        roots = [root / rel for rel in rel_targets if (root / rel).exists()]
    for target in roots:
        if target.is_file():
            yield target
            continue
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in p.parts):
                continue
            yield p


def _scan_files_for_terms(
    base_root: Path,
    files: Iterable[Path],
    terms: tuple[str, ...],
) -> list[dict[str, object]]:
    lowered_terms = [(term, term.casefold()) for term in terms]
    hits: list[dict[str, object]] = []
    for path in files:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="replace")
        rel = str(path.relative_to(base_root))
        for lineno, line in enumerate(text.splitlines(), 1):
            line_cf = line.casefold()
            for orig_term, needle in lowered_terms:
                if needle in line_cf:
                    hits.append(
                        {
                            "path": rel,
                            "line": lineno,
                            "term": orig_term,
                            "snippet": line.strip()[:160],
                        }
                    )
    return hits


def check_core_release_package(
    *,
    repo_root: Path | None = None,
    export_dir: Path | None = None,
) -> CheckReport:
    """Run all three Batch 3b Core release isolation checks."""
    report = CheckReport()

    if repo_root is not None:
        resolved_repo = Path(repo_root).expanduser().resolve()
        report.public_source_hits = _scan_files_for_terms(
            resolved_repo,
            _iter_text_files(resolved_repo, PUBLIC_MIGRATED_SOURCE_DIRS),
            PROPRIETARY_SCAN_TERMS,
        )

    if export_dir is not None:
        resolved_export = Path(export_dir).expanduser().resolve()
        for rel in EXPORT_FORBIDDEN_PATHS:
            if (resolved_export / rel).exists():
                report.forbidden_paths_present.append(rel)
        for dalton_yaml in sorted(resolved_export.rglob("*.dalton.yaml")):
            report.forbidden_paths_present.append(str(dalton_yaml.relative_to(resolved_export)))

        report.export_migrated_hits = _scan_files_for_terms(
            resolved_export,
            _iter_text_files(resolved_export, EXPORT_MIGRATED_TARGETS),
            PROPRIETARY_SCAN_TERMS,
        )
        report.whole_export_commercial_hits = _scan_files_for_terms(
            resolved_export,
            _iter_text_files(resolved_export, None),
            WHOLE_PACKAGE_FORBIDDEN_COMMERCIAL_TERMS,
        )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Out-of-package Standalone Core release verification gate")
    parser.add_argument("--repo-root", default=None, help="Repository root to check migrated public source dirs")
    parser.add_argument("--export-dir", default=None, help="Exported Standalone Core directory to check")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")
    parser.add_argument("--report-only", action="store_true", help="Print report and always exit 0")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser().resolve() if args.repo_root else None
    export_dir = Path(args.export_dir).expanduser().resolve() if args.export_dir else None
    if repo_root is None and export_dir is None:
        repo_root = Path(__file__).resolve().parents[3]

    report = check_core_release_package(repo_root=repo_root, export_dir=export_dir)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("=== Standalone Core Release Scan Report ===")
        print(f"status: {'PASS' if report.ok else 'FAIL'}")
        print(f"public_source_hits: {len(report.public_source_hits)}")
        for h in report.public_source_hits[:20]:
            print(f"  [public] {h['path']}:{h['line']} ({h['term']}): {h['snippet']}")
        print(f"forbidden_paths_present: {len(report.forbidden_paths_present)}")
        for p in report.forbidden_paths_present[:20]:
            print(f"  [forbidden_path] {p}")
        print(f"export_migrated_hits: {len(report.export_migrated_hits)}")
        for h in report.export_migrated_hits[:20]:
            print(f"  [export_migrated] {h['path']}:{h['line']} ({h['term']}): {h['snippet']}")
        print(f"whole_export_commercial_hits: {len(report.whole_export_commercial_hits)}")
        for h in report.whole_export_commercial_hits[:20]:
            print(f"  [whole_export] {h['path']}:{h['line']} ({h['term']}): {h['snippet']}")
        if args.report_only and export_dir is not None:
            resolved_export = export_dir.expanduser().resolve()
            info_hits = _scan_files_for_terms(
                resolved_export,
                _iter_text_files(resolved_export, None),
                PROPRIETARY_SCAN_TERMS,
            )
            print(f"informational_whole_export_hits: {len(info_hits)}")
            for h in info_hits[:40]:
                print(f"  [info] {h['path']}:{h['line']} ({h['term']}): {h['snippet']}")

    if args.report_only:
        return 0
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
