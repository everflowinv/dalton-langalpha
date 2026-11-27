#!/usr/bin/env python3
"""Live Antigravity multimodal probe (read-only; intended for manual prod execution).

Uses the application's existing Antigravity OAuth credential lookup (strictly
read-only, never writing back to the database and never printing tokens) and
``create_llm`` client builder to send four tiny probes to a specified
Antigravity model:

1. User turn with an inline generated PNG showing a 4-digit number.
2. Simulated ``Read`` tool call whose ``ToolMessage`` carries the same PNG.
3. Simulated ``Read`` tool call whose ``ToolMessage`` carries a 1-page PDF
   showing a 4-digit number.
4. Two parallel ``Read`` tool calls whose ``ToolMessage`` results each carry a
   PNG showing a different 4-digit number (sent as one user turn: FR1, FR2,
   image 1, image 2).

Each probe asks for the number(s), prints the model's answer and PASS/FAIL, and
exits 0 only when all four probes pass.

Usage (inside the prod backend container)::

    uv run python scripts/dalton/ops/prod/probe_antigravity_multimodal.py \\
        --model gemini-4-argon-medium-oauth

    uv run python scripts/dalton/ops/prod/probe_antigravity_multimodal.py \\
        --model claude-sonnet-5-5-medium-oauth
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import os
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from PIL import Image, ImageDraw

from src.ptc_agent.agent.middleware.file_operations.multimodal import build_content_blocks
from src.ptc_agent.agent.transcript.render import visible_text

# 5x7 crisp pixel-art digit bitmaps so image rendering never depends on system TTF fonts.
_DIGIT_BITMAPS: dict[str, tuple[str, ...]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00110", "01000", "10000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("01110", "10000", "11110", "10001", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}

_DUMMY_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "Read a file from disk.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file."}
            },
            "required": ["file_path"],
        },
    },
}

_ANTIGRAVITY_PROVIDER = "antigravity-oauth"

_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(refresh[-_]?token|id[-_]?token|cookie|set-cookie|x-goog-api-key)"
            r"(\s*[=:]\s*)([\"']?)[^\s\"',;)}\]]+"
        ),
        r"\1\2\3[REDACTED]",
    ),
    (re.compile(r"\bya29\.[A-Za-z0-9._~+/=-]+"), "[REDACTED]"),
    (re.compile(r"(?i)\bheaders\s*[=:]\s*\{[^}]*\}"), "headers=[REDACTED]"),
)


def _client_secret_values(client: Any) -> tuple[str, ...]:
    """Extract raw token values from ``client`` solely for exact-match redaction."""
    raw = getattr(client, "api_key", None)
    if raw is None:
        return ()
    getter = getattr(raw, "get_secret_value", None)
    val = getter() if callable(getter) else (raw if isinstance(raw, str) else "")
    return (val,) if isinstance(val, str) and val else ()


def _format_safe_error(exc: BaseException, *, secrets: Sequence[str] = ()) -> str:
    """Return a concise, single-line error summary free of headers and tokens."""
    from src.server.utils.error_sanitization import sanitize_error_text, single_line

    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)

    raw = str(exc) or type(exc).__name__
    for secret in secrets:
        if secret:
            raw = raw.replace(secret, "[REDACTED]")
    sanitized = sanitize_error_text(raw)
    for pattern, repl in _EXTRA_SECRET_PATTERNS:
        sanitized = pattern.sub(repl, sanitized)
    sanitized = single_line(sanitized).strip()[:200]
    # Only the exception type and HTTP status leave the probe: upstream messages may echo headers.
    del sanitized
    return f"{type(exc).__name__}(status={status})" if status is not None else type(exc).__name__


def make_number_png(digits: str, *, scale: int = 12, padding: int = 24) -> bytes:
    """Generate a high-contrast PNG displaying ``digits`` with zero external font dependency."""
    clean = "".join(ch for ch in digits if ch in _DIGIT_BITMAPS)
    if not clean:
        raise ValueError("digits must contain at least one ASCII digit 0-9")
    glyph_w = 5 * scale
    glyph_h = 7 * scale
    gap = 2 * scale
    width = padding * 2 + len(clean) * glyph_w + max(0, len(clean) - 1) * gap
    height = padding * 2 + glyph_h

    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    for idx, ch in enumerate(clean):
        ox = padding + idx * (glyph_w + gap)
        for row_i, row in enumerate(_DIGIT_BITMAPS[ch]):
            for col_i, bit in enumerate(row):
                if bit == "1":
                    x0 = ox + col_i * scale
                    y0 = padding + row_i * scale
                    draw.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_number_pdf(digits: str) -> bytes:
    """Generate a minimal, spec-valid 1-page PDF 1.4 displaying ``digits``."""
    escaped = digits.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 48 Tf\n72 700 Td\n({escaped}) Tj\nET\n".encode("ascii")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream\nendobj\n"
        ),
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for obj in objects:
        offsets.append(len(out))
        out.extend(obj)

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def _extract_answer_text(msg: Any) -> str:
    if isinstance(msg, AIMessage):
        return visible_text(msg.content).strip()
    return str(getattr(msg, "content", msg) or "").strip()


def _answer_matches(answer: str, expected: str) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(expected)}(?!\d)", answer))


def _answer_matches_all(answer: str, expected: Sequence[str]) -> bool:
    return bool(expected) and all(_answer_matches(answer, code) for code in expected)


async def _resolve_user_id(explicit_user_id: str | None) -> str:
    if explicit_user_id and explicit_user_id.strip():
        return explicit_user_id.strip()
    from src.dalton.credentials import get_team_credential_holder_id_async

    holder = await get_team_credential_holder_id_async()
    if holder:
        return holder
    env_uid = os.getenv("DALTON_PROBE_USER_ID", "").strip()
    if env_uid:
        return env_uid
    raise RuntimeError(
        "Unable to resolve credential holder user_id automatically; pass --user-id explicitly."
    )


async def _build_readonly_client(model_name: str, user_id: str) -> Any:
    """Resolve an Antigravity OAuth client without writing tokens back to PostgreSQL."""
    from src.dalton.llm.providers.antigravity.oauth import refresh_tokens as refresh_ag_tokens
    from src.llms.llm import LLM as LLMFactory, create_llm
    from src.server.database.oauth_tokens import get_oauth_tokens
    from src.server.services.llm.clients import subscription_route

    mc = LLMFactory.get_model_config()
    route = subscription_route(model_name, mc, custom_entry=None)
    if route is None:
        raise RuntimeError(f"Model {model_name!r} is not a subscription/OAuth model in the catalog.")

    model_info = route.model_info
    provider = str(model_info.get("provider") or "")
    if provider != _ANTIGRAVITY_PROVIDER:
        raise RuntimeError(
            f"Model {model_name!r} uses provider={provider!r}, not {_ANTIGRAVITY_PROVIDER!r}. "
            "Pass an Antigravity catalog model (e.g. gemini-4-argon-medium-oauth or "
            "claude-sonnet-5-5-medium-oauth)."
        )

    row = await get_oauth_tokens(user_id, provider)
    if not row or not row.get("access_token"):
        raise RuntimeError(f"No stored OAuth token found for provider={provider!r}.")

    access_token = str(row["access_token"])
    expires_at = row.get("expires_at")
    now = datetime.now(timezone.utc)
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expires_at = None
    if isinstance(expires_at, datetime):
        exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if exp <= now + timedelta(seconds=60):
            refresh_tok = str(row.get("refresh_token") or "")
            if not refresh_tok:
                raise RuntimeError(
                    f"Stored OAuth token for provider={provider!r} is expired and has no refresh_token."
                )
            refreshed = await refresh_ag_tokens(refresh_tok)
            access_token = str(refreshed["access_token"])

    return create_llm(model_name, api_key=access_token)


def _build_probe_messages(
    img_b64: str,
    pdf_b64: str,
    img2_b64: str | None = None,
) -> list[tuple[str, tuple[str, ...], list[Any], bool]]:
    img_blocks = build_content_blocks(img_b64, "/tmp/probe_number.png", "image/png")
    img2_payload = img2_b64 or base64.b64encode(make_number_png("4816")).decode("ascii")
    img2_blocks = build_content_blocks(img2_payload, "/tmp/probe_number_2.png", "image/png")
    pdf_blocks = build_content_blocks(
        pdf_b64, "/tmp/probe_number.pdf", "application/pdf", pages=1
    )

    probe_1 = [
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "What 4-digit number is shown in this image? Reply with the digits only.",
                },
                img_blocks[1],
            ]
        )
    ]

    probe_2 = [
        HumanMessage(content="Read /tmp/probe_number.png and tell me the 4-digit number inside. Reply with digits only."),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "probe_call_img",
                    "name": "Read",
                    "args": {"file_path": "/tmp/probe_number.png"},
                }
            ],
        ),
        ToolMessage(content=img_blocks, tool_call_id="probe_call_img", name="Read"),
    ]

    probe_3 = [
        HumanMessage(content="Read /tmp/probe_number.pdf and tell me the 4-digit number inside. Reply with digits only."),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "probe_call_pdf",
                    "name": "Read",
                    "args": {"file_path": "/tmp/probe_number.pdf"},
                }
            ],
        ),
        ToolMessage(content=pdf_blocks, tool_call_id="probe_call_pdf", name="Read"),
    ]

    probe_4 = [
        HumanMessage(
            content=(
                "Read both /tmp/probe_number.png and /tmp/probe_number_2.png and tell me both "
                "4-digit numbers separated by a comma. Reply with the two numbers only."
            )
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "probe_call_parallel_1",
                    "name": "Read",
                    "args": {"file_path": "/tmp/probe_number.png"},
                },
                {
                    "id": "probe_call_parallel_2",
                    "name": "Read",
                    "args": {"file_path": "/tmp/probe_number_2.png"},
                },
            ],
        ),
        ToolMessage(content=img_blocks, tool_call_id="probe_call_parallel_1", name="Read"),
        ToolMessage(content=img2_blocks, tool_call_id="probe_call_parallel_2", name="Read"),
    ]

    return [
        ("1. user_turn_image", ("7391",), probe_1, False),
        ("2. tool_result_image", ("7391",), probe_2, True),
        ("3. tool_result_pdf", ("5924",), probe_3, True),
        ("4. parallel_tool_result_images", ("7391", "4816"), probe_4, True),
    ]


async def run_probes(model_name: str, user_id: str | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)

    from src.server.database.pool import get_or_create_pool

    pool = get_or_create_pool()
    pool_opened = False
    known_secrets: tuple[str, ...] = ()
    try:
        await pool.open()
        pool_opened = True
        resolved_uid = await _resolve_user_id(user_id)
        client = await _build_readonly_client(model_name, resolved_uid)
        known_secrets = _client_secret_values(client)
        tool_client = client.bind_tools([_DUMMY_READ_TOOL])

        img_b64 = base64.b64encode(make_number_png("7391")).decode("ascii")
        img2_b64 = base64.b64encode(make_number_png("4816")).decode("ascii")
        pdf_b64 = base64.b64encode(make_number_pdf("5924")).decode("ascii")
        probes = _build_probe_messages(img_b64, pdf_b64, img2_b64)

        print(f"Probing model={model_name} (read-only; {len(probes)} requests)")
        all_ok = True
        for label, expected, messages, use_tools in probes:
            target = tool_client if use_tools else client
            try:
                reply = await target.ainvoke(messages)
                answer = _extract_answer_text(reply)
                matched = _answer_matches_all(answer, expected)
                status = "PASS" if matched else "FAIL"
                if not matched:
                    all_ok = False
                snippet = answer.replace("\n", " ")[:120]
                expected_str = ",".join(expected)
                print(f"[{status}] {label}: expected={expected_str} answer={snippet!r}")
            except Exception as exc:
                all_ok = False
                print(f"[FAIL] {label}: {_format_safe_error(exc, secrets=known_secrets)}")
        return 0 if all_ok else 1
    except Exception as exc:
        print(f"[FAIL] setup: {_format_safe_error(exc, secrets=known_secrets)}")
        return 1
    finally:
        if pool_opened:
            await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only live multimodal probe for Antigravity OAuth models."
    )
    parser.add_argument(
        "--model",
        required=True,
        help=(
            "Antigravity OAuth catalog model key "
            "(e.g. gemini-4-argon-medium-oauth or claude-sonnet-5-5-medium-oauth)."
        ),
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Optional user_id for OAuth lookup (defaults to team credential holder).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run_probes(args.model, user_id=args.user_id))


if __name__ == "__main__":
    raise SystemExit(main())
