"""The audit report's 核验状态 line is honest about uncertain figures — R3a #3.

The production case: 1,100 figures, 0 verified, 1,088 uncertain, and the
sidecar said "核验状态：✅ 通过". `passed` keeps its blocking-only meaning
(uncertain is the PM's call); the rendering is what changes.
"""

from __future__ import annotations

from src.dalton.publish.audit import AuditResult, render_audit_md


def _format_audit_status(audit: AuditResult) -> str:
    """Imported per call so a reverted audit.py fails these tests on the
    rendered markdown (behaviour), not on module import."""
    from src.dalton.publish.audit import format_audit_status

    return format_audit_status(audit)


def _audit(
    *,
    figures: int,
    matched: int,
    unmatched: list[dict] | None = None,
    uncertain: list[dict] | None = None,
) -> AuditResult:
    return AuditResult(
        draft_sha256="a" * 64,
        figures=figures,
        matched=matched,
        exempt=0,
        unmatched=list(unmatched or []),
        uncertain=list(uncertain or []),
    )


def test_a_clean_pass_still_reads_as_one() -> None:
    audit = _audit(figures=3, matched=3)
    assert _format_audit_status(audit) == "✅ 通过"


def test_the_production_shape_reads_as_waiting_on_the_pm() -> None:
    """No blockers, 1,088 of 1,100 figures unjudged — the GOOGL draft. The
    line names the count and its share, and never says 通过."""
    audit = _audit(
        figures=1100,
        matched=12,
        uncertain=[{"id": f"u{i}", "line": 1, "raw": "1"} for i in range(1088)],
    )
    assert audit.passed is True  # the blocking semantics are untouched
    status = _format_audit_status(audit)
    assert status.startswith("⚠️")
    assert "1088 个数字（98%）" in status
    assert "待 PM 决定" in status


def test_one_uncertain_figure_is_enough_for_the_warning() -> None:
    """Any uncertain count above zero takes the warning shape — no ratio
    threshold that would green-wash a small-but-real gap."""
    audit = _audit(figures=100, matched=99, uncertain=[{"id": "u1", "line": 2, "raw": "7"}])
    assert _format_audit_status(audit) == (
        "⚠️ 无冲突，但 1 个数字（1%）未能核验，待 PM 决定"
    )


def test_a_blocking_audit_keeps_its_old_line() -> None:
    audit = _audit(
        figures=2,
        matched=0,
        unmatched=[{"id": "b1", "line": 3, "raw": "5", "reason": "unverified"}],
    )
    assert _format_audit_status(audit) == "❌ 存在未核实或冲突数字"


def test_the_markdown_carries_the_same_three_shapes() -> None:
    clean = render_audit_md(_audit(figures=1, matched=1), draft_path="d.md")
    assert "- **核验状态**：✅ 通过" in clean

    uncertain = render_audit_md(
        _audit(figures=10, matched=9, uncertain=[{"id": "u1", "line": 1, "raw": "3"}]),
        draft_path="d.md",
    )
    assert "⚠️ 无冲突，但 1 个数字（10%）未能核验，待 PM 决定" in uncertain
    assert "✅ 通过" not in uncertain

    blocked = render_audit_md(
        _audit(
            figures=1,
            matched=0,
            unmatched=[{"id": "b1", "line": 1, "raw": "2", "reason": "unverified"}],
        ),
        draft_path="d.md",
    )
    assert "- **核验状态**：❌ 存在未核实或冲突数字" in blocked
