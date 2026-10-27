"""Which chat attachments go to the model inline and which only to the sandbox.

Providers refuse an inline image above about 5 MB and a request above about
32 MB (Anthropic); Gemini takes about 20 MB inline. An attachment inlined into
the user message also stays in the checkpoint and is resent every turn, so one
that is too large breaks the thread rather than one call.
"""

from __future__ import annotations

from typing import Any

NATIVE_IMAGE_MAX_BYTES = 5 * 1024 * 1024
NATIVE_PDF_MAX_BYTES = 20 * 1024 * 1024


def _decoded_size(data: str) -> int:
    payload = data.split(",", 1)[1] if "," in data else data
    return len(payload) * 3 // 4


def split_oversized(contexts: list[Any]) -> tuple[list[Any], list[Any]]:
    """(inline, sandbox-only) for image/PDF contexts the model supports."""
    inline: list[Any] = []
    oversized: list[Any] = []
    for ctx in contexts:
        data = ctx.data if hasattr(ctx, "data") else ctx.get("data", "")
        cap = NATIVE_PDF_MAX_BYTES if data.startswith("data:application/pdf") else NATIVE_IMAGE_MAX_BYTES
        (oversized if _decoded_size(data) > cap else inline).append(ctx)
    return inline, oversized
