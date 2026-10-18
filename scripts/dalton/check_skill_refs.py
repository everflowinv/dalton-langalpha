#!/usr/bin/env python3
"""Report-only static checker for Dalton skills (plugins/dalton/skills/**).

Scans all .md and .py files under plugins/dalton/skills/ for:
1. Known deprecated/ghost tool or skill references.
2. Broken relative file references (references/*, templates/*, scripts/*).
3. Methodology 2-line header presence in SKILL.md files.

Always exits 0 (report-only, non-blocking).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills"

GHOST_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ghost_tool:x_search", re.compile(r"\bx_search\b")),
    ("ghost_tool:firecrawl__firecrawl_scrape", re.compile(r"firecrawl__firecrawl_scrape")),
    ("ghost_tool:dalton_sec.extract_facts", re.compile(r"\bextract_facts\b")),
    ("ghost_tool:dalton_sec.verify_numbers", re.compile(r"\bverify_numbers\b")),
    ("ghost_tool:dalton_sec.get_filing_metadata", re.compile(r"\bget_filing_metadata\b")),
    ("ghost_tool:dalton_sec.read_filing_section", re.compile(r"\bread_filing_section\b")),
    ("bad_param:knowledge_update_target", re.compile(r"knowledge_update\(\s*target\s*=")),
    ("bad_param:RunWorkflow_args", re.compile(r"RunWorkflow\([^)]*\bargs\s*=")),
    ("ghost_skill:dalton-initiate-full", re.compile(r"\bdalton-initiate-full\b")),
    ("ghost_skill:dalton-earnings-review", re.compile(r"\bdalton-earnings-review\b")),
]

REL_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])((?:references|templates|scripts)/[A-Za-z0-9_./-]+\.(?:md|py|json|yaml|sh))"
)

SECOND_HEADER_EXEMPT = {
    "dalton-conventions",
    "dalton-knowledge",
    "dalton-agenda",
    "dalton-research-director",
    "dalton-weekly-review",
    "dalton-financial-lenses",
    "dalton-excel-model",
    "dalton-init-company",
    "dalton-earnings-calendar",
    "dalton-publish",
}


def strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def scan_skills(skills_root: Path = SKILLS_ROOT) -> dict:
    ghost_hits: list[dict] = []
    broken_refs: list[dict] = []
    header_notes: list[dict] = []

    if not skills_root.exists():
        return {
            "ok": True,
            "skills_root": str(skills_root),
            "ghost_hits": [],
            "broken_refs": [],
            "header_notes": [],
        }

    for skill_dir in sorted(p for p in skills_root.iterdir() if p.is_dir()):
        skill_name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            body = strip_frontmatter(skill_md.read_text(encoding="utf-8"))
            first_lines = "\n".join(body.splitlines()[:12])
            has_line1 = "dalton-conventions/SKILL.md" in first_lines
            has_line2 = (
                "dalton-knowledge/SKILL.md" in first_lines
                and "dalton-agenda/SKILL.md" in first_lines
            )
            if not has_line1 and skill_name != "dalton-conventions":
                header_notes.append(
                    {
                        "skill": skill_name,
                        "file": str(skill_md.relative_to(REPO_ROOT)),
                        "issue": "missing_conventions_line_in_first_12_lines",
                    }
                )
            elif not has_line2 and skill_name not in SECOND_HEADER_EXEMPT:
                header_notes.append(
                    {
                        "skill": skill_name,
                        "file": str(skill_md.relative_to(REPO_ROOT)),
                        "issue": "missing_knowledge_agenda_line_in_first_12_lines",
                    }
                )

        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".py"}:
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            rel_file = str(path.relative_to(REPO_ROOT))

            for label, pattern in GHOST_PATTERNS:
                for match in pattern.finditer(text):
                    line_no = text.count("\n", 0, match.start()) + 1
                    ghost_hits.append(
                        {
                            "skill": skill_name,
                            "file": rel_file,
                            "line": line_no,
                            "rule": label,
                            "match": match.group(0),
                        }
                    )

            if path.suffix == ".md":
                for match in REL_REF_RE.finditer(text):
                    ref_rel = match.group(1)
                    if "<" in ref_rel or ">" in ref_rel:
                        continue
                    candidate = skill_dir / ref_rel
                    repo_candidate = REPO_ROOT / ref_rel
                    if not candidate.exists() and not repo_candidate.exists():
                        line_no = text.count("\n", 0, match.start()) + 1
                        broken_refs.append(
                            {
                                "skill": skill_name,
                                "file": rel_file,
                                "line": line_no,
                                "ref": ref_rel,
                            }
                        )

    return {
        "ok": True,
        "skills_scanned": len([p for p in skills_root.iterdir() if p.is_dir()]),
        "ghost_hits_count": len(ghost_hits),
        "broken_refs_count": len(broken_refs),
        "header_notes_count": len(header_notes),
        "ghost_hits": ghost_hits,
        "broken_refs": broken_refs,
        "header_notes": header_notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Report-only static skill reference checker.")
    parser.add_argument("--json", action="store_true", help="Output full JSON report.")
    args = parser.parse_args()

    report = scan_skills()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"[check_skill_refs] Scanned {report['skills_scanned']} skills: "
            f"ghost_hits={report['ghost_hits_count']}, "
            f"broken_refs={report['broken_refs_count']}, "
            f"header_notes={report['header_notes_count']}"
        )
        for item in report["ghost_hits"]:
            print(f"  [GHOST] {item['file']}:{item['line']} ({item['rule']}): {item['match']}")
        for item in report["broken_refs"]:
            print(f"  [BROKEN_REF] {item['file']}:{item['line']} -> {item['ref']}")
        for item in report["header_notes"]:
            print(f"  [HEADER] {item['file']}: {item['issue']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
