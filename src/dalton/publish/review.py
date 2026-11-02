"""Server-side independent review using a distinct model family (PK.10)."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt

from src.dalton.settings import REPO_ROOT

logger = logging.getLogger(__name__)

REVIEWER_AGENT_PATH = REPO_ROOT / "plugins" / "dalton" / "agents" / "dalton-reviewer.md"

# The reviewer agent prompt numbers its checks 1..9; a reply must cover every
# one or it is not a review of the whole contract (missing items are invalid,
# not silently "pass").
REVIEW_CHECK_NUMBERS: tuple[int, ...] = tuple(range(1, 10))


class ReviewCheckItem(BaseModel):
    """Single check item (1..9) from the independent reviewer.

    ``check`` is a STRICT integer: a stringly-typed ``"1"`` is an invalid
    reply, not something to coerce (prompt review batch 1 round 3 #4)."""

    check: StrictInt = Field(ge=1, le=9)
    result: Literal["pass", "revise"]
    reason: str
    lines: list[int] = Field(default_factory=list)


class ReviewVerdict(BaseModel):
    """Structured output schema for ``dalton-reviewer`` (PK.10)."""

    verdict: Literal["pass", "revise", "block"]
    summary: str
    items: list[ReviewCheckItem] = Field(default_factory=list)
    must_fix: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    # The prompt no longer asks the model to echo the draft hash — the caller
    # injects it into the review input and stamps the result itself. The field
    # stays so a reply that carries one anyway can be checked for consistency
    # instead of being trusted or silently dropped.
    draft_sha256: str | None = None


def review_contract_error(
    verdict: ReviewVerdict, *, draft_sha256: str | None = None
) -> str | None:
    """Contract check on a reviewer reply; ``None`` means the reply is valid.

    Items must cover checks 1..9 (a missing check is an invalid review, not a
    pass), and a ``draft_sha256`` echoed by the model must equal the hash the
    caller injected into the review input.
    """
    covered = {item.check for item in verdict.items}
    missing = [str(n) for n in REVIEW_CHECK_NUMBERS if n not in covered]
    if missing:
        return f"items 未覆盖检查项 {','.join(missing)}（要求逐项覆盖 1–9）"
    if (
        draft_sha256
        and verdict.draft_sha256
        and verdict.draft_sha256.strip().lower() != draft_sha256.strip().lower()
    ):
        return (
            "回填的 draft_sha256 与被审草稿不一致"
            f"（{verdict.draft_sha256[:16]}… ≠ 注入的 {draft_sha256[:16]}…）"
        )
    return None


def reviewer_reply_error(reply: Any) -> str | None:
    """Contract check on any ``dalton-reviewer`` reply — the workflow child,
    the manual sidecar, and the server path all judge the same shape.

    A reply that is not a valid ``ReviewVerdict`` (checks 1..9 covered, usable
    item shapes) is an invalid review, never a pass; the caller records it as
    such so resume never reuses it.
    """
    from pydantic import ValidationError

    try:
        verdict = ReviewVerdict.model_validate(reply)
    except ValidationError as error:
        first = error.errors()[0] if error.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ()))
        return (
            "审稿输出不符合结构"
            f"（{loc or '根字段'}: {first.get('msg', 'invalid')}）——"
            "要求 verdict/summary/items 覆盖 1–9 项/must_fix/nice_to_have"
        )
    return review_contract_error(verdict)


