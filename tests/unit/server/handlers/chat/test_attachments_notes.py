"""What the model is told about attachments it cannot view inline.

An attachment that failed to save must not collapse into a bare filename
(the model would ignore it) — the note has to tell the model to send the
user back to the file panel. The size-splitting and per-request budget this
file used to pin were replaced by the upstream B5 attachment pipeline
(#599 per-call image fit, #607 paged PDF reads and overflow-to-compaction),
so only the note contract remains.
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
async def test_an_attachment_without_a_sandbox_says_so_too(monkeypatch):
    content = await _run(
        monkeypatch,
        [_attachment("image/png", 512, "lost.png")],
        None,
    )
    assert "saving it to the workspace failed" in content
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
