"""Draft figure audit orchestration, sidecar `<slug>.audit.md` and `<slug>.annotated.md` rendering (PK.9)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from src.dalton.publish.match import MatchOutcome, match_figures
from src.dalton.publish.numbers import (
    ExtractedFigure,
    FigureLabels,
    extract_figures,
    normalize_period,
    parse_facts_ledger,
)

REASON_ZH: dict[str, str] = {
    "unverified": "未核实（无来源）",
    "no_source_found": "未找到匹配来源",
    "source_conflict": "数值与来源冲突（超容差）",
    "period_mismatch": "期间不匹配",
    "unit_mismatch": "单位或量级不匹配",
    "consensus_confusion": "一致预期口径混淆或缺少一致预期来源",
    "consensus_needs_provenance": "一致预期缺少 prov: 来源",
}


@dataclasses.dataclass
class AuditResult:
    """Structured output of a draft figure audit (PK.9)."""

    draft_sha256: str
    figures: int
    matched: int
    exempt: int
    unmatched: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    matches: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    outcomes: list[MatchOutcome] = dataclasses.field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.unmatched) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_sha256": self.draft_sha256,
            "figures": self.figures,
            "matched": self.matched,
            "exempt": self.exempt,
            "unmatched": self.unmatched,
            "matches": self.matches,
        }

    def summary_line(self, audit_path: str | None = None) -> str:
        arrow = f" → {audit_path}" if audit_path else ""
        return (
            f"{self.figures} figures: {self.matched} matched, "
            f"{self.exempt} exempt, {len(self.unmatched)} unverified{arrow}"
        )


async def maybe_label_prose_figures_with_llm(
    figures: list[ExtractedFigure],
    *,
    llm_service: Any = None,
    user_id: str | None = None,
    extract_model: str | None = None,
) -> None:
    """Optionally refine prose figure ``metric`` and ``period`` labels using ``LLMService.complete`` (PK.9).

    Strictly preserves the set of extracted figure IDs — the LLM cannot add or drop figures.
    """
    if llm_service is None:
        return
    prose_figs = [
        f for f in figures if not f.exempt and not f.location.get("table")
    ]
    if not prose_figs:
        return

    prompt_lines = [
        "Label each extracted figure below with its financial metric name and normalized period (e.g. 3Q26, FY2026).",
        "Return every figure id unchanged; do not add or omit any id.",
        "",
    ]
    for f in prose_figs:
        prompt_lines.append(
            f"- {f.id} (line {f.location.get('line')}): raw={f.raw!r} | sentence={f.location.get('sentence')!r}"
        )

    try:
        res = await llm_service.complete(
            user_id=user_id,
            system_prompt="You label extracted financial figures with metric and period.",
            user_prompt="\n".join(prompt_lines),
            response_schema=FigureLabels,
            slot="background",
            request_model=extract_model,
        )
        if isinstance(res, FigureLabels):
            expected_ids = {f.id for f in prose_figs}
            returned_map = {item.id: item for item in res.items}
            if set(returned_map.keys()) == expected_ids:
                for f in prose_figs:
                    lbl = returned_map[f.id]
                    if lbl.metric:
                        f.metric = lbl.metric
                    if lbl.period:
                        f.period = normalize_period(lbl.period) or f.period
    except Exception:
        # Fallback to deterministic labels if background LLM call fails
        return


def audit_draft(
    draft_markdown: str,
    *,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    facts_markdown: str | None = None,
    metric_aliases: dict[str, list[str]] | None = None,
) -> AuditResult:
    """Run deterministic figure extraction and source matching on ``draft_markdown`` (PK.9)."""
    draft_sha = hashlib.sha256(draft_markdown.encode("utf-8")).hexdigest()
    extracted = extract_figures(draft_markdown)
    facts_ledger = parse_facts_ledger(facts_markdown) if facts_markdown else {}

    outcomes = match_figures(
        extracted,
        thesis_doc=thesis_doc,
        models_doc=models_doc,
        model_cells=model_cells,
        provenance_records=provenance_records,
        facts_ledger=facts_ledger,
        metric_aliases=metric_aliases,
    )

    matched_list: list[dict[str, Any]] = []
    unmatched_list: list[dict[str, Any]] = []
    exempt_count = 0

    for out in outcomes:
        fig = out.figure
        if fig.exempt:
            exempt_count += 1
            continue
        if out.matched:
            matched_list.append(
                {
                    "id": fig.id,
                    "raw": fig.raw,
                    "line": fig.location.get("line", 0),
                    "metric": fig.metric,
                    "period": fig.period,
                    "kind": out.kind,
                    "ref": out.ref,
                    "excerpt": out.excerpt,
                    "source_sha256": out.source_sha256,
                }
            )
        else:
            unmatched_list.append(
                {
                    "id": fig.id,
                    "raw": fig.raw,
                    "line": fig.location.get("line", 0),
                    "metric": fig.metric,
                    "period": fig.period,
                    "reason": out.reason or "unverified",
                    "hint": out.hint,
                    "candidates": [c.to_dict() for c in out.candidates],
                }
            )

    return AuditResult(
        draft_sha256=draft_sha,
        figures=len(extracted),
        matched=len(matched_list),
        exempt=exempt_count,
        unmatched=unmatched_list,
        matches=matched_list,
        outcomes=outcomes,
    )


def render_audit_md(audit: AuditResult, *, draft_path: str = "") -> str:
    """Render the Chinese audit sidecar ``drafts/<slug>.audit.md`` sorted by line number (PK.9)."""
    lines = [
        f"# 数字核验报告（Draft Audit）：`{draft_path or 'draft'}`",
        "",
        f"- **草稿哈希**：`{audit.draft_sha256[:16]}…`",
        f"- **数字总数**：{audit.figures}（已匹配 {audit.matched} ｜ 豁免 {audit.exempt} ｜ 未核实 {len(audit.unmatched)}）",
        f"- **核验状态**：{'✅ 通过' if audit.passed else '❌ 存在未核实数字'}",
        "",
    ]

    if audit.unmatched:
        lines.append("## 未核实与冲突数字清单（按行号排序）")
        lines.append("")
        lines.append("| ID | 行号 | 原文数字 | 指标 | 期间 | 原因分类 | 候选来源与修复建议 |")
        lines.append("|---|---|---|---|---|---|---|")
        for item in sorted(audit.unmatched, key=lambda x: int(x.get("line") or 0)):
            r_code = str(item.get("reason") or "unverified")
            r_zh = REASON_ZH.get(r_code, r_code)
            cands = item.get("candidates") or []
            cand_str = (
                "；".join(
                    f"`{c['kind']}:{c['id']}` ({c['why']})" for c in cands
                )
                if cands
                else (item.get("hint") or "补充脚注来源")
            )
            lines.append(
                f"| **{item['id']}** | L{item['line']} | `{item['raw']}` | {item.get('metric') or '—'} "
                f"| {item.get('period') or '—'} | `{r_code}` ({r_zh}) | {cand_str} |"
            )
        lines.append("")

    if audit.matches:
        lines.append("## 已核实数字明细")
        lines.append("")
        lines.append("| ID | 行号 | 原文数字 | 指标 | 期间 | 来源类型 | 来源引用 |")
        lines.append("|---|---|---|---|---|---|---|")
        for m in sorted(audit.matches, key=lambda x: int(x.get("line") or 0)):
            lines.append(
                f"| {m['id']} | L{m['line']} | `{m['raw']}` | {m.get('metric') or '—'} "
                f"| {m.get('period') or '—'} | `{m.get('kind')}` | `{m.get('ref')}` |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def render_annotated_draft(draft_markdown: str, audit: AuditResult) -> str:
    """Render ``drafts/<slug>.annotated.md`` marking unmatched figures as ``**<raw>**⟨未核实⟩`` (PK.9)."""
    if not audit.unmatched:
        return draft_markdown

    by_line: dict[int, list[str]] = {}
    for u in audit.unmatched:
        lno = int(u.get("line") or 0)
        raw = str(u.get("raw") or "")
        if lno > 0 and raw:
            by_line.setdefault(lno, []).append(raw)

    out_lines: list[str] = []
    for idx, line in enumerate(draft_markdown.splitlines(), start=1):
        raws = by_line.get(idx)
        if raws:
            mod_line = line
            for r in raws:
                mod_line = mod_line.replace(r, f"**{r}**⟨未核实⟩", 1)
            out_lines.append(mod_line)
        else:
            out_lines.append(line)
    trailing = "\n" if draft_markdown.endswith("\n") else ""
    return "\n".join(out_lines) + trailing
