"""Images and PDFs too large to inline ride the sandbox path, not the model message."""

from __future__ import annotations

import base64
from unittest.mock import create_autospec

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.dalton.attachments import (
    NATIVE_IMAGE_MAX_BYTES,
    NATIVE_MESSAGE_MAX_BYTES,
    NATIVE_PDF_MAX_BYTES,
    NATIVE_REQUEST_MAX_BYTES,
    DaltonAttachmentBudgetMiddleware,
    split_oversized,
    trim_request_inline_attachments,
)
from src.dalton.middleware.attachments import reading_path_text
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


# Payload byte counts are picked divisible by 3, so the middleware's
# len*3//4 decoded-size estimate over the base64 is exact and budget math in
# the tests below is byte-precise.
_MIB = 1024 * 1024
_FOUR_MIB = 4 * _MIB - 1
_NINE_MIB = 9 * _MIB


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


def _read_image_turn(name: str, size: int) -> ToolMessage:
    """A Read result carrying an inline image, as MultimodalMiddleware builds it."""
    b64 = base64.b64encode(b"\0" * size).decode()
    return ToolMessage(
        content=[
            {"type": "text", "text": "[Viewing image]"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
        tool_call_id=f"call_{name}",
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
    m1 = _pdf_turn("t1.pdf", _NINE_MIB, path="work/uploads/t1.pdf")
    m2 = _pdf_turn("t2.pdf", _NINE_MIB, path=None)
    m3 = _pdf_turn("t3.pdf", _NINE_MIB, path="work/uploads/t3.pdf")
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

    # Earlier turns' 9 MiB PDFs are replaced by one-line text pointers: with a
    # path the size-aware reading path, without one an honest "not found".
    assert sent[2].content[1] == {
        "type": "text",
        "text": "[附件 t2.pdf 较早上传，已从本次请求中省略，未找到它在工作区的保存位置；如需再看，请让用户重新上传]",
    }
    assert sent[0].content[1] == {
        "type": "text",
        "text": (
            "[附件 t1.pdf 较早上传，已保存在 work/uploads/t1.pdf；"
            "Read 打不开超过 5 MB 的 PDF，用 Python（pypdf / pdfplumber）提取文字，"
            "或拆成小于 5 MB 的文件再读]"
        ),
    }

    # Checkpoint messages are completely unchanged.
    assert m1.content == orig_m1_blocks
    assert m2.content == orig_m2_blocks
    assert m3.content == orig_m3_blocks
    assert m1.content[1]["type"] == "file"
    assert m2.content[1]["type"] == "file"


def test_read_images_on_tool_messages_share_the_budget_and_keep_tool_pairing():
    # A 9 MiB PDF upload plus four 4 MiB Read images is 25 MiB of inline
    # payload; the 12 MiB budget keeps the newest 12 MiB (three images) and
    # replaces the earlier ones. Tool results used to bypass the count
    # entirely.
    pdf_turn = _pdf_turn("t1.pdf", _NINE_MIB, path="work/uploads/t1.pdf")
    reads = [_read_image_turn(f"img{i}.png", _FOUR_MIB) for i in range(4)]
    messages = [
        pdf_turn,
        AIMessage(content="reading"),
        *reads,
        HumanMessage(content="summarize what you saw"),
    ]

    sent = trim_request_inline_attachments(messages)

    # The newest three 4 MiB images stay inline.
    for kept_idx in (3, 4, 5):
        assert sent[kept_idx].content[1]["type"] == "image_url"

    # The fourth (older) image: the ToolMessage survives with its tool_call_id
    # and its label text, only the attachment block is swapped for a pointer.
    # A Read image records no name or save path, so the pointer says so instead
    # of guessing one.
    evicted = sent[2]
    assert isinstance(evicted, ToolMessage)
    assert evicted.tool_call_id == "call_img0.png"
    assert evicted.content[0] == {"type": "text", "text": "[Viewing image]"}
    assert evicted.content[1]["type"] == "text"
    assert "未找到它在工作区的保存位置" in evicted.content[1]["text"]

    # The oldest 9 MiB PDF upload is replaced too.
    assert sent[0].content[1]["type"] == "text"
    assert "work/uploads/t1.pdf" in sent[0].content[1]["text"]

    # The original checkpoint messages are untouched.
    assert reads[0].content[1]["type"] == "image_url"
    assert pdf_turn.content[1]["type"] == "file"


def test_an_older_small_attachment_stays_when_only_a_bigger_one_overflows():
    # The newest turn spent 8 MiB; the 9 MiB PDF behind it does not fit and is
    # dropped, but a still-older 2 MiB image fits in what is left (8 + 2 ≤ 12)
    # and stays inline — eviction is per block, not everything older.
    two_mib = 2 * _MIB - 2  # divisible by 3, exact estimate
    eight_mib = 8 * _MIB - 4  # divisible by 3, exact estimate
    old_small = HumanMessage(
        content=[
            {"type": "text", "text": "[Attached image: old.png (saved to work/uploads/old.png)]"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"\0" * two_mib).decode()},
            },
        ]
    )
    big_pdf = _pdf_turn("big.pdf", _NINE_MIB, path="work/uploads/big.pdf")
    newest = HumanMessage(
        content=[
            {"type": "text", "text": "[Attached image: new.png (saved to work/uploads/new.png)]"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"\0" * eight_mib).decode()},
            },
        ]
    )
    sent = trim_request_inline_attachments([old_small, big_pdf, newest])

    assert sent[2].content[1]["type"] == "image_url"  # newest 8 MiB kept
    assert sent[1].content[1]["type"] == "text"  # 9 MiB PDF replaced
    assert "已保存在 work/uploads/big.pdf" in sent[1].content[1]["text"]
    assert sent[0].content[1]["type"] == "image_url"  # older 2 MiB still fits


