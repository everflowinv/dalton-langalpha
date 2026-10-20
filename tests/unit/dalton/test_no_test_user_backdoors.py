"""Lock the R4 security fix: no test-user special-casing in production code.

Real-run evidence scripts must obtain permissions, credentials and budget
headroom through configuration (pm_user_ids, per-user tokens, cap/floor
settings) — never through production branches keyed on test user IDs
(REVIEW-CHECKLIST-R4 §2.1 B1–B3, L38).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROD_DIRS = (_REPO_ROOT / "src" / "dalton", _REPO_ROOT / "plugins")
_BANNED_RE = re.compile(
    r"realrun-|"
    r"startswith\(\(?['\"](?:realrun|test-user)-|"
    r"(?:[!=]=|\bis(?:\s+not)?\b|\b(?:not\s+)?in\b)\s*(?:['\"]local-dev-user['\"]|LOCAL_DEV_USER_ID\b)|"
    r"(?:['\"]local-dev-user['\"]|LOCAL_DEV_USER_ID\b)\s*(?:[!=]=|\bis(?:\s+not)?\b|\b(?:not\s+)?in\b)"
)


def _is_banned_user_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "LOCAL_DEV_USER_ID":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "LOCAL_DEV_USER_ID":
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        val = node.value
        if val == "local-dev-user" or val.startswith(("realrun-", "test-user-")):
            return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_banned_user_expr(elt) for elt in node.elts)
    return False


def find_test_user_backdoor_offenders(source: str, rel_label: str = "<mem>") -> list[str]:
    offenders: list[str] = []
    seen_lines: set[int] = set()
    lines = source.splitlines()
    for lineno, line in enumerate(lines, start=1):
        if _BANNED_RE.search(line):
            offenders.append(f"{rel_label}:{lineno}: {line.strip()}")
            seen_lines.add(lineno)
    try:
        tree = ast.parse(source, filename=rel_label)
    except SyntaxError:
        return offenders
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_is_banned_user_expr(op) for op in operands):
                lineno = getattr(node, "lineno", 0)
                if lineno not in seen_lines:
                    snippet = lines[lineno - 1].strip() if 1 <= lineno <= len(lines) else "<ast.Compare>"
                    offenders.append(f"{rel_label}:{lineno}: {snippet}")
                    seen_lines.add(lineno)
    return offenders


def test_no_test_user_backdoor_branches_in_production_code() -> None:
    offenders: list[str] = []
    for root in _PROD_DIRS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = str(path.relative_to(_REPO_ROOT))
            offenders.extend(find_test_user_backdoor_offenders(text, rel))
    assert not offenders, (
        "Production code must not special-case test user IDs or local-dev-user "
        "(use user_id is None for system context, or configure pm_user_ids / per-user credentials):\n"
        + "\n".join(offenders)
    )


def test_backdoor_detector_catches_reversed_and_membership_comparisons() -> None:
    samples = [
        "if user_id == LOCAL_DEV_USER_ID: pass",
        "if LOCAL_DEV_USER_ID == user_id: pass",
        "if 'local-dev-user' != user_id: pass",
        "if user_id in (LOCAL_DEV_USER_ID, 'other'): pass",
        "if user_id is LOCAL_DEV_USER_ID: pass",
        "if LOCAL_DEV_USER_ID is not user_id: pass",
    ]
    for sample in samples:
        assert find_test_user_backdoor_offenders(sample), f"Expected detector to flag: {sample}"
    # Default parameter and BoolOp fallback remain allowed
    assert not find_test_user_backdoor_offenders("def f(user_id: str = LOCAL_DEV_USER_ID):\n    return user_id or LOCAL_DEV_USER_ID")


