"""Draft figure audit orchestration, sidecar `<slug>.audit.md` and `<slug>.annotated.md` rendering (PK.9 / G3.1)."""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any

from src.dalton.publish.match import (
    MatchOutcome,
    _RequiresModelCellOnly,
    match_figures,
    retrieve_candidate_sources,
    values_within_tolerance,
)
from src.dalton.publish.numbers import (
    ExtractedFigure,
    FigureLabels,
    extract_figures,
    maybe_label_prose_figures_with_llm as _label_figures_assist,
    normalize_period,
    parse_facts_ledger,
)

try:
    from src.dalton.llm.assist import assist_json
except ImportError:  # pragma: no cover
    assist_json = None  # type: ignore[assignment]

REASON_ZH: dict[str, str] = {
    "unverified": "未核实（无来源）",
    "no_source_found": "未找到匹配来源",
    "source_conflict": "数值与来源冲突（超容差）",
    "period_mismatch": "期间不匹配",
    "unit_mismatch": "单位或量级不匹配",
    "consensus_confusion": "一致预期口径混淆或缺少一致预期来源",
    "consensus_needs_provenance": "一致预期缺少 prov: 来源",
    "uncertain": "未能判断 — 交由 PM 决定",
}

CONTRADICTED_REASONS = frozenset(
    {
        "source_conflict",
        "period_mismatch",
        "unit_mismatch",
        "consensus_confusion",
        "consensus_needs_provenance",
    }
)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["supported", "contradicted", "unverified", "exempt"],
        },
        "matched_source_id": {"type": ["string", "null"]},
        "quote": {"type": ["string", "null"]},
        "source_value": {"type": ["number", "null"]},
        "source_unit": {"type": ["string", "null"]},
        "explanation": {"type": "string"},
    },
    "required": ["verdict", "explanation"],
    "additionalProperties": False,
}