def test_a_save_note_for_another_attachment_is_never_borrowed_as_a_path():
    # One message, two PDFs, one upload note — and the note is for the OTHER
    # file. The unmatched attachment must say it has no known save location,
    # not point at a path saved for its sibling.
    six_mib = 6 * _MIB
    b64_a = base64.b64encode(b"\0" * six_mib).decode()
    b64_b = base64.b64encode(b"\0" * six_mib).decode()
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "[Attached PDF: a.pdf]"},
            {"type": "file", "base64": b64_a, "mime_type": "application/pdf", "filename": "a.pdf"},
            {"type": "text", "text": "[Attached PDF: b.pdf]"},
            {"type": "file", "base64": b64_b, "mime_type": "application/pdf", "filename": "b.pdf"},
            {
                "type": "text",
                "text": (
                    "The user attached a file (a.pdf, application/pdf). "
                    "It has been saved to work/uploads/a.pdf_x1.pdf."
                ),
            },
        ]
    )
    sent = trim_request_inline_attachments([msg], budget_bytes=1024)

    assert "work/uploads/a.pdf_x1.pdf" in sent[0].content[1]["text"]
    assert "a.pdf_x1.pdf" not in sent[0].content[3]["text"]
    assert "未找到它在工作区的保存位置" in sent[0].content[3]["text"]


def test_the_pointer_names_the_reading_path_that_fits_the_size():
    # Over Read's 5 MB limit the pointer routes around it; under, Read serves
    # the file directly. Same wording source as the upload note.
    big = 6 * _MIB
    small = _MIB

    def _img_turn(name: str, size: int) -> HumanMessage:
        return HumanMessage(
            content=[
                {"type": "text", "text": f"[Attached image: {name} (saved to work/uploads/{name})]"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"\0" * size).decode()},
                },
            ]
        )

    sent = trim_request_inline_attachments(
        [_img_turn("big.png", big), _img_turn("small.png", small)],
        budget_bytes=1024,
    )
    assert reading_path_text("image", big) in sent[0].content[1]["text"]
    assert "PIL" in sent[0].content[1]["text"]
    assert "用 Read 查看" in sent[1].content[1]["text"]
    assert "PIL" not in sent[1].content[1]["text"]


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
