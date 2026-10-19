#!/usr/bin/env python3
"""Scan development reports and PROGRESS.md for unverifiable backticked identifiers and missing evidence paths (P0I §2.2, L38).

Checks:
1. Backticked file paths (`src/...`, `web/...`, `scripts/...`, `docs/...`, `tests/...`, or `*.py`/`*.tsx`/`*.json` filenames) exist in the repository.
2. Backticked evidence paths under `var/` exist on disk (supporting `{a,b}` brace expansion and `*` globs).
3. Backticked code identifiers (function names, class/exception names, regex/constant names) exist in the repository's source tree (`src/`, `web/src/`, `scripts/`, `plugins/`, `tests/`, `integrations/`).

By default, reports findings and exits with status 0 ("只报告，不阻止", P0I §2.2).
Pass `--strict` to exit non-zero when missing claims are found.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SEARCH_DIRS = (
    "src",
    "web/src",
    "scripts",
    "plugins",
    "tests",
    "integrations",
    "config",
    "workflows",
)

SOURCE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".sh",
    ".json",
    ".yaml",
    ".yml",
    ".sql",
}

# Known data/domain/schema/enum tokens or CLI/config tokens that appear in backticks
# in reports and PROGRESS.md but are string values/keys rather than Python/TS symbols.
NON_SYMBOL_ALLOWLIST = frozenset(
    {
        # Audit / verdict / schema enum values and payload keys
        "verified",
        "contradicted",
        "unverified",
        "uncertain",
        "exempt",
        "incidental",
        "actual",
        "house_estimate",
        "consensus",
        "guidance",
        "assumption",
        "reported",
        "estimate",
        "derived",
        "supported",
        "pass",
        "revise",
        "fail",
        "active",
        "once",
        "skip",
        "wake",
        "done",
        "pending",
        "failed",
        "indeterminate",
        "in_flight",
        "conflict",
        "legacy_thesis",
        "broker_report",
        "internal_note",
        # Common config / payload / DB column / JSON keys referenced in prose
        "value_type",
        "extra_figures",
        "Raw_thesis_md",
        "raw_thesis_md",
        "Status",
        "status",
        "metric",
        "period",
        "unit",
        "owner",
        "origin",
        "agenda",
        "watch",
        "events",
        "response",
        "usage",
        "assist",
        "background",
        "compaction",
        "fetch",
        "lead",
        "reviewer",
        "contributor",
        "text_only",
        "byok",
        "oauth",
        "platform",
        # Upstream external table / library names mentioned as "non-existent table"
        "internal_documents",
        # Tickers / currencies / common env vars
        "BABA",
        "META",
        "GOOGL",
        "AMZN",
        "MSFT",
        "NVDA",
        "AAPL",
        "TSLA",
        "USD",
        "CNY",
        "HKD",
        "EUR",
        "JPY",
        "UTC",
        "HEAD",
        "NULL",
        "TRUE",
        "FALSE",
        "AND",
        "OR",
        "NOT",
    }
)

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^#{1,4}\s+(.*)$")
_ERRATA_HEADING_RE = re.compile(r"(勘误|errata|明确删掉或不做的内容)", re.IGNORECASE)

_FILE_EXT_RE = re.compile(
    r"^[A-Za-z0-9_.\-/{}*]+\.(?:py|tsx|ts|mjs|js|sh|json|yaml|yml|md|sql|toml|txt|log)$"
)
_REPO_PREFIX_RE = re.compile(
    r"^(?:src|web|scripts|plugins|config|docs|tests|integrations|workflows)/"
)
_CALL_EXPR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_REGEX_OR_CONST_RE = re.compile(r"^_?[A-Z][A-Z0-9_]{2,}$")
_CAMEL_CLASS_RE = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-zA-Z0-9]+)+$")
_SNAKE_IDENT_RE = re.compile(r"^_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
_TASK_OR_GATE_CODE_RE = re.compile(
    r"^(?:P\d+[A-Z0-9.\-]*|G\d+(?:\.\d+)?|H\d+|I\d+|A\d+|B\d+|L\d+|D\d+|E\d+|S\d+|R\d+[A-Za-z0-9\-]*|K\d+|N\d+|TS\d+|HTTP_\d+|V-\d+|F-\d+|E-\d+|FY\d{2,4}(?:Q[1-4]|H[12])?|CY\d{2,4}(?:Q[1-4]|H[12])?|YTD_[A-Z0-9]+|[1-4]Q\d{2,4}|[12]H\d{2,4})$"
)

SELF_EXCLUDED_REL_PATHS = frozenset(
    {
        "scripts/dalton/check_report_claims.py",
        "tests/unit/dalton/test_realrun_evidence_tools.py",
    }
)


def _expand_braces(pattern: str) -> list[str]:
    """Expand simple `{a,b,c}` brace expressions into a list of glob patterns."""
    m = re.search(r"\{([^{}]+)\}", pattern)
    if not m:
        return [pattern]
    prefix = pattern[: m.start()]
    suffix = pattern[m.end() :]
    choices = [c.strip() for c in m.group(1).split(",") if c.strip()]
    expanded: list[str] = []
    for choice in choices:
        expanded.extend(_expand_braces(f"{prefix}{choice}{suffix}"))
    return expanded


def _strip_code_comments(text: str, suffix: str) -> str:
    """Strip pure comment lines so commented-out names do not count as defined code symbols."""
    if suffix == ".py":
        out_lines: list[str] = []
        for ln in text.splitlines():
            s = ln.lstrip()
            if s.startswith("#") and "DALTON-HOOK" not in s:
                continue
            if "#" in ln and "DALTON-HOOK" not in ln:
                ln = ln.split("#", 1)[0]
            out_lines.append(ln)
        return "\n".join(out_lines)
    return text


def build_repo_index(repo_root: Path = REPO_ROOT) -> tuple[set[str], dict[str, list[str]], str]:
    """Index all files and source text in the repository for fast lookup."""
    rel_files: set[str] = set()
    basename_map: dict[str, list[str]] = {}
    corpus_chunks: list[str] = []

    for d in SEARCH_DIRS:
        base = repo_root / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(part in ("__pycache__", "node_modules", ".git", ".venv", "dist") for part in p.parts):
                continue
            rel = p.relative_to(repo_root).as_posix()
            rel_files.add(rel)
            basename_map.setdefault(p.name, []).append(rel)
            if p.suffix in SOURCE_EXTENSIONS:
                try:
                    raw_txt = p.read_text(encoding="utf-8", errors="ignore")
                    if rel in SELF_EXCLUDED_REL_PATHS:
                        raw_txt = "\n".join(
                            ln
                            for ln in raw_txt.splitlines()
                            if not any(
                                neg in ln
                                for neg in (
                                    "_METRIC_SYNONYM_GROUPS",
                                    "_LLM_JUDGE_SCHEMA",
                                    "JournalCashQueryError",
                                    "_CODE_FENCE_RE",
                                )
                            )
                        )
                    corpus_chunks.append(_strip_code_comments(raw_txt, p.suffix))
                except Exception:
                    pass

    docs_dir = repo_root / "docs"
    if docs_dir.exists():
        for p in docs_dir.rglob("*"):
            if p.is_file():
                rel = p.relative_to(repo_root).as_posix()
                rel_files.add(rel)
                basename_map.setdefault(p.name, []).append(rel)

    for top_p in repo_root.iterdir():
        if top_p.is_file():
            rel = top_p.relative_to(repo_root).as_posix()
            rel_files.add(rel)
            basename_map.setdefault(top_p.name, []).append(rel)

    return rel_files, basename_map, "\n".join(corpus_chunks)


def check_var_path_exists(raw_token: str, repo_root: Path = REPO_ROOT) -> tuple[bool, list[str]]:
    """Check whether a `var/...` path or glob pattern resolves to at least one existing file."""
    cleaned = raw_token.strip().rstrip(".,;:")
    patterns = _expand_braces(cleaned)
    matched: list[str] = []
    for pat in patterns:
        abs_pat = str(repo_root / pat)
        if "*" in pat or "?" in pat or "[" in pat:
            hits = glob.glob(abs_pat, recursive=True)
            for h in hits:
                hp = Path(h)
                if hp.exists():
                    matched.append(hp.relative_to(repo_root).as_posix())
        else:
            p = repo_root / pat
            if p.exists():
                matched.append(pat)
    return len(matched) > 0, matched


def check_repo_path_exists(
    raw_token: str,
    rel_files: set[str],
    basename_map: dict[str, list[str]],
    repo_root: Path = REPO_ROOT,
) -> bool:
    """Check whether a file path or bare filename exists in the repository."""
    cleaned = raw_token.strip().split("#", 1)[0].split(":", 1)[0].rstrip("/")
    if not cleaned:
        return True
    if "..." in cleaned or "…" in cleaned or "<" in cleaned or ">" in cleaned:
        return True
    if "*" in cleaned or "{" in cleaned:
        for pat in _expand_braces(cleaned):
            if glob.glob(str(repo_root / pat), recursive=True):
                return True
        return False
    if (repo_root / cleaned).exists() or cleaned in rel_files:
        return True
    if "/" not in cleaned and cleaned in basename_map:
        return True
    # Check suffix match e.g. `probes/internal.py` or `knowledge/service.py`
    suffix = "/" + cleaned
    for rf in rel_files:
        if rf.endswith(suffix):
            return True
    return False


def classify_and_extract_identifier(token: str) -> tuple[str | None, str | None]:
    """Classify a backticked token into `('var_path' | 'repo_path' | 'identifier', normalized)`."""
    t = token.strip()
    if not t:
        return None, None

    # Ignore shell commands, SQL snippets, URLs, flags, numbers, or expressions with spaces
    if t.startswith(("http://", "https://", "GET ", "POST ", "PUT ", "DELETE ", "PATCH ")):
        return None, None
    if t.startswith(("--", "$", "≥", "≤", "<=", ">=", "+", "-", "0 ", "*/", "30 ", "[")):
        return None, None

    # Extract `var/...` paths inside backticks
    if t.startswith("var/") or t.startswith("var\\"):
        return "var_path", t

    # Check if it's a CLI command containing a function call expression
    if " " in t:
        m_call = _CALL_EXPR_RE.match(t)
        if m_call and not t.startswith(("uv ", "pnpm ", "git ", "bash ", "node ", "cd ", "grep ")):
            cand = m_call.group(1)
            if cand not in NON_SYMBOL_ALLOWLIST:
                return "identifier", cand
        return None, None

    # Ignore `<placeholder>` or paths inside workspace/wiki runtime state
    if (
        "<" in t
        or ">" in t
        or t.startswith(("drafts/", "published/", "sources/", "Home/", "data/", "companies/", "/tmp/"))
    ):
        return None, None

    # Repo path or filename
    if _REPO_PREFIX_RE.match(t) or _FILE_EXT_RE.match(t):
        if re.match(r"^[a-z0-9\-]+\.\d+$", t):
            return None, None
        return "repo_path", t

    # Strip trailing `()` or `[]`
    if t.endswith("()"):
        t = t[:-2]
    elif t.endswith("[]"):
        t = t[:-2]

    if "::" in t:
        parts = [p for p in t.split("::") if p]
        t = parts[-1]

    if "." in t and not t.startswith("."):
        return None, None

    if t in NON_SYMBOL_ALLOWLIST or _TASK_OR_GATE_CODE_RE.match(t):
        return None, None

    # Hex git commit sha (7-40 hex chars)
    if re.fullmatch(r"[0-9a-f]{7,40}", t):
        return None, None

    # Regex/constant name
    if _REGEX_OR_CONST_RE.match(t):
        return "identifier", t

    # CamelCase class / exception / component
    if _CAMEL_CLASS_RE.match(t):
        return "identifier", t

    # Snake_case function / symbol
    if _SNAKE_IDENT_RE.match(t):
        return "identifier", t

    return None, None


def identifier_exists_in_corpus(ident: str, corpus: str, basename_map: dict[str, list[str]]) -> bool:
    """Return True if `ident` appears as a whole word in the repository source corpus or file basenames."""
    if f"{ident}.py" in basename_map or f"{ident}.tsx" in basename_map or f"{ident}.ts" in basename_map:
        return True
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])")
    return pattern.search(corpus) is not None


def _compute_file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_SHA256_HEX_RE = re.compile(r"\b([0-9a-f]{64})\b")
_VAR_JSON_PATH_RE = re.compile(r"(var/[^\s`|)]+?\.json)")


def scan_markdown_file(
    md_path: Path,
    *,
    rel_files: set[str],
    basename_map: dict[str, list[str]],
    corpus: str,
    repo_root: Path = REPO_ROOT,
    include_errata: bool = False,
) -> dict[str, Any]:
    """Scan a single Markdown file and return structured claim verification results."""
    from scripts.dalton.realrun.render_report_tables import verify_generated_evidence_block

    raw_text = md_path.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()
    missing_identifiers: list[dict[str, Any]] = []
    missing_repo_paths: list[dict[str, Any]] = []
    missing_var_paths: list[dict[str, Any]] = []
    sha256_mismatches: list[dict[str, Any]] = []
    generated_block_mismatches = verify_generated_evidence_block(raw_text, repo_root=repo_root)
    verified_count = 0

    # First pass: collect any 64-hex hashes mentioned in Errata / 勘误 sections so historical
    # reports with an appended errata section can acknowledge superseded hashes without modifying original prose.
    errata_acknowledged_hashes: set[str] = set()
    if not include_errata:
        in_err = False
        for line in lines:
            st = line.strip()
            mh = _HEADING_RE.match(st)
            if mh:
                in_err = bool(_ERRATA_HEADING_RE.search(mh.group(1)))
            if in_err:
                for hm in _SHA256_HEX_RE.finditer(line):
                    errata_acknowledged_hashes.add(hm.group(1))

    in_errata_section = False
    in_code_fence = False
    pending_summary_var_json: str | None = None

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue

        m_head = _HEADING_RE.match(stripped)
        if m_head:
            heading_text = m_head.group(1)
            in_errata_section = bool(_ERRATA_HEADING_RE.search(heading_text))
            pending_summary_var_json = None

        if in_errata_section and not include_errata:
            continue

        # Check var/*.json and sha256 pairings on the same line or adjacent summary metadata rows
        var_json_matches = _VAR_JSON_PATH_RE.findall(line)
        sha_matches = _SHA256_HEX_RE.findall(line)
        if var_json_matches and sha_matches:
            v_rel = var_json_matches[0]
            claimed_sha = sha_matches[0]
            v_abs = repo_root / v_rel
            if v_abs.is_file():
                actual_sha = _compute_file_sha256(v_abs)
                if actual_sha == claimed_sha or claimed_sha in errata_acknowledged_hashes:
                    verified_count += 1
                else:
                    sha256_mismatches.append(
                        {
                            "line": line_no,
                            "path": v_rel,
                            "claimed_sha256": claimed_sha,
                            "actual_sha256": actual_sha,
                        }
                    )
            pending_summary_var_json = None
        elif var_json_matches and "原始证据路径" in line:
            pending_summary_var_json = var_json_matches[0]
        elif sha_matches and pending_summary_var_json and "sha256" in line.lower():
            v_rel = pending_summary_var_json
            claimed_sha = sha_matches[0]
            v_abs = repo_root / v_rel
            if v_abs.is_file():
                actual_sha = _compute_file_sha256(v_abs)
                if actual_sha == claimed_sha or claimed_sha in errata_acknowledged_hashes:
                    verified_count += 1
                else:
                    sha256_mismatches.append(
                        {
                            "line": line_no,
                            "path": v_rel,
                            "claimed_sha256": claimed_sha,
                            "actual_sha256": actual_sha,
                        }
                    )
            pending_summary_var_json = None

        for m in _BACKTICK_RE.finditer(line):
            raw_token = m.group(1).strip()
            kind, norm = classify_and_extract_identifier(raw_token)
            if not kind or not norm:
                continue

            if kind == "var_path":
                ok, _ = check_var_path_exists(norm, repo_root=repo_root)
                if ok:
                    verified_count += 1
                else:
                    missing_var_paths.append(
                        {
                            "line": line_no,
                            "path": norm,
                            "context": stripped[:140],
                        }
                    )
            elif kind == "repo_path":
                ok = check_repo_path_exists(norm, rel_files, basename_map, repo_root=repo_root)
                if ok:
                    verified_count += 1
                else:
                    missing_repo_paths.append(
                        {
                            "line": line_no,
                            "path": norm,
                            "context": stripped[:140],
                        }
                    )
            elif kind == "identifier":
                ok = identifier_exists_in_corpus(norm, corpus, basename_map)
                if ok:
                    verified_count += 1
                else:
                    missing_identifiers.append(
                        {
                            "line": line_no,
                            "identifier": norm,
                            "raw": raw_token,
                            "context": stripped[:140],
                        }
                    )

    try:
        rel_doc = md_path.relative_to(repo_root).as_posix()
    except ValueError:
        rel_doc = md_path.as_posix()

    return {
        "file": rel_doc,
        "verified_claims": verified_count,
        "missing_identifiers": missing_identifiers,
        "missing_repo_paths": missing_repo_paths,
        "missing_var_paths": missing_var_paths,
        "sha256_mismatches": sha256_mismatches,
        "generated_block_mismatches": generated_block_mismatches,
    }


def scan_reports(
    paths: list[str | Path],
    *,
    repo_root: Path = REPO_ROOT,
    include_errata: bool = False,
) -> dict[str, Any]:
    rel_files, basename_map, corpus = build_repo_index(repo_root)
    reports: list[dict[str, Any]] = []
    total_missing_identifiers = 0
    total_missing_repo_paths = 0
    total_missing_var_paths = 0
    total_sha256_mismatches = 0
    total_generated_mismatches = 0

    for p in paths:
        fp = Path(p)
        if not fp.is_absolute():
            fp = repo_root / fp
        if not fp.exists():
            reports.append(
                {
                    "file": str(p),
                    "error": "file_not_found",
                    "verified_claims": 0,
                    "missing_identifiers": [],
                    "missing_repo_paths": [],
                    "missing_var_paths": [],
                    "sha256_mismatches": [],
                    "generated_block_mismatches": [],
                }
            )
            continue
        res = scan_markdown_file(
            fp,
            rel_files=rel_files,
            basename_map=basename_map,
            corpus=corpus,
            repo_root=repo_root,
            include_errata=include_errata,
        )
        total_missing_identifiers += len(res["missing_identifiers"])
        total_missing_repo_paths += len(res["missing_repo_paths"])
        total_missing_var_paths += len(res["missing_var_paths"])
        total_sha256_mismatches += len(res["sha256_mismatches"])
        total_generated_mismatches += len(res["generated_block_mismatches"])
        reports.append(res)

    return {
        "summary": {
            "files_scanned": len(reports),
            "missing_identifiers_count": total_missing_identifiers,
            "missing_repo_paths_count": total_missing_repo_paths,
            "missing_var_paths_count": total_missing_var_paths,
            "sha256_mismatches_count": total_sha256_mismatches,
            "generated_block_mismatches_count": total_generated_mismatches,
            "all_clean": (
                total_missing_identifiers == 0
                and total_missing_repo_paths == 0
                and total_missing_var_paths == 0
                and total_sha256_mismatches == 0
                and total_generated_mismatches == 0
            ),
        },
        "reports": reports,
    }


def format_text_report(result: dict[str, Any]) -> str:
    lines: list[str] = ["=== Dalton Report & PROGRESS Claim Check ==="]
    for rep in result.get("reports", []):
        lines.append(
            f"\nFile: {rep['file']} (verified={rep.get('verified_claims', 0)}, "
            f"missing_identifiers={len(rep.get('missing_identifiers', []))}, "
            f"missing_repo_paths={len(rep.get('missing_repo_paths', []))}, "
            f"missing_var_paths={len(rep.get('missing_var_paths', []))}, "
            f"sha256_mismatches={len(rep.get('sha256_mismatches', []))}, "
            f"generated_block_mismatches={len(rep.get('generated_block_mismatches', []))})"
        )
        for item in rep.get("missing_identifiers", []):
            lines.append(f"  [MISSING_IDENTIFIER] L{item['line']}: `{item['identifier']}`")
        for item in rep.get("missing_repo_paths", []):
            lines.append(f"  [MISSING_REPO_PATH]  L{item['line']}: `{item['path']}`")
        for item in rep.get("missing_var_paths", []):
            lines.append(f"  [MISSING_VAR_PATH]   L{item['line']}: `{item['path']}`")
        for item in rep.get("sha256_mismatches", []):
            lines.append(
                f"  [SHA256_MISMATCH]    L{item['line']}: `{item['path']}` "
                f"claimed={item['claimed_sha256'][:12]}... actual={item['actual_sha256'][:12]}..."
            )
        for item in rep.get("generated_block_mismatches", []):
            lines.append(
                f"  [GENERATED_MISMATCH] L{item['line']}: {item['reason']}"
            )

    s = result["summary"]
    lines.append(
        f"\nSummary: files={s['files_scanned']} "
        f"missing_identifiers={s['missing_identifiers_count']} "
        f"missing_repo_paths={s['missing_repo_paths_count']} "
        f"missing_var_paths={s['missing_var_paths_count']} "
        f"sha256_mismatches={s.get('sha256_mismatches_count', 0)} "
        f"generated_block_mismatches={s.get('generated_block_mismatches_count', 0)} "
        f"all_clean={s['all_clean']}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check backticked identifiers, var/ evidence paths, sha256 hashes, and generated tables in dev reports and PROGRESS.md."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Markdown files to scan (defaults to PROGRESS.md, trial-run summaries, and P0J-round4-dev-report.md)",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    parser.add_argument(
        "--include-errata",
        action="store_true",
        help="Also scan sections titled '勘误' / 'Errata'",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 if any missing identifier, path, or sha256/generated mismatch is found",
    )
    args = parser.parse_args(argv)

    target_files: list[str | Path] = list(args.files)
    if not target_files:
        default_candidates: list[str] = ["docs/dalton/dev-guide/PROGRESS.md"]
        p0j_rel = "docs/dalton/dev-guide/P0J-round4-dev-report.md"
        if (REPO_ROOT / p0j_rel).exists():
            default_candidates.append(p0j_rel)
        for g_md in sorted((REPO_ROOT / "docs/dalton/dev-guide/trial-runs").glob("realrun-*/G*.md")):
            default_candidates.append(g_md.relative_to(REPO_ROOT).as_posix())
        target_files = [c for c in default_candidates if (REPO_ROOT / c).exists()]

    result = scan_reports(
        target_files,
        repo_root=REPO_ROOT,
        include_errata=args.include_errata,
    )
    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_text_report(result) + "\n")

    if args.strict and not result["summary"]["all_clean"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
