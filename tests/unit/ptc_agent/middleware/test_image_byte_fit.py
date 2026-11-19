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
        # DALTON-HOOK(dense-image-reading): the note asks for crops, without repeating that the image was not sent.
        assert "裁剪块" in note["text"]
        assert "原图未发送" not in note["text"]
        assert "当前不能执行代码时，请交给具备代码执行能力的代理生成裁剪块" in note["text"]
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

    @pytest.mark.asyncio
    async def test_ten_3mib_images_in_one_message_keep_newest_inside_budget(self):
        # 10 images of ~3.0 MiB each in a single HumanMessage (~30 MiB total > 20 MiB):
        # the newest 6 (~18 MiB) stay as image_url blocks and only the oldest 4
        # become left-out notes naming their workspace paths. An older tiny image
        # before the overflow still cannot displace a newer image.
        per_image = _noise_png(1050, 1000)
        assert 3 * 1024 * 1024 <= len(per_image) < _MAX_IMAGE_BYTES
        blocks: list[dict] = [
            {"type": "text", "text": "[Attached image: tiny0.png (saved to work/uploads/tiny0.png)]"},
            _image_block(_noise_png(40, 40)),
        ]
        originals: list[dict] = [blocks[1]]
        for n in range(1, 10):
            img_block = _image_block(_noise_png(1050, 1000))
            originals.append(img_block)
            blocks.append({
                "type": "text",
                "text": f"[Attached image: chart{n}.png (saved to work/uploads/chart{n}.png)]",
            })
            blocks.append(img_block)

        history = [HumanMessage(content=blocks, id="msg-10")]
        result, unreadable, left_out = await fit_images(
            history, COMMON_IMAGE_TYPES, lambda count: 8000
        )

        assert unreadable == frozenset()
        out_blocks = result[0].content
        assert len(out_blocks) == 20
        # Oldest 4 images (tiny0 + chart1..chart3) became left-out notes; tiny0 did not jump the queue.
        expected_left_out = ["tiny0.png", "chart1.png", "chart2.png", "chart3.png"]
        for idx, name in enumerate(expected_left_out):
            note = out_blocks[2 * idx + 1]
            assert note["type"] == "text"
            assert name in note["text"]
            assert f"work/uploads/{name}" in note["text"]
            assert "can be viewed with Read" in note["text"]
        # Newest 6 images (chart4..chart9) stay byte-identical.
        for idx in range(4, 10):
            kept = out_blocks[2 * idx + 1]
            assert kept["type"] == "image_url"
            assert kept == originals[idx]
        assert left_out == frozenset({"msg-10:1", "msg-10:3", "msg-10:5", "msg-10:7"})

    @pytest.mark.asyncio
    async def test_already_left_out_blocks_excluded_from_count_and_rescaling(self):
        # Blocks in already_left_out are excluded up front from total and located,
        # so replay count only reflects surviving images and skipped blocks are never re-scaled.
        small = _noise_png(20, 20)
        wide_img = Image.new("RGB", (2500, 100), color=(10, 20, 30))
        wide_buf = io.BytesIO()
        wide_img.save(wide_buf, "PNG")
        wide_block = _image_block(wide_buf.getvalue())

        m1_blocks: list[dict] = []
        for k in range(5):
            m1_blocks.append({"type": "text", "text": f"[Attached image: old{k}.png (saved to work/uploads/old{k}.png)]"})
            m1_blocks.append(_image_block(small))
        m2_blocks: list[dict] = [wide_block] + [_image_block(small) for _ in range(15)]
        history = [
            HumanMessage(content=m1_blocks, id="m1"),
            HumanMessage(content=m2_blocks, id="m2"),
        ]
        seen_counts: list[int] = []

        def max_side(count: int) -> int:
            seen_counts.append(count)
            return 2000 if count >= 21 else 8000

        # Pre-seed m1:1 as already left out -> only 20 surviving images count (keeps 2500px wide image).
        first_res, _, first_left = await fit_images(
            history, COMMON_IMAGE_TYPES, max_side, already_left_out={"m1:1"}
        )
        # Replaying without new images yields the exact same count (20) and byte-identical wide block.
        second_res, unreadable, second_left = await fit_images(
            history, COMMON_IMAGE_TYPES, max_side, already_left_out=first_left
        )

        assert unreadable == frozenset()
        assert seen_counts == [20, 20]
        assert first_res[0].content[1]["type"] == "text"
        assert second_res[1].content[0] == first_res[1].content[0] == wide_block
        assert second_left == first_left == frozenset({"m1:1"})
