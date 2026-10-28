"""The byte ceilings under the pixel fit: one image, and the request together.

The pixel limits bound neither — a 3000x2000 PNG sits under every side limit
and still carries 15 MB, which a provider refuses as a plain 400 the overflow
classification never matches, so the thread fails on every later call. The
ceilings are enforced on the request copy, per call, exactly like the pixel
fit: history keeps the original, in every case here.
"""

import base64
import io
import os
import struct
import zlib

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image

from ptc_agent.agent.middleware.file_operations import image_fit
from ptc_agent.agent.middleware.file_operations.image_fit import (
    _MAX_IMAGE_BYTES,
    _MAX_REQUEST_IMAGE_BYTES,
    fit_images,
)
from src.llms.llm import COMMON_IMAGE_TYPES


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _image_block(raw: bytes, mime: str = "image/png") -> dict:
    return {"type": "image_url", "image_url": {"url": _data_url(raw, mime)}}


def _noise_png(width: int, height: int) -> bytes:
    """A PNG random noise keeps near its raw size: too many bytes for its
    pixels, in both PNG and every JPEG the ladder can make of it."""
    img = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    img.save(buffer, "PNG")
    return buffer.getvalue()


def _corrupt_png(width: int, height: int, filler: bytes) -> bytes:
    """A PNG whose header sniffs fine and whose payload no decoder opens: the
    ladder has nothing to shrink, so the note is all that can go out."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    return (
        b"\x89PNG\r\n\x1a\n"
        + ihdr
        + chunk(b"IDAT", filler)
        + chunk(b"IEND", b"")
    )


def _decoded_size(block: dict) -> int:
    payload = block["image_url"]["url"].split(",", 1)[1]
    return len(payload) * 3 // 4


def _media_type(block: dict) -> str:
    return block["image_url"]["url"].split(";", 1)[0].removeprefix("data:")


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.setattr(image_fit, "_MEMO", image_fit._Memo(budget=1 << 20))


class TestPerImageBytes:
    @pytest.mark.asyncio
    async def test_a_15mb_png_is_brought_under_the_ceiling_and_sent(self):
        raw = _noise_png(3000, 2000)
        assert len(raw) > _MAX_IMAGE_BYTES  # the pixel limits never bound this
        original = _image_block(raw)
        history = [HumanMessage(content=[{"type": "text", "text": "look"}, original])]

        result, unreadable, _left_out = await fit_images(
            history, COMMON_IMAGE_TYPES, lambda count: 8000
        )

        assert unreadable == frozenset()
        label, fitted, note = result[0].content
        assert label == {"type": "text", "text": "look"}
        assert fitted["type"] == "image_url"
        assert _media_type(fitted) == "image/jpeg"
        assert _decoded_size(fitted) <= _MAX_IMAGE_BYTES
        assert note["type"] == "text"
        assert "re-encoded" in note["text"]
        # The checkpoint's copy keeps the original bytes.
        assert history[0].content == [{"type": "text", "text": "look"}, original]

    @pytest.mark.asyncio
    async def test_an_image_no_step_can_fit_is_replaced_by_a_note(self):
        raw = _corrupt_png(3000, 2000, os.urandom(6 * 1024 * 1024))
        original = _image_block(raw)
        history = [
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": "[Attached image: scan.png (saved to work/uploads/scan.png)]",
                    },
                    original,
                ]
            )
        ]

        result, unreadable, _left_out = await fit_images(
            history, COMMON_IMAGE_TYPES, lambda count: 8000
        )

        assert unreadable == frozenset()
        [label, note] = result[0].content
        assert label["text"].startswith("[Attached image: scan.png")
        assert note["type"] == "text"
        assert "scan.png" in note["text"]
        assert "was not sent" in note["text"]
        assert "work/uploads/scan.png" in note["text"]
        assert "PIL" in note["text"]
        # No image block rides the request.
        assert not any(block.get("type") == "image_url" for block in result[0].content)
        # History keeps what could not be sent.
        assert history[0].content[1] == original


class TestRequestTotal:
    @pytest.mark.asyncio
    async def test_the_oldest_images_are_left_out_when_the_total_overflows(self):
        # Each under the per-image ceiling, together over the request's: the
        # newest four stay and the oldest one becomes a note.
        per_image = 1240 * 1240 * 3  # ~4.4 MB of noise, PNG keeps it all
        assert per_image < _MAX_IMAGE_BYTES
        assert 5 * per_image > _MAX_REQUEST_IMAGE_BYTES
        originals = []
        history = []
        for n in range(5):
            block = _image_block(_noise_png(1240, 1240))
            originals.append(block)
            history.append(
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": f"[Attached image: chart{n}.png (saved to work/uploads/chart{n}.png)]",
                        },
                        block,
                    ]
                )
            )

        result, unreadable, _left_out = await fit_images(
            history, COMMON_IMAGE_TYPES, lambda count: 8000
        )

        assert unreadable == frozenset()
        [label, note] = result[0].content
        assert label["text"].startswith("[Attached image: chart0.png")
        assert note["type"] == "text"
        assert "chart0.png" in note["text"]
        assert "left out of this request" in note["text"]
        assert "work/uploads/chart0.png" in note["text"]
        for n, message in enumerate(result[1:], start=1):
            [kept_label, kept] = message.content
            assert kept["type"] == "image_url"
            # A kept image is sent byte-identical: under every ceiling as is.
            assert kept == originals[n]
        # The checkpoint keeps all five.
        for n, message in enumerate(history):
            assert message.content[1] == originals[n]
