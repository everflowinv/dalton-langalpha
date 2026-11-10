"""Regression: manual-parse fallback must accept content_blocks list, and a call
only ever switches tracing off."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langsmith.run_helpers import get_tracing_context, tracing_context
from pydantic import BaseModel

from src.llms.api_call import make_api_call


class _Schema(BaseModel):
    description: str
    summary: str


class _ListContentMessage:
    def __init__(self, blocks: list[dict[str, Any]]) -> None:
        self.content_blocks = blocks
        self.content = blocks
        self.usage_metadata = None
        self.response_metadata: dict[str, Any] = {}
        self.additional_kwargs: dict[str, Any] = {}


class _StubLLM:
    def __init__(self, message: _ListContentMessage) -> None:
        self._message = message

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> "_StubLLM":
        return self

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        self._calls = getattr(self, "_calls", 0) + 1
        if self._calls == 1:
            return {"parsed": None, "raw": self._message}
        return self._message


@pytest.mark.asyncio
async def test_fallback_handles_content_blocks_list() -> None:
    blocks = [
        {"type": "thinking", "thinking": "let me think about this"},
        {
            "type": "text",
            "text": (
                'Here is the JSON:\n\n'
                '{"description": "A test memo.", "summary": "Two short paragraphs."}'
            ),
        },
    ]
    llm = _StubLLM(_ListContentMessage(blocks))

    # ``tracing_context`` requires LangSmith plumbing we don't need in a
    # unit test — patch it out.
    with patch("langsmith.tracing_context") as ctx:
        ctx.return_value.__enter__ = lambda self: None
        ctx.return_value.__exit__ = lambda self, *a: None
        result = await make_api_call(
            llm,
            system_prompt="sys",
            user_prompt="user",
            response_schema=_Schema,
            max_parsing_retries=2,
        )

    assert isinstance(result, _Schema)
    assert result.description == "A test memo."
    assert result.summary == "Two short paragraphs."


@pytest.mark.asyncio
async def test_fallback_still_rejects_truly_empty_list() -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": "no answer"},
    ]
    llm = _StubLLM(_ListContentMessage(blocks))

    with patch("langsmith.tracing_context") as ctx:
        ctx.return_value.__enter__ = lambda self: None
        ctx.return_value.__exit__ = lambda self, *a: None
        with pytest.raises(ValueError) as exc_info:
            await make_api_call(
                llm,
                system_prompt="sys",
                user_prompt="user",
                response_schema=_Schema,
                max_parsing_retries=2,
            )

    # The error should be about JSON extraction, not type rejection.
    assert "Response is not a string" not in str(exc_info.value)


class _TracingProbe:
    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> AIMessage:
        self.enabled = get_tracing_context()["enabled"]
        return AIMessage(content="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ambient", "disable_tracing", "seen"),
    [
        pytest.param(None, False, None, id="unconfigured-stays-unconfigured"),
        pytest.param(True, False, True, id="traced-caller-stays-traced"),
        pytest.param(True, True, False, id="opt-out"),
    ],
)
async def test_a_call_never_switches_tracing_on(ambient, disable_tracing, seen) -> None:
    llm = _TracingProbe()

    with tracing_context(enabled=ambient):
        await make_api_call(llm, "sys", "user", disable_tracing=disable_tracing)

    assert llm.enabled is seen


# DALTON-HOOK(notional-pricing): verify per-attempt usage tracking (`per_call_usages`)
# across structured-output retries and native raw parse fallbacks for threshold pricing.
class _SequencedRetryLLM:
    """Stub LLM that yields a sequence of responses or exceptions."""

    def __init__(
        self,
        responses: list[Any],
        *,
        structured_raw_response: Any = None,
        structured_raises: Exception | None = NotImplementedError("no structured output"),
    ) -> None:
        self._responses = list(responses)
        self._structured_raw_response = structured_raw_response
        self._structured_raises = structured_raises
        self.ainvoke_calls = 0

    def with_structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._structured_raises is not None:
            raise self._structured_raises
        parent = self

        class _StructuredWrapper:
            async def ainvoke(self, *_a: Any, **_kw: Any) -> Any:
                if isinstance(parent._structured_raw_response, Exception):
                    raise parent._structured_raw_response
                return parent._structured_raw_response

        return _StructuredWrapper()

    async def ainvoke(self, *_args: Any, **_kwargs: Any) -> Any:
        idx = self.ainvoke_calls
        self.ainvoke_calls += 1
        item = self._responses[idx]
        if isinstance(item, Exception):
            raise item
        return item


def _ai_msg(
    text: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> AIMessage:
    usage_meta: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_tokens:
        usage_meta["input_token_details"] = {"cache_read": cached_tokens}
    return AIMessage(content=text, usage_metadata=usage_meta)


@pytest.mark.asyncio
async def test_structured_retry_per_call_usages_two_150k_vs_single_300k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two 150K/1K retry attempts in make_api_call must price at $0.031000 nominal
    ($0.0155 each in <=200K tier), never $0.093000 (single 300K/2K >200K tier),
    with cash_usd == 0."""
    import asyncio
    from decimal import Decimal
    from types import SimpleNamespace
    from unittest.mock import create_autospec
    from ptc_agent.config.agent import CredentialSource
    from src.dalton.llm.complete import compute_call_costs
    import src.server.services.llm.service as llm_service_mod

    monkeypatch.setattr(asyncio, "sleep", create_autospec(asyncio.sleep, return_value=None))

    valid_json = '{"description": "valid desc", "summary": "valid summary"}'
    retry_llm = _SequencedRetryLLM(
        [
            _ai_msg("not valid json at all", input_tokens=150_000, output_tokens=1_000),
            _ai_msg(valid_json, input_tokens=150_000, output_tokens=1_000),
        ]
    )

    fake_resolved = SimpleNamespace(
        llm_client=retry_llm,
        llm=SimpleNamespace(background="muse-ember-1.0-oauth", name="muse-ember-1.0-oauth"),
        credential_source=CredentialSource.OAUTH,
    )
    monkeypatch.setattr(
        llm_service_mod,
        "resolve_llm_config",
        create_autospec(llm_service_mod.resolve_llm_config, return_value=fake_resolved),
    )

    svc = llm_service_mod.LLMService(agent_config=SimpleNamespace(llm=fake_resolved.llm))
    parsed, usage = await svc.complete(
        user_id="user-ember-test",
        system_prompt="sys",
        user_prompt="user",
        response_schema=_Schema,
        slot="primary",
        request_model="muse-ember-1.0-oauth",
        is_byok=True,
        disable_tracing=True,
        return_token_usage=True,
    )
    assert isinstance(parsed, _Schema)
    assert parsed.description == "valid desc"
    assert usage["input_tokens"] == 300_000
    assert usage["output_tokens"] == 2_000
    assert usage["retry_attempts"] == 2
    assert len(usage["per_call_usages"]) == 2
    assert usage["per_call_usages"][0]["input_tokens"] == 150_000
    assert usage["per_call_usages"][1]["input_tokens"] == 150_000

    cash_two, notional_two = compute_call_costs("muse-ember-1.0-oauth", "oauth", usage)
    assert cash_two == Decimal("0")
    assert notional_two == Decimal("0.031000")

    # Contrast: a single 300K input / 2K output call is in the >200K tier ($0.093000)
    single_llm = _SequencedRetryLLM(
        [_ai_msg(valid_json, input_tokens=300_000, output_tokens=2_000)]
    )
    parsed_single, usage_single = await make_api_call(
        single_llm,
        system_prompt="sys",
        user_prompt="user",
        response_schema=_Schema,
        return_token_usage=True,
        disable_tracing=True,
    )
    assert isinstance(parsed_single, _Schema)
    assert usage_single["input_tokens"] == 300_000
    assert usage_single["output_tokens"] == 2_000
    assert len(usage_single["per_call_usages"]) == 1

    cash_single, notional_single = compute_call_costs(
        "muse-ember-1.0-oauth", "oauth", usage_single
    )
    assert cash_single == Decimal("0")
    assert notional_single == Decimal("0.093000")