@dataclasses.dataclass
class AuditResult:
    """Structured output of a draft figure audit (PK.9 / G3.1)."""

    draft_sha256: str
    figures: int
    matched: int
    exempt: int
    unmatched: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    matches: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    contradicted: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    unverified: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    uncertain: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    outcomes: list[MatchOutcome] = dataclasses.field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Return True when there are no blocking (`contradicted` or `unverified`) figures.

        `uncertain` items do NOT auto-reject; they are surfaced to PM on PublishApprovalCard.
        """
        return len(self.unmatched) == 0

    @property
    def verified(self) -> list[dict[str, Any]]:
        return self.matches

    @property
    def verified_count(self) -> int:
        return self.matched

    @property
    def contradicted_count(self) -> int:
        return len(self.contradicted)

    @property
    def unverified_count(self) -> int:
        return len(self.unverified)

    @property
    def uncertain_count(self) -> int:
        return len(self.uncertain)

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_sha256": self.draft_sha256,
            "figures": self.figures,
            "matched": self.matched,
            "verified": self.matched,
            "contradicted": len(self.contradicted),
            "unverified": len(self.unverified),
            "uncertain": len(self.uncertain),
            "exempt": self.exempt,
            "unmatched": self.unmatched,
            "matches": self.matches,
            "verified_items": self.matches,
            "contradicted_items": self.contradicted,
            "unverified_items": self.unverified,
            "uncertain_items": self.uncertain,
        }

    def summary_line(self, audit_path: str | None = None) -> str:
        arrow = f" → {audit_path}" if audit_path else ""
        unc_part = f", {len(self.uncertain)} uncertain" if self.uncertain else ""
        return (
            f"{self.figures} figures: {self.matched} matched, "
            f"{self.exempt} exempt, {len(self.unmatched)} unverified{unc_part}{arrow}"
        )


AuditReport = AuditResult


def verify_llm_judge_verdict(
    fig: ExtractedFigure,
    verdict_data: dict[str, Any],
    candidate_sources: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Deterministically re-verify an LLM `audit.judge` verdict (G3.1 step 4).

    A `'supported'` verdict is accepted ONLY if:
    1. `quote` is a verbatim substring of the referenced source record, AND
    2. `source_value` converted by `source_unit` matches `fig` within rounding tolerance
       (and the quote itself does not contain a conflicting unit/scale).
    """
    verdict = str(verdict_data.get("verdict") or "unverified").strip().lower()
    explanation = str(verdict_data.get("explanation") or "").strip()

    if verdict == "exempt":
        return "exempt", {"exempt_reason": explanation or "llm_exempt"}

    if verdict == "contradicted":
        return "contradicted", {
            "reason": "source_conflict",
            "hint": explanation or "LLM judge detected contradiction with source",
        }

    if verdict not in ("supported", "verified"):
        return "unverified", {
            "reason": "unverified",
            "hint": explanation or "未找到支持该数字的来源",
        }

    matched_sid = str(
        verdict_data.get("matched_source_id") or verdict_data.get("source_id") or ""
    ).strip()
    quote = str(verdict_data.get("quote") or "").strip()
    raw_val = verdict_data.get("source_value")
    src_unit = str(verdict_data.get("source_unit") or "").strip()

    if not matched_sid or not quote or raw_val is None or not src_unit:
        return "unverified", {
            "reason": "unverified",
            "hint": "LLM judge 'supported' verdict missing matched_source_id, quote, source_value, or source_unit",
        }

    # Find referenced source record
    target_src: dict[str, Any] | None = None
    norm_sid = matched_sid.replace("evidence:", "").replace("prov:", "").replace("model:", "").replace("f:", "").strip()
    for src in candidate_sources:
        s_id = str(src.get("source_id") or "").strip()
        s_ref = str(src.get("ref") or "").strip()
        if matched_sid in (s_id, s_ref) or norm_sid == s_id:
            target_src = src
            break

    if target_src is None:
        return "unverified", {
            "reason": "unverified",
            "hint": f"LLM judge cited source {matched_sid!r} not in candidate sources",
        }

    source_text = str(target_src.get("text") or "")
    # Requirement 1: quote must be a verbatim substring of the referenced source record
    if quote not in source_text:
        return "unverified", {
            "reason": "unverified",
            "hint": f"LLM judge quote {quote!r} is not a verbatim substring of source {matched_sid}",
        }

    try:
        src_val_f = float(raw_val)
    except (TypeError, ValueError):
        return "unverified", {
            "reason": "unverified",
            "hint": f"LLM judge source_value {raw_val!r} is non-numeric",
        }

    # Also guard against the quote itself having an explicit conflicting scale/unit (e.g. '百万美元' vs '亿美元')
    quote_figs = extract_figures(quote)
    for qf in quote_figs:
        _, q_unit_mis, _ = values_within_tolerance(fig, qf.value, qf.unit, qf.scale)
        if q_unit_mis:
            return "contradicted", {
                "reason": "unit_mismatch",
                "hint": f"来源引文 {quote!r} 单位 ({qf.unit}) 与正文 {fig.raw} ({fig.unit}) 不匹配",
            }

    # Requirement 2: source_value converted by source_unit matches draft figure within tolerance
    ok_val, unit_mis, _ = values_within_tolerance(fig, src_val_f, src_unit)
    if unit_mis:
        return "contradicted", {
            "reason": "unit_mismatch",
            "hint": f"单位/量级不匹配：正文为 {fig.raw} ({fig.unit})，来源 {matched_sid} 为 {src_val_f} {src_unit}",
        }
    if not ok_val:
        return "contradicted", {
            "reason": "source_conflict",
            "hint": f"数值与来源 {matched_sid} ({src_val_f} {src_unit}) 超出容差范围",
        }

    return "verified", {
        "kind": str(target_src.get("kind") or "evidence"),
        "ref": str(target_src.get("source_id") or matched_sid),
        "excerpt": quote,
    }


