"""Images and PDFs too large to inline ride the sandbox path, not the model message."""

from __future__ import annotations

import base64

from src.dalton.attachments import NATIVE_IMAGE_MAX_BYTES, NATIVE_PDF_MAX_BYTES, split_oversized
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