def effective_review_verdict(reply: Any) -> str | None:
    """The reply's verdict after the must_fix rule, or ``None`` when invalid.

    ``pass`` with a non-empty ``must_fix`` reads as ``revise`` — the same rule
    the server applies (``run_review``); ``block`` is never downgraded.
    """
    from pydantic import ValidationError

    try:
        verdict = ReviewVerdict.model_validate(reply)
    except ValidationError:
        return None
    if review_contract_error(verdict) is not None:
        return None
    if verdict.verdict == "pass" and verdict.must_fix:
        return "revise"
    return verdict.verdict


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
    # The sha256 of the exact draft text that was sent for review — computed
    # and stamped by this caller, never taken from the model's reply.
    draft_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.status != "done" or self.verdict is None:
            return {
                "status": self.status,
                "model": self.model,
                "family": self.family,
                "verdict": "unavailable",
                "reason": self.reason or "No cross-family reviewer model available",
            }
        # must_fix forces revise at minimum, but never downgrades a block.
        effective_verdict = (
            self.verdict.verdict
            if self.verdict.verdict == "block"
            else ("revise" if self.verdict.must_fix else self.verdict.verdict)
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
            "draft_sha256": self.draft_sha256,
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


async def pick_reviewer_model(
    author_model: str | None,
    *,
    candidates: list[str] | None = None,
    available_models: set[str] | None = None,
    user_id: str | None = None,
) -> str | None:
    """Pick the first candidate reviewer model whose family differs from ``author_model`` (PK.10 / G3.2).

    Refuses selection (returns ``None``) if ``author_model`` is ``None`` or ``family_of(author_model) == 'unknown'``.
    """
    author_family = family_of(author_model)
    if not author_model or author_family == "unknown":
        return None

    from src.dalton.llm.role_policy import select_independent_reviewer

    chosen = await select_independent_reviewer(
        str(author_model),
        candidates,
        require_tool_calling=False,
        available_models=available_models,
        user_id=user_id,
    )
    if chosen and family_of(chosen) not in ("unknown", author_family):
        return chosen

    if candidates is not None:
        candidate_list = list(candidates)
        if available_models is not None:
            candidate_list = [c for c in candidate_list if c in available_models]
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
        return (
            "你是基金的独立审稿人。只读审稿，输出严格 JSON"
            "（verdict: pass|revise|block, summary, items 覆盖 1–9 项检查, must_fix, nice_to_have）。"
        )
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return raw.strip()


def render_review_input(
    bundle: ReviewBundle, *, max_chars: int = 120_000, draft_sha256: str | None = None
) -> str:
    """Assemble the numbered draft, audit summary, thesis, debate, and management context for review.

    ``draft_sha256`` is the caller-injected identity of the draft under review:
    the reviewer sees it in the header, and the stored review result is stamped
    with the same value — the model is never asked to echo it back.
    """
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

    from src.dalton.publish.audit import format_audit_summary_from_dict

    aud = bundle.audit_summary or {}
    summary_str = format_audit_summary_from_dict(aud) or "共 0 个数字：已核实 0 / 冲突 0 / 未核实 0 / 不需核实 0"

    sections = [
        f"# 待审报告：《{bundle.title}》（类型：{bundle.kind}）",
        "",
        "## 数字核验摘要",
        summary_str,
        "",
        "## 报告正文（带行号）",
        numbered_draft,
    ]
    if draft_sha256:
        # 绑定被审草稿身份：由调用方注入；审稿输出不需要回填它。
        sections.insert(1, f"被审草稿 sha256：{draft_sha256}")
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
    candidates: list[str] | None = None,
    available_models: set[str] | None = None,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> ReviewResult:
    """Run authoritative server-side independent review via ``dalton_complete`` (PK.10 / G3.2)."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    author_family = family_of(author_model)
    if not author_model or author_family == "unknown":
        return ReviewResult(
            status="unavailable",
            reason=(
                f"无法发布：作者模型（{author_model or 'None'}）家族未知（unknown），"
                "无法保证跨家族独立审稿"
            ),
        )

    eff_available = available_models
    if eff_available is None and candidates is None and user_id:
        try:
            from src.dalton.services.provider_status import explain_model_availability

            avail_map = await explain_model_availability(user_id=user_id)
            avail_set = {
                k
                for k, v in avail_map.items()
                if isinstance(v, dict) and v.get("available")
            }
            if avail_set:
                eff_available = avail_set
        except Exception:
            pass

    remaining_candidates: list[str] | None = (
        list(candidates) if candidates is not None else None
    )
    model = await pick_reviewer_model(
        author_model,
        candidates=remaining_candidates,
        available_models=eff_available,
        user_id=user_id,
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
    # The hash is caller-side: computed from the exact draft text being sent,
    # injected into the input, verified against any echoed value on receipt,
    # and stamped onto the stored result.
    draft_sha = hashlib.sha256(bundle.draft_markdown.encode("utf-8")).hexdigest()
    user_prompt = render_review_input(bundle, draft_sha256=draft_sha)

    last_failure: ReviewResult | None = None
    while model and family_of(model) != author_family:
        cand_family = family_of(model)
        if remaining_candidates is None:
            from src.dalton.llm.model_prefs import dalton_model_pref

            resolved_cands = await dalton_model_pref(user_id, "dalton_reviewer_models")
            remaining_candidates = (
                list(resolved_cands) if isinstance(resolved_cands, list) else []
            )
        remaining_candidates = [
            c for c in remaining_candidates if c != model and family_of(c) != cand_family
        ]
        try:
            from src.dalton.llm.complete import dalton_complete

            comp = await dalton_complete(
                None,
                user_id=user_id,
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
                last_failure = ReviewResult(
                    status="unavailable",
                    model=model,
                    family=cand_family,
                    reason=f"独立审稿调用失败：{comp.error or comp.status}",
                )
            else:
                verdict_obj = comp.data
                usage = dict(comp.usage) if isinstance(comp.usage, dict) else {}
                usage["replayed"] = bool(comp.replayed)
                usage["cached"] = bool(comp.cached)
                usage["cash_usd"] = float(comp.cash_usd)
                usage["notional_usd"] = float(comp.notional_usd)
                usage["invocation_id"] = comp.invocation_id

                if isinstance(verdict_obj, dict):
                    verdict_obj = ReviewVerdict.model_validate(verdict_obj)
                if not isinstance(verdict_obj, ReviewVerdict):
                    raise ValueError(f"Unexpected review output type: {type(verdict_obj)}")

                contract_err = review_contract_error(
                    verdict_obj, draft_sha256=draft_sha
                )
                if contract_err:
                    raise ValueError(f"审稿结果无效：{contract_err}")

                # must_fix forces revise at minimum, but never downgrades a block.
                if verdict_obj.must_fix and verdict_obj.verdict == "pass":
                    verdict_obj.verdict = "revise"

                return ReviewResult(
                    status="done",
                    model=model,
                    family=cand_family,
                    verdict=verdict_obj,
                    usage=usage,
                    draft_sha256=draft_sha,
                )
        except Exception as e:
            logger.warning("Independent review call failed on model %s: %s", model, e)
            last_failure = ReviewResult(
                status="unavailable",
                model=model,
                family=cand_family,
                reason=f"独立审稿调用失败：{e}",
            )

        model = await pick_reviewer_model(
            author_model,
            candidates=remaining_candidates,
            available_models=eff_available,
            user_id=user_id,
        )

    assert last_failure is not None
    return last_failure