async def judge_figure_with_llm(
    fig: ExtractedFigure,
    candidate_sources: list[dict[str, Any]],
    *,
    user_id: str = "local-dev-user",
    llm_service: Any = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run grounded LLM source-consistency judgment (`audit.judge`) with deterministic re-verification (G3.1)."""
    if assist_json is None:
        return "uncertain", {
            "reason": "uncertain",
            "hint": "未能判断（assist_json 不可用）— 交由 PM 决定",
        }

    payload = {
        "figure": {
            "id": fig.id,
            "raw": fig.raw,
            "value": fig.value,
            "decimals": fig.decimals,
            "unit": fig.unit,
            "scale": fig.scale,
            "metric": fig.metric,
            "period": fig.period,
            "sentence": fig.location.get("sentence") or "",
            "line": fig.location.get("line") or 0,
        },
        "candidate_sources": candidate_sources[:5],
    }
    res = await assist_json(
        task="audit.judge",
        instructions=(
            "Judge whether the draft figure is supported, contradicted, unverified, or exempt "
            "based ONLY on the provided candidate_sources. If supported, `quote` MUST be an exact "
            "verbatim substring from the matched source's `text`, and `source_value`/`source_unit` "
            "must reflect the exact numeric value and canonical unit in that source."
        ),
        payload=payload,
        schema=JUDGE_SCHEMA,
        user_id=user_id,
        llm_service=llm_service,
        workspace_id=workspace_id,
        thread_id=thread_id,
    )
    if not res.ok or not isinstance(res.data, dict):
        return "uncertain", {
            "reason": "uncertain",
            "hint": f"未能判断（{res.error or 'assist_failed'}）— 交由 PM 决定",
        }
    return verify_llm_judge_verdict(fig, res.data, candidate_sources)


async def maybe_label_prose_figures_with_llm(
    figures: list[ExtractedFigure],
    *,
    markdown_text: str = "",
    llm_service: Any = None,
    user_id: str | None = None,
    extract_model: str | None = None,
) -> list[ExtractedFigure]:
    """Refine prose figure labels using `assist_json(task='audit.label_figures')` or `llm_service`."""
    if markdown_text:
        return await _label_figures_assist(
            markdown_text,
            figures,
            user_id=user_id or "local-dev-user",
            llm_service=llm_service,
        )
    if llm_service is None:
        return figures
    prose_figs = [
        f for f in figures if not f.exempt and not f.location.get("table")
    ]
    if not prose_figs:
        return figures

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
        return figures
    return figures


def _build_audit_result(
    draft_sha: str,
    extracted: list[ExtractedFigure],
    outcomes: list[MatchOutcome],
    uncertain_list: list[dict[str, Any]] | None = None,
) -> AuditResult:
    matched_list: list[dict[str, Any]] = []
    contradicted_list: list[dict[str, Any]] = []
    unverified_list: list[dict[str, Any]] = []
    unc_list: list[dict[str, Any]] = list(uncertain_list or [])
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
        elif out.reason == "uncertain":
            unc_list.append(
                {
                    "id": fig.id,
                    "raw": fig.raw,
                    "line": fig.location.get("line", 0),
                    "metric": fig.metric,
                    "period": fig.period,
                    "reason": "uncertain",
                    "hint": out.hint or "未能判断 — 交由 PM 决定",
                    "sentence": fig.location.get("sentence", ""),
                    "candidates": [c.to_dict() for c in out.candidates],
                }
            )
        else:
            reason_code = out.reason or "unverified"
            entry = {
                "id": fig.id,
                "raw": fig.raw,
                "line": fig.location.get("line", 0),
                "metric": fig.metric,
                "period": fig.period,
                "reason": reason_code,
                "hint": out.hint,
                "sentence": fig.location.get("sentence", ""),
                "candidates": [c.to_dict() for c in out.candidates],
            }
            if reason_code in CONTRADICTED_REASONS:
                contradicted_list.append(entry)
            else:
                unverified_list.append(entry)

    unmatched_blocking = contradicted_list + unverified_list
    return AuditResult(
        draft_sha256=draft_sha,
        figures=len(extracted),
        matched=len(matched_list),
        exempt=exempt_count,
        unmatched=unmatched_blocking,
        matches=matched_list,
        contradicted=contradicted_list,
        unverified=unverified_list,
        uncertain=unc_list,
        outcomes=outcomes,
    )


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
    """Run deterministic figure extraction and source matching on ``draft_markdown`` (PK.9 / G3.1)."""
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
    return _build_audit_result(draft_sha, extracted, outcomes)


async def aaudit_draft(
    draft_markdown: str,
    *,
    thesis_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    model_cells: dict[str, dict[str, Any]] | None = None,
    provenance_records: dict[str, dict[str, Any]] | None = None,
    facts_markdown: str | None = None,
    metric_aliases: dict[str, list[str]] | None = None,
    use_llm_judge: bool = True,
    user_id: str = "local-dev-user",
    llm_service: Any = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> AuditResult:
    """Async figure audit with hybrid extraction + grounded `audit.judge` + deterministic re-verification (G3.1)."""
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

    if use_llm_judge:
        for idx, out in enumerate(outcomes):
            fig = out.figure
            if fig.exempt or out.matched:
                continue
            # Hard deterministic rules (target price requiring model cell, explicit contradiction)
            # are never overridden by LLM judge
            if _RequiresModelCellOnly(fig) or out.reason in CONTRADICTED_REASONS:
                continue
            cand_sources = retrieve_candidate_sources(
                fig,
                thesis_doc=thesis_doc,
                models_doc=models_doc,
                model_cells=model_cells,
                provenance_records=provenance_records,
                facts_ledger=facts_ledger,
                metric_aliases=metric_aliases,
                limit=5,
            )
            if not cand_sources:
                continue
            bucket, detail = await judge_figure_with_llm(
                fig,
                cand_sources,
                user_id=user_id,
                llm_service=llm_service,
                workspace_id=workspace_id,
                thread_id=thread_id,
            )
            if bucket == "verified":
                outcomes[idx] = MatchOutcome(
                    figure=fig,
                    matched=True,
                    kind=detail.get("kind", "evidence"),
                    ref=detail.get("ref", ""),
                    excerpt=detail.get("excerpt", ""),
                )
            elif bucket == "exempt":
                fig.exempt = True
                fig.exempt_reason = detail.get("exempt_reason", "llm_exempt")
                outcomes[idx] = MatchOutcome(
                    figure=fig,
                    matched=True,
                    kind="exempt",
                    ref=fig.exempt_reason,
                )
            elif bucket == "contradicted":
                outcomes[idx] = MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason=detail.get("reason", "source_conflict"),
                    hint=detail.get("hint", ""),
                    candidates=out.candidates,
                )
            elif bucket == "uncertain":
                outcomes[idx] = MatchOutcome(
                    figure=fig,
                    matched=False,
                    reason="uncertain",
                    hint=detail.get("hint", "未能判断 — 交由 PM 决定"),
                    candidates=out.candidates,
                )

    return _build_audit_result(draft_sha, extracted, outcomes)


def render_audit_md(audit: AuditResult, *, draft_path: str = "") -> str:
    """Render the Chinese audit sidecar ``drafts/<slug>.audit.md`` sorted by line number (PK.9 / G3.1)."""
    lines = [
        f"# 数字核验报告（Draft Audit）：`{draft_path or 'draft'}`",
        "",
        f"- **草稿哈希**：`{audit.draft_sha256[:16]}…`",
        (
            f"- **数字总数**：{audit.figures}（已核实 {audit.matched} ｜ 冲突 {len(audit.contradicted)} "
            f"｜ 未核实 {len(audit.unverified)} ｜ 未能判断 {len(audit.uncertain)} ｜ 豁免 {audit.exempt}）"
        ),
        f"- **核验状态**：{'✅ 通过' if audit.passed else '❌ 存在未核实或冲突数字'}",
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

    if audit.uncertain:
        lines.append("## 未能判断 — 交由 PM 决定（不自动拦截）")
        lines.append("")
        lines.append("| ID | 行号 | 原文数字 | 指标 | 期间 | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for item in sorted(audit.uncertain, key=lambda x: int(x.get("line") or 0)):
            lines.append(
                f"| **{item['id']}** | L{item['line']} | `{item['raw']}` | {item.get('metric') or '—'} "
                f"| {item.get('period') or '—'} | {item.get('hint') or '未能判断 — 交由 PM 决定'} |"
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