@pytest.mark.asyncio
async def test_structured_retry_mixed_input_lengths_and_cache_select_tier_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each retry attempt selects its own tier using that attempt's total input (including cache)."""
    import asyncio
    from decimal import Decimal
    from unittest.mock import create_autospec
    from src.dalton.llm.complete import compute_call_costs

    monkeypatch.setattr(asyncio, "sleep", create_autospec(asyncio.sleep, return_value=None))

    valid_json = '{"description": "d", "summary": "s"}'
    # Attempt 1: 180K total (100K cached, 80K uncached), 1K output -> <=200K tier:
    #   80K * 0.10/1M + 100K * 0.004/1M + 1K * 0.50/1M = 0.008 + 0.0004 + 0.0005 = 0.0089
    # Attempt 2: 220K total (100K cached, 120K uncached), 1K output -> >200K tier:
    #   120K * 0.30/1M + 100K * 0.012/1M + 1K * 1.50/1M = 0.036 + 0.0012 + 0.0015 = 0.0387
    # Total nominal = 0.0089 + 0.0387 = 0.047600
    llm = _SequencedRetryLLM(
        [
            _ai_msg(
                "broken json",
                input_tokens=180_000,
                output_tokens=1_000,
                cached_tokens=100_000,
            ),
            _ai_msg(
                valid_json,
                input_tokens=220_000,
                output_tokens=1_000,
                cached_tokens=100_000,
            ),
        ]
    )
    _, usage = await make_api_call(
        llm,
        system_prompt="sys",
        user_prompt="user",
        response_schema=_Schema,
        return_token_usage=True,
        disable_tracing=True,
    )
    assert usage["input_tokens"] == 400_000
    assert usage["cached_tokens"] == 200_000
    assert usage["output_tokens"] == 2_000
    assert len(usage["per_call_usages"]) == 2

    cash_usd, notional_usd = compute_call_costs("muse-ember-1.0-oauth", "oauth", usage)
    assert cash_usd == Decimal("0")
    assert notional_usd == Decimal("0.047600")


