"""What the model is told about attachments it cannot view inline.

A too-large attachment that also failed to save must not collapse into a bare
filename (the model would ignore it), and a saved one needs the reading path
that fits its size (PIL shrink for big images, paged Read for big PDFs).
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.server.handlers.chat import attachments as attachments_mod
from src.server.handlers.chat.attachments import attach_request_files
from src.server.models.chat import ChatRequest


def _attachment(mime: str, size: int, name: str) -> dict:
    data = base64.b64encode(b"\0" * size).decode()
    return {"type": "image", "data": f"data:{mime};base64,{data}", "description": name}


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
async def test_a_saved_oversized_image_gets_the_pil_shrink_path(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("image/png", 6 * 1024 * 1024, "big.png")],
        ["work/uploads/big.png_x1.png"],
    )
    assert "work/uploads/big.png_x1.png" in content
    assert "5 MB" in content
    assert "PIL" in content
    assert "Read" in content


@pytest.mark.asyncio
async def test_a_saved_oversized_pdf_gets_the_paged_read_path(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("application/pdf", 11 * 1024 * 1024, "big.pdf")],
        ["work/uploads/big.pdf_x1.pdf"],
    )
    assert "page by page" in content
    assert "Python" in content


@pytest.mark.asyncio
async def test_an_oversized_attachment_that_failed_to_save_says_so(monkeypatch):
    # Not a bare filename: the model must tell the user to reupload from the
    # file panel instead of silently dropping the attachment.
    content = await _run(
        monkeypatch,
        [_attachment("application/pdf", 11 * 1024 * 1024, "lost.pdf")],
        [None],
    )
    assert "exceeds the size that can be viewed inline" in content
    assert "saving it to the workspace failed" in content
    assert "file panel" in content


@pytest.mark.asyncio
async def test_an_oversized_attachment_without_a_sandbox_says_so_too(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("image/png", 6 * 1024 * 1024, "lost.png")],
        None,
    )
    assert "exceeds the size that can be viewed inline" in content
    assert "file panel" in content


@pytest.mark.asyncio
async def test_a_plain_saved_file_keeps_the_short_python_note(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("text/csv", 512, "data.csv")],
        ["work/uploads/data.csv_x1.csv"],
    )
    assert "Use Python to process it." in content
    assert "PIL" not in content
