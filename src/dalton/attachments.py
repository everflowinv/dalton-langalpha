"""Which chat attachments go to the model inline and which only to the sandbox.

Providers refuse an inline image above about 5 MB and a request above about
32 MB (Anthropic); Gemini takes about 20 MB inline. An attachment inlined into
the user message also stays in the checkpoint and is resent every turn, so one
that is too large breaks the thread rather than one call. Across turns,
``DaltonAttachmentBudgetMiddleware`` (`src/dalton/middleware/attachments.py`)
caps the cumulative inline attachment bytes sent on each model request to 12 MB
by replacing older turns' attachment blocks with text pointers on the model-call
copy while leaving the checkpoint messages intact.

DALTON-HOOK(attachment-native-cap): these caps are a deliberately conservative
policy — two inline PDFs large enough individually must not together blow the
request limit, and the checkpoint must not grow by their bytes every turn.
They are expected to be replaced once the upstream attachment adaptation (B5)
lands and sizes to the actual per-provider request budget.
"""

from __future__ import annotations

from typing import Any

from src.dalton.middleware.attachments import (
    NATIVE_REQUEST_MAX_BYTES,
    READ_INLINE_MAX_BYTES,
    DaltonAttachmentBudgetMiddleware,
    reading_path_text,
    trim_request_inline_attachments,
)

NATIVE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
NATIVE_PDF_MAX_BYTES = 10 * 1024 * 1024
# Per-message budget over every inlined attachment together (decoded bytes):
# inline-eligible files are admitted in attachment order until the running
# total reaches this; the rest ride the sandbox path only.
NATIVE_MESSAGE_MAX_BYTES = 10 * 1024 * 1024

__all__ = [
    "DaltonAttachmentBudgetMiddleware",
    "NATIVE_IMAGE_MAX_BYTES",
    "NATIVE_MESSAGE_MAX_BYTES",
    "NATIVE_PDF_MAX_BYTES",
    "NATIVE_REQUEST_MAX_BYTES",
    "READ_INLINE_MAX_BYTES",
    "decoded_size",
    "reading_path_text",
    "split_oversized",
    "trim_request_inline_attachments",
]


def decoded_size(data: str) -> int:
    """Decoded byte size of a data-URL payload (the base64 chars after ``,``)."""
    payload = data.split(",", 1)[1] if "," in data else data
    return len(payload) * 3 // 4


def split_oversized(contexts: list[Any]) -> tuple[list[Any], list[Any]]:
    """(inline, sandbox-only) for image/PDF contexts the model supports."""
    inline: list[Any] = []
    oversized: list[Any] = []
    inline_total = 0
    for ctx in contexts:
        data = ctx.data if hasattr(ctx, "data") else ctx.get("data", "")
        cap = NATIVE_PDF_MAX_BYTES if data.startswith("data:application/pdf") else NATIVE_IMAGE_MAX_BYTES
        size = decoded_size(data)
        fits_alone = size <= cap
        fits_message = inline_total + size <= NATIVE_MESSAGE_MAX_BYTES
        if fits_alone and fits_message:
            inline.append(ctx)
            inline_total += size
        else:
            oversized.append(ctx)
    return inline, oversized