@pytest.mark.asyncio
async def test_native_structured_raw_parse_failure_retains_usage_and_no_response_exception_adds_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When with_structured_output(include_raw=True) returns a raw response with parsed=None,
    its real token usage is preserved into fallback total_token_info and per_call_usages;
    when with_structured_output raises before any response, no usage is fabricated."""
    import asyncio
    from decimal import Decimal
    from unittest.mock import create_autospec
    from src.dalton.llm.complete import compute_call_costs

    monkeypatch.setattr(asyncio, "sleep", create_autospec(asyncio.sleep, return_value=None))

    valid_json = '{"description": "from fallback", "summary": "ok"}'
    raw_failed_msg = _ai_msg("invalid structured output", input_tokens=150_000, output_tokens=1_000)
    fallback_ok_msg = _ai_msg(valid_json, input_tokens=150_000, output_tokens=1_000)

    llm_with_raw = _SequencedRetryLLM(
        [fallback_ok_msg],
        structured_raw_response={"parsed": None, "raw": raw_failed_msg},
        structured_raises=None,
    )
    parsed, usage = await make_api_call(
        llm_with_raw,
        system_prompt="sys",
        user_prompt="user",
        response_schema=_Schema,
        return_token_usage=True,
        disable_tracing=True,
    )
    assert isinstance(parsed, _Schema)
    assert usage["input_tokens"] == 300_000
    assert usage["output_tokens"] == 2_000
    assert usage["retry_attempts"] == 1
    assert len(usage["per_call_usages"]) == 2
    cash_usd, notional_usd = compute_call_costs("muse-ember-1.0-oauth", "oauth", usage)
    assert cash_usd == Decimal("0")
    assert notional_usd == Decimal("0.031000")

    # When structured call raises an exception without returning a response, no tokens are fabricated
    llm_structured_err = _SequencedRetryLLM(
        [fallback_ok_msg],
        structured_raw_response=RuntimeError("transport error before response"),
        structured_raises=None,
    )
    _, usage_err = await make_api_call(
        llm_structured_err,
        system_prompt="sys",
        user_prompt="user",
        response_schema=_Schema,
        return_token_usage=True,
        disable_tracing=True,
    )
    assert usage_err["input_tokens"] == 150_000
    assert usage_err["output_tokens"] == 1_000
    assert len(usage_err["per_call_usages"]) == 1
    cash_e, notional_e = compute_call_costs("muse-ember-1.0-oauth", "oauth", usage_err)
    assert cash_e == Decimal("0")
    assert notional_e == Decimal("0.015500")
