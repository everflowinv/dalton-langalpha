"""Images and PDFs too large to inline ride the sandbox path, not the model message."""

from __future__ import annotations

import base64
from unittest.mock import create_autospec

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from src.dalton.attachments import (
    NATIVE_IMAGE_MAX_BYTES,
    NATIVE_MESSAGE_MAX_BYTES,
    NATIVE_PDF_MAX_BYTES,
    NATIVE_REQUEST_MAX_BYTES,
    DaltonAttachmentBudgetMiddleware,
    split_oversized,
    trim_request_inline_attachments,
)
from src.server.models.additional_context import MultimodalContext


def _ctx(mime: str, size: int) -> MultimodalContext:
    data = base64.b64encode(b"\0" * size).decode()
    return MultimodalContext(type="image", data=f"data:{mime};base64,{data}", description="f")


def test_small_attachments_stay_inline_and_large_ones_move_to_the_sandbox():
    small_img = _ctx("image/png", 1024)
    big_img = _ctx("image/png", NATIVE_IMAGE_MAX_BYTES + 1024)
    small_pdf = _ctx("application/pdf", NATIVE_IMAGE_MAX_BYTES + 1024)  # PDFs get their own, larger cap
    big_pdf = _ctx("application/pdf", NATIVE_PDF_MAX_BYTES + 1024)
    inline, oversized = split_oversized([small_img, big_img, small_pdf, big_pdf])
    assert inline == [small_img, small_pdf]
    assert oversized == [big_img, big_pdf]


def test_the_pdf_cap_is_ten_mb():
    # Two 13 MB PDFs used to inline together and blow the 32 MB request cap
    # every later turn; 10 MB keeps a single large PDF under every provider.
    assert NATIVE_PDF_MAX_BYTES == 10 * 1024 * 1024


def test_inline_attachments_share_one_per_message_budget():
    # Each 6 MB PDF passes its own cap; together they would exceed the 10 MB
    # message budget, so the second rides the sandbox only.
    pdf_a = _ctx("application/pdf", 6 * 1024 * 1024)
    pdf_b = _ctx("application/pdf", 6 * 1024 * 1024)
    inline, oversized = split_oversized([pdf_a, pdf_b])
    assert inline == [pdf_a]
    assert oversized == [pdf_b]
    assert NATIVE_MESSAGE_MAX_BYTES == 10 * 1024 * 1024


def test_the_budget_is_applied_in_attachment_order():
    # An image small enough alone still moves when earlier attachments spent
    # the budget — the checkpoint must not grow past the cap either way.
    first = _ctx("application/pdf", 9 * 1024 * 1024)
    second = _ctx("image/png", 2 * 1024 * 1024)
    inline, oversized = split_oversized([first, second])
    assert inline == [first]
    assert oversized == [second]


def _pdf_turn(name: str, size: int, path: str | None = None) -> HumanMessage:
    b64 = base64.b64encode(b"\0" * size).decode()
    path_note = f" (saved to {path})" if path else ""
    return HumanMessage(
        content=[
            {"type": "text", "text": f"[Attached PDF: {name}{path_note}]"},
            {
                "type": "file",
                "base64": b64,
                "mime_type": "application/pdf",
                "filename": name,
            },
            {"type": "text", "text": f"read {name}"},
        ]
    )


def _model_request(messages: list) -> ModelRequest:
    req = create_autospec(ModelRequest, instance=True)
    req.messages = messages

    def _override(**kwargs):
        new_req = create_autospec(ModelRequest, instance=True)
        new_req.messages = kwargs.get("messages", messages)
        return new_req

    req.override.side_effect = _override
    return req


@pytest.mark.asyncio
async def test_three_nine_mib_turns_keep_newest_and_replace_earlier_without_mutating_checkpoint():
    assert NATIVE_REQUEST_MAX_BYTES == 12 * 1024 * 1024
    nine_mib = 9 * 1024 * 1024
    m1 = _pdf_turn("t1.pdf", nine_mib, path="work/uploads/t1.pdf")
    m2 = _pdf_turn("t2.pdf", nine_mib, path=None)
    m3 = _pdf_turn("t3.pdf", nine_mib, path="work/uploads/t3.pdf")
    checkpoint_messages = [m1, AIMessage(content="ok 1"), m2, AIMessage(content="ok 2"), m3]
    orig_m1_blocks = list(m1.content)
    orig_m2_blocks = list(m2.content)
    orig_m3_blocks = list(m3.content)

    mw = DaltonAttachmentBudgetMiddleware()
    captured: list = []

    async def _handler(req: ModelRequest):
        captured.append(req.messages)
        return "done"

    await mw.awrap_model_call(_model_request(checkpoint_messages), _handler)
    sent = captured[0]

    # Latest turn's 9 MiB PDF stays inline.
    assert sent[4].content[1]["type"] == "file"
    assert sent[4].content[1]["filename"] == "t3.pdf"

    # Earlier turns' 9 MiB PDFs are replaced by 1-line text pointers (with and without explicit path).
    assert sent[2].content[1] == {
        "type": "text",
        "text": "[附件 t2.pdf 较早上传，已保存在工作区 work/uploads/ 下；需要时用工具读取]",
    }
    assert sent[0].content[1] == {
        "type": "text",
        "text": "[附件 t1.pdf 较早上传，已保存在 work/uploads/t1.pdf；需要时用工具读取]",
    }

    # Checkpoint messages are completely unchanged.
    assert m1.content == orig_m1_blocks
    assert m2.content == orig_m2_blocks
    assert m3.content == orig_m3_blocks
    assert m1.content[1]["type"] == "file"
    assert m2.content[1]["type"] == "file"


def test_under_request_budget_keeps_all_turns_untouched():
    five_mib = 5 * 1024 * 1024
    m1 = _pdf_turn("a.pdf", five_mib, path="work/uploads/a.pdf")
    m2 = _pdf_turn("b.pdf", five_mib, path="work/uploads/b.pdf")
    msgs = [m1, AIMessage(content="ack"), m2]

    mw = DaltonAttachmentBudgetMiddleware()
    captured: list = []
    mw.wrap_model_call(_model_request(msgs), lambda req: captured.append(req.messages))

    assert captured[0] is msgs
    assert trim_request_inline_attachments(msgs) is msgs
