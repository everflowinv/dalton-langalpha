"""What the model is told about attachments it cannot view inline.

An attachment that failed to save must not collapse into a bare filename
(the model would ignore it) — the note has to tell the model to send the
user back to the file panel. A small readable image with no sandbox to save
to still inlines: the block itself is its own copy, so nothing is stranded.
An unsaved PDF over the inline ceiling (``INLINE_UNSAVED_PDF_MAX_BYTES``)
does not inline at all — a whole PDF in the checkpoint re-sends on every
later turn — and takes the save-failure note instead. The per-image byte
splitting this file used to pin was replaced by the call-time fit
(``test_image_byte_fit.py``), so only the routing contract remains.
"""

from __future__ import annotations

import base64
import io
import os
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from PIL import Image

from src.server.handlers.chat import attachments as attachments_mod
from src.server.handlers.chat.attachments import attach_request_files
from src.server.models.chat import ChatRequest
from src.server.utils.multimodal_context import INLINE_UNSAVED_PDF_MAX_BYTES


def _attachment(mime: str, size: int, name: str) -> dict:
    data = base64.b64encode(b"\0" * size).decode()
    return {"type": "image", "data": f"data:{mime};base64,{data}", "description": name}


def _png_attachment(name: str) -> dict:
    """A small image that really decodes, so it rides the inline path."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, "PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    return {"type": "image", "data": f"data:image/png;base64,{data}", "description": name}


def _pdf_attachment(size: int, name: str) -> dict:
    data = base64.b64encode(b"%PDF-1.4\n" + os.urandom(size)).decode()
    return {"type": "image", "data": f"data:application/pdf;base64,{data}", "description": name}


def _request(ctxs: list[dict]) -> ChatRequest:
    return ChatRequest(messages=[], additional_context=ctxs)


async def _run(monkeypatch, ctxs: list[dict], file_paths: list[str | None] | None):
    """attach_request_files with the modalities and upload stubbed; the notes text back."""
    if file_paths is None:
        session = None
    else:
        session = SimpleNamespace(sandbox=object())
        monkeypatch.setattr(
            attachments_mod,
            "upload_to_sandbox",
            create_autospec(attachments_mod.upload_to_sandbox, return_value=file_paths),
        )
    monkeypatch.setattr(
        attachments_mod,
        "_modalities",
        create_autospec(attachments_mod._modalities, return_value=["text", "image", "pdf"]),
    )
    messages = [{"role": "user", "content": "hi"}]
    await attach_request_files(messages, _request(ctxs), session, "any-model", None)
    return messages[-1]["content"]


@pytest.mark.asyncio
async def test_an_attachment_that_failed_to_save_says_so(monkeypatch):
    # Not a bare filename: the model must tell the user to reupload from the
    # file panel instead of silently dropping the attachment.
    content = await _run(
        monkeypatch,
        [_attachment("text/csv", 512, "lost.csv")],
        [None],
    )
    assert "saving it to the workspace failed" in content
    assert "file panel" in content


@pytest.mark.asyncio
async def test_an_undecodable_image_without_a_sandbox_gets_the_note(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("image/png", 512, "lost.png")],
        None,
    )
    assert "saving it to the workspace failed" in content
    assert "file panel" in content


@pytest.mark.asyncio
async def test_a_small_image_without_a_sandbox_inlines_and_says_so(monkeypatch):
    """No sandbox to save to is no reason to drop the note: the inline block
    is its own copy, and the reminder beside it says the workspace copy is
    what is missing and sends the user back to the file panel."""
    content = await _run(
        monkeypatch,
        [_png_attachment("ok.png")],
        None,
    )
    assert isinstance(content, list)
    texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    assert any("[Attached image: ok.png]" in b["text"] for b in texts)
    assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in content)
    assert any(
        "shown inline above" in b["text"]
        and "saving it to the workspace failed" in b["text"]
        and "file panel" in b["text"]
        for b in texts
    )


@pytest.mark.asyncio
async def test_an_image_whose_upload_failed_inlines_with_the_note_too(monkeypatch):
    """A sandbox that refuses the upload is the same state: the block rides,
    the reminder says no workspace copy exists."""
    content = await _run(
        monkeypatch,
        [_png_attachment("late.png")],
        [None],
    )
    assert isinstance(content, list)
    assert any(isinstance(b, dict) and b.get("type") == "image_url" for b in content)
    texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    assert any(
        "shown inline above" in b["text"] and "file panel" in b["text"] for b in texts
    )


@pytest.mark.asyncio
async def test_an_unsaved_pdf_within_the_ceiling_inlines_whole(monkeypatch):
    size = INLINE_UNSAVED_PDF_MAX_BYTES // 2
    content = await _run(
        monkeypatch,
        [_pdf_attachment(size, "small.pdf")],
        None,
    )
    assert isinstance(content, list)
    [file_block] = [
        b for b in content if isinstance(b, dict) and b.get("type") == "file"
    ]
    assert file_block["mime_type"] == "application/pdf"
    assert file_block["unsaved"] is True
    assert file_block["filename"] == "small.pdf"


@pytest.mark.asyncio
async def test_an_unsaved_pdf_over_the_ceiling_is_not_inlined(monkeypatch):
    size = INLINE_UNSAVED_PDF_MAX_BYTES + 1024 * 1024
    content = await _run(
        monkeypatch,
        [_pdf_attachment(size, "big.pdf")],
        None,
    )
    # The save-failure note instead of a whole PDF the checkpoint would
    # re-send on every later turn.
    assert isinstance(content, str)
    assert "big.pdf" in content
    assert "saving it to the workspace failed" in content
    assert "file panel" in content
    assert "base64" not in content


@pytest.mark.asyncio
async def test_an_unsaved_pdf_of_exactly_the_ceiling_inlines(monkeypatch):
    """Padding is not payload: a PDF whose decoded size sits exactly at the
    ceiling inlines, not a hair over it through the base64 tail."""
    from src.server.utils.multimodal_context import data_url_decoded_size

    raw = b"%PDF-1.4\n" + os.urandom(INLINE_UNSAVED_PDF_MAX_BYTES - len(b"%PDF-1.4\n"))
    assert len(raw) == INLINE_UNSAVED_PDF_MAX_BYTES
    data_url = (
        "data:application/pdf;base64,"
        + base64.b64encode(raw).decode()
    )
    assert data_url_decoded_size(data_url) == INLINE_UNSAVED_PDF_MAX_BYTES
    content = await _run(
        monkeypatch,
        [{"type": "image", "data": data_url, "description": "edge.pdf"}],
        None,
    )
    assert isinstance(content, list)
    [file_block] = [
        b for b in content if isinstance(b, dict) and b.get("type") == "file"
    ]
    assert file_block["unsaved"] is True
    # The inline copy rides, with the unsaved reminder beside it.
    texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
    assert any("shown inline above" in b["text"] for b in texts)


@pytest.mark.asyncio
async def test_a_saved_pdf_is_never_inlined_whatever_its_size(monkeypatch):
    """A saved composer PDF rides a one-line note with its path, the upstream
    contract; the ceiling only governs the unsaved ones."""
    size = INLINE_UNSAVED_PDF_MAX_BYTES + 1024 * 1024
    content = await _run(
        monkeypatch,
        [_pdf_attachment(size, "saved.pdf")],
        ["work/uploads/saved.pdf_x1.pdf"],
    )
    assert isinstance(content, list)
    # The one-line label, and no file block carrying the PDF itself.
    assert not any(isinstance(b, dict) and b.get("type") == "file" for b in content)
    texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
    assert any(
        "[Attached PDF: saved.pdf (saved to work/uploads/saved.pdf_x1.pdf)]" in t
        for t in texts
    )


@pytest.mark.asyncio
async def test_a_plain_saved_file_keeps_the_short_python_note(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("text/csv", 512, "data.csv")],
        ["work/uploads/data.csv_x1.csv"],
    )
    assert "Use Python to process it." in content
    assert "PIL" not in content


def test_decoded_size_never_undercounts_malformed_payloads():
    """Over-padded or non-base64 payloads report their raw length, so a file
    just past the ceiling cannot read as under it."""
    from src.server.utils.multimodal_context import data_url_decoded_size

    raw = b"x" * (INLINE_UNSAVED_PDF_MAX_BYTES + 1)
    over_padded = (
        "data:application/pdf;base64," + base64.b64encode(raw).decode() + "===="
    )
    assert base64.b64decode(over_padded.split(",", 1)[1]) == raw
    assert data_url_decoded_size(over_padded) > INLINE_UNSAVED_PDF_MAX_BYTES
    assert data_url_decoded_size("data:application/pdf,ABCD") == 4
    assert data_url_decoded_size("data:application/pdf;base64,=") == 0
    assert data_url_decoded_size("no-comma") == 0
