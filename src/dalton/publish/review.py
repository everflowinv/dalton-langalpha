"""Server-side independent review using a distinct model family (PK.10)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.dalton.settings import REPO_ROOT, get_dalton_config

logger = logging.getLogger(__name__)

REVIEWER_AGENT_PATH = REPO_ROOT / "plugins" / "dalton" / "agents" / "dalton-reviewer.md"


class ReviewCheckItem(BaseModel):
    """Single check item (1..7) from the independent reviewer."""

    check: int = Field(ge=1, le=7)
    result: Literal["pass", "revise"]
    reason: str
    lines: list[int] = Field(default_factory=list)


class ReviewVerdict(BaseModel):
    """Structured output schema for ``dalton-reviewer`` (PK.10)."""

    verdict: Literal["pass", "revise"]
    summary: str
    items: list[ReviewCheckItem] = Field(default_factory=list)
    must_fix: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)


@dataclasses.dataclass
class ReviewBundle:
    """Inputs assembled for independent review."""

    title: str
    kind: str
    draft_markdown: str
    audit_summary: dict[str, Any]
    thesis_md: str = ""
    debate_md: str = ""
    management_md: str = ""


@dataclasses.dataclass
class ReviewResult:
    """Authoritative server-side review result (PK.10)."""

    status: Literal["done", "unavailable"]
    model: str | None = None
    family: str | None = None
    verdict: ReviewVerdict | None = None
    usage: dict[str, Any] | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.status != "done" or self.verdict is None:
            return {
                "status": self.status,
                "model": self.model,
                "family": self.family,
                "verdict": "unavailable",
                "reason": self.reason or "No cross-family reviewer model available",
            }
        effective_verdict = (
            "revise" if self.verdict.must_fix else self.verdict.verdict
        )
        points = list(self.verdict.must_fix) + [
            f"[建议] {x}" for x in self.verdict.nice_to_have
        ]
        if not points and self.verdict.summary:
            points = [self.verdict.summary]
        return {
            "status": "done",
            "model": self.model,
            "family": self.family,
            "verdict": effective_verdict,
            "summary": self.verdict.summary,
            "items": [i.model_dump() for i in self.verdict.items],
            "must_fix": list(self.verdict.must_fix),
            "nice_to_have": list(self.verdict.nice_to_have),
            "reasons": list(self.verdict.must_fix) or [self.verdict.summary],
            "points": points,
            "usage": self.usage or {},
            "reviewed_at": (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            ),
        }


def family_of(model_key: str | None) -> str:
    """Determine the model family of ``model_key`` (delegating to P0.6 ``role_policy.family_of`` if present)."""
    if not model_key or not str(model_key).strip():
        return "unknown"
    try:
        from src.dalton.llm.role_policy import family_of as _role_family_of

        fam = _role_family_of(model_key)
        if fam and str(fam) != "unknown":
            return str(fam)
    except Exception:
        pass

    key = str(model_key).strip().lower()
    if any(t in key for t in ("claude", "anthropic", "opus", "sonnet", "haiku")):
        return "anthropic-claude"
    if any(t in key for t in ("gpt", "openai", "codex", "o1", "o3", "o4")):
        return "openai-gpt"
    if any(t in key for t in ("gemini", "google")):
        return "google-gemini"
    if any(t in key for t in ("grok", "xai")):
        return "xai-grok"
    if "deepseek" in key:
        return "deepseek"
    if any(t in key for t in ("glm", "z-ai", "zhipu")):
        return "zhipu-glm"
    if "qwen" in key:
        return "alibaba-qwen"
    if "kimi" in key or "moonshot" in key:
        return "moonshot-kimi"
    return "unknown"


def pick_reviewer_model(
    author_model: str | None,
    *,
    candidates: list[str] | None = None,
    available_models: set[str] | None = None,
) -> str | None:
    """Pick the first candidate reviewer model whose family differs from ``author_model`` (PK.10 / G3.2).

    Refuses selection (returns ``None``) if ``author_model`` is ``None`` or ``family_of(author_model) == 'unknown'``.
    """
    author_family = family_of(author_model)
    if not author_model or author_family == "unknown":
        return None

    candidate_list = (
        list(candidates)
        if candidates is not None
        else list(get_dalton_config().publish.reviewer_models)
    )
    if available_models is not None:
        candidate_list = [c for c in candidate_list if c in available_models]

    try:
        from src.dalton.llm.role_policy import select_independent_reviewer

        chosen = select_independent_reviewer(
            str(author_model),
            candidate_list,
            require_tool_calling=False,
        )
        if chosen and family_of(chosen) not in ("unknown", author_family):
            return chosen
    except Exception:
        pass

    for cand in candidate_list:
        cand_family = family_of(cand)
        if cand_family != "unknown" and cand_family != author_family:
            return cand
    return None


def load_agent_body(agent_name: str = "dalton-reviewer") -> str:
    """Load the system prompt body from ``plugins/dalton/agents/<agent_name>.md``."""
    path = REVIEWER_AGENT_PATH if agent_name == "dalton-reviewer" else (
        REPO_ROOT / "plugins" / "dalton" / "agents" / f"{agent_name}.md"
    )
    if not path.exists():
        return "你是基金的独立审稿人。只读审稿，输出严格 JSON（verdict: pass|revise, summary, items, must_fix, nice_to_have）。"
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return raw.strip()


def render_review_input(bundle: ReviewBundle, *, max_chars: int = 120_000) -> str:
    """Assemble the numbered draft, audit summary, thesis, debate, and management context for review."""
    numbered_draft = "\n".join(
        f"{idx:4d}: {line}"
        for idx, line in enumerate(bundle.draft_markdown.splitlines(), start=1)
    )
    mgmt = bundle.management_md
    debate = bundle.debate_md
    thesis = bundle.thesis_md

    # Progressive context trimming if exceeding 60% context budget (PK.10):
    # management.md -> debate.md -> draft tail
    total_len = len(numbered_draft) + len(thesis) + len(debate) + len(mgmt)
    if total_len > max_chars:
        mgmt = ""
        total_len = len(numbered_draft) + len(thesis) + len(debate)
    if total_len > max_chars:
        debate = debate[: max(0, max_chars - len(numbered_draft) - len(thesis))]

    sections = [
        f"# 待审报告：《{bundle.title}》（类型：{bundle.kind}）",
        "",
        "## 数字核验摘要",
        f"总数字：{bundle.audit_summary.get('figures', 0)}，"
        f"已匹配：{bundle.audit_summary.get('matched', 0)}，"
        f"豁免：{bundle.audit_summary.get('exempt', 0)}，"
        f"未核实：{len(bundle.audit_summary.get('unmatched') or [])}",
        "",
        "## 报告正文（带行号）",
        numbered_draft,
    ]
    if thesis:
        sections.extend(["", "## 当前 Thesis（thesis.md）", thesis])
    if debate:
        sections.extend(["", "## 当前 Top Debates（debate.md）", debate])
    if mgmt:
        sections.extend(["", "## 管理层与指引风格（management.md）", mgmt])

    return "\n".join(sections)


async def run_review(
    *,
    user_id: str | None,
    author_model: str | None,
    bundle: ReviewBundle,
    llm_service: Any = None,
    candidates: list[str] | None = None,
    available_models: set[str] | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> ReviewResult:
    """Run authoritative server-side independent review via ``dalton_complete`` / ``LLMService.complete`` (PK.10 / G3.2)."""
    author_family = family_of(author_model)
    if not author_model or author_family == "unknown":
        return ReviewResult(
            status="unavailable",
            reason=(
                f"无法发布：作者模型（{author_model or 'None'}）家族未知（unknown），"
                "无法保证跨家族独立审稿"
            ),
        )

    model = pick_reviewer_model(
        author_model,
        candidates=candidates,
        available_models=available_models,
    )
    if not model or family_of(model) == author_family:
        return ReviewResult(
            status="unavailable",
            reason=(
                f"无法发布：缺少与作者模型（{author_model} / "
                f"{author_family}）不同家族的可用审稿模型"
            ),
        )

    system_prompt = load_agent_body("dalton-reviewer")
    user_prompt = render_review_input(bundle)

    try:
        if llm_service is None:
            from src.dalton.llm.complete import dalton_complete

            comp = await dalton_complete(
                user_id=user_id or "local-dev-user",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                purpose="publish.review",
                request_model=model,
                response_schema=ReviewVerdict,
                slot="primary",
                reasoning_effort="high",
                timeout_s=300.0,
                workspace_id=workspace_id,
                thread_id=thread_id,
                raise_on_error=False,
            )
            if not comp.ok:
                return ReviewResult(
                    status="unavailable",
                    model=model,
                    family=family_of(model),
                    reason=f"独立审稿调用失败：{comp.error or comp.status}",
                )
            verdict_obj = comp.data
            usage = comp.usage
        else:
            out = await asyncio.wait_for(
                llm_service.complete(
                    user_id=user_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_schema=ReviewVerdict,
                    request_model=model,
                    slot="primary",
                    reasoning_effort="high",
                    return_token_usage=True,
                ),
                timeout=300.0,
            )
            if isinstance(out, tuple) and len(out) == 2:
                verdict_obj, usage = out
            else:
                verdict_obj, usage = out, {}

        if isinstance(verdict_obj, dict):
            verdict_obj = ReviewVerdict.model_validate(verdict_obj)
        if not isinstance(verdict_obj, ReviewVerdict):
            raise ValueError(f"Unexpected review output type: {type(verdict_obj)}")

        if verdict_obj.must_fix and verdict_obj.verdict != "revise":
            verdict_obj.verdict = "revise"

        return ReviewResult(
            status="done",
            model=model,
            family=family_of(model),
            verdict=verdict_obj,
            usage=usage if isinstance(usage, dict) else {},
        )
    except Exception as e:
        logger.warning("Independent review call failed on model %s: %s", model, e)
        return ReviewResult(
            status="unavailable",
            model=model,
            family=family_of(model),
            reason=f"独立审稿调用失败：{e}",
        )

