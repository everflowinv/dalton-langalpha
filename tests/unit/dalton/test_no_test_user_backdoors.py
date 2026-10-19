"""Lock the R4 security fix: no test-user special-casing in production code.

Real-run evidence scripts must obtain permissions, credentials and budget
headroom through configuration (pm_user_ids, per-user tokens, cap/floor
settings) — never through production branches keyed on test user IDs
(REVIEW-CHECKLIST-R4 §2.1 B1–B3, L38).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROD_DIRS = (_REPO_ROOT / "src" / "dalton", _REPO_ROOT / "plugins")
_BANNED = re.compile(r'realrun-|startswith\(\(?["\'](?:realrun|test-user)-')


def test_no_test_user_backdoor_branches_in_production_code() -> None:
    offenders: list[str] = []
    for root in _PROD_DIRS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _BANNED.search(line):
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Production code must not special-case test user IDs "
        "(configure pm_user_ids / per-user credentials instead):\n" + "\n".join(offenders)
    )
