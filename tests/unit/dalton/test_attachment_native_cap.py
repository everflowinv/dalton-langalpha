"""Images and PDFs too large to inline ride the sandbox path, not the model message."""

from __future__ import annotations

import base64

from src.dalton.attachments import (
    NATIVE_IMAGE_MAX_BYTES,
    NATIVE_MESSAGE_MAX_BYTES,
    NATIVE_PDF_MAX_BYTES,
    split_oversized,
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
