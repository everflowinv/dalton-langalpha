"""Contract tests for the unified reviewer output (prompt review batch 1 #2).

`dalton-reviewer` 的提示词、服务端 `publish/review.py`、`publish/service.py`
与各 workflow.js 使用同一份契约：9 项检查逐项覆盖、verdict 三值
（pass|revise|block）、草稿哈希由调用方注入并验证，不让模型回填。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest
from pydantic import ValidationError

from src.dalton.llm import complete as complete_mod
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
from src.dalton.publish.review import (
    REVIEW_CHECK_NUMBERS,
    ReviewBundle,
    ReviewCheckItem,
    ReviewResult,
    ReviewVerdict,
    render_review_input,
    review_contract_error,
    run_review,
)


def _nine_items(result: str = "pass") -> list[ReviewCheckItem]:
    return [
        ReviewCheckItem(check=n, result=result, reason="ok") for n in range(1, 10)
    ]


def _bundle(draft: str = "# Draft\nline 2") -> ReviewBundle:
    return ReviewBundle(
        title="META 3Q26",
        kind="earnings",
        draft_markdown=draft,
        audit_summary={},
    )


class TestNineCheckContract:
    def test_check_ids_accept_1_through_9_and_reject_10(self) -> None:
        assert REVIEW_CHECK_NUMBERS == tuple(range(1, 10))
        assert ReviewCheckItem(check=9, result="pass", reason="ok")
        with pytest.raises(ValidationError):
            ReviewCheckItem(check=10, result="pass", reason="ok")

    def test_block_verdict_parses_and_survives_must_fix(self) -> None:
        verdict = ReviewVerdict(
            verdict="block",
            summary="事实错误",
            items=_nine_items("revise"),
            must_fix=["第 5 行广告收入与 10-Q 不符"],
        )
        res = ReviewResult(
            status="done", model="gpt-6.1-sol-oauth", family="openai-gpt", verdict=verdict
        )
        d = res.to_dict()
        # must_fix forces revise at minimum, but never downgrades a block.
        assert d["verdict"] == "block"

    def test_pass_with_must_fix_is_reported_as_revise(self) -> None:
        verdict = ReviewVerdict(
            verdict="pass",
            summary="总体通过",
            items=_nine_items("pass"),
            must_fix=["补一句风险"],
        )
        res = ReviewResult(status="done", verdict=verdict)
        assert res.to_dict()["verdict"] == "revise"

    def test_missing_check_items_make_the_review_invalid(self) -> None:
        verdict = ReviewVerdict(
            verdict="revise", summary="x", items=_nine_items()[:8], must_fix=[]
        )
        err = review_contract_error(verdict)
        assert err is not None and "9" in err

    def test_complete_nine_items_pass_the_contract(self) -> None:
        verdict = ReviewVerdict(
            verdict="pass", summary="ok", items=_nine_items(), must_fix=[]
        )
        assert review_contract_error(verdict) is None

    def test_echoed_hash_mismatch_is_rejected_matching_echo_is_accepted(self) -> None:
        injected = "a" * 64
        good = ReviewVerdict(
            verdict="pass",
            summary="ok",
            items=_nine_items(),
            draft_sha256=injected.upper(),
        )
        assert review_contract_error(good, draft_sha256=injected) is None

        bad = ReviewVerdict(
            verdict="pass",
            summary="ok",
            items=_nine_items(),
            draft_sha256="b" * 64,
        )
        err = review_contract_error(bad, draft_sha256=injected)
        assert err is not None and "draft_sha256" in err

    def test_no_echoed_hash_is_accepted_the_prompt_does_not_ask_for_it(self) -> None:
        verdict = ReviewVerdict(verdict="pass", summary="ok", items=_nine_items())
        assert review_contract_error(verdict, draft_sha256="a" * 64) is None


class TestRunReviewInjectsAndVerifiesHash:
    @pytest.mark.asyncio
    async def test_valid_block_reply_is_bound_to_caller_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        draft = "# Draft\n收入 594 亿美元"
        captured: dict[str, Any] = {}

        async def _fake_complete(
            llm_service: Any = None,
            *,
            user_id: str | None = None,
            system_prompt: str | None = None,
            user_prompt: str = "",
            request_model: str | None = None,
            response_schema: Any = None,
            raise_on_error: bool = False,
            **_kwargs: Any,
        ) -> DaltonCompleteResult:
            captured["user_prompt"] = user_prompt
            return DaltonCompleteResult(
                ok=True,
                status="done",
                model=str(request_model),
                data=ReviewVerdict(
                    verdict="block",
                    summary="结论无证据支撑",
                    items=_nine_items("revise"),
                    must_fix=["§3 结论与证据脱节"],
                ),
                usage={},
                cash_usd=0.0,
                notional_usd=0.01,
                invocation_id="lj1:rev-block",
            )

        monkeypatch.setattr(
            complete_mod,
            "dalton_complete",
            create_autospec(dalton_complete, side_effect=_fake_complete),
        )
        res = await run_review(
            user_id="u1",
            author_model="claude-opus-5-5-oauth",
            bundle=_bundle(draft),
            candidates=["gpt-6.1-sol-oauth"],
        )
        assert res.status == "done"
        assert res.verdict is not None and res.verdict.verdict == "block"
        import hashlib

        expected = hashlib.sha256(draft.encode("utf-8")).hexdigest()
        assert res.draft_sha256 == expected
        d = res.to_dict()
        assert d["verdict"] == "block"
        assert d["draft_sha256"] == expected
        # The caller injected the hash into the review input.
        assert f"被审草稿 sha256：{expected}" in captured["user_prompt"]

    @pytest.mark.asyncio
    async def test_missing_items_reply_is_rejected_not_stored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_complete(
            llm_service: Any = None,
            *,
            request_model: str | None = None,
            raise_on_error: bool = False,
            **_kwargs: Any,
        ) -> DaltonCompleteResult:
            return DaltonCompleteResult(
                ok=True,
                status="done",
                model=str(request_model),
                data=ReviewVerdict(
                    verdict="pass",
                    summary="只看了前 7 项",
                    items=_nine_items()[:7],
                ),
                usage={},
                cash_usd=0.0,
                notional_usd=0.01,
                invocation_id="lj1:rev-incomplete",
            )

        monkeypatch.setattr(
            complete_mod,
            "dalton_complete",
            create_autospec(dalton_complete, side_effect=_fake_complete),
        )
        res = await run_review(
            user_id="u1",
            author_model="claude-opus-5-5-oauth",
            bundle=_bundle(),
            candidates=["gpt-6.1-sol-oauth"],
        )
        # Missing items are an invalid review, not a pass: every candidate is
        # tried and the run ends unavailable with the contract reason.
        assert res.status == "unavailable"
        assert res.reason is not None and "审稿结果无效" in res.reason
        assert "8,9" in res.reason

    @pytest.mark.asyncio
    async def test_mismatched_echoed_hash_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_complete(
            llm_service: Any = None,
            *,
            request_model: str | None = None,
            raise_on_error: bool = False,
            **_kwargs: Any,
        ) -> DaltonCompleteResult:
            return DaltonCompleteResult(
                ok=True,
                status="done",
                model=str(request_model),
                data=ReviewVerdict(
                    verdict="pass",
                    summary="ok",
                    items=_nine_items(),
                    draft_sha256="f" * 64,
                ),
                usage={},
                cash_usd=0.0,
                notional_usd=0.01,
                invocation_id="lj1:rev-hash",
            )

        monkeypatch.setattr(
            complete_mod,
            "dalton_complete",
            create_autospec(dalton_complete, side_effect=_fake_complete),
        )
        res = await run_review(
            user_id="u1",
            author_model="claude-opus-5-5-oauth",
            bundle=_bundle("# real draft"),
            candidates=["gpt-6.1-sol-oauth"],
        )
        assert res.status == "unavailable"
        assert res.reason is not None and "draft_sha256" in res.reason

    def test_render_review_input_without_hash_omits_the_line(self) -> None:
        text = render_review_input(_bundle(), max_chars=10_000)
        assert "被审草稿 sha256" not in text
        text2 = render_review_input(
            _bundle(), max_chars=10_000, draft_sha256="c" * 64
        )
        assert f"被审草稿 sha256：{'c' * 64}" in text2


class TestReviewerPromptContract:
    def test_agent_prompt_drops_the_hash_echo_requirement_and_lists_nine_items(
        self,
    ) -> None:
        from src.dalton.publish.review import load_agent_body

        body = load_agent_body("dalton-reviewer")
        assert "draft_sha256" not in body
        # 旧契约要求"原样回填提示词中给定的哈希"；新契约明确禁止模型回填。
        assert "原样回填" not in body
        assert "不要在输出里回填任何哈希" in body
        # 9 项检查在提示词里编号 1–9，items 覆盖要求与 schema 一致。
        assert "逐项覆盖第 1–9 项检查" in body
        assert '"verdict":"pass"|"revise"|"block"' in body
