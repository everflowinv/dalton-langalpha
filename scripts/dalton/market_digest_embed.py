#!/usr/bin/env python3
"""Embed one bounded batch from stdin. Stdout is a single JSON object.

The parent supplies ``GEMINI_API_KEY`` and the JSON body. This process does
not read a config file, a dotenv file, or a profile, and it does not import
the embedding client until the request has been checked.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys

_MAX_STDIN_BYTES = 2 * 1024 * 1024
_MAX_TEXTS = 100
_MAX_TEXT_CHARS = 3000
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")
_SAFE_CODES = frozenset({
    "invalid_request",
    "invalid_texts",
    "invalid_model",
    "embedding_not_configured",
    "embed_failed",
})


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    sys.stdout.write("\n")


def _fail(code: str) -> int:
    safe = code if code in _SAFE_CODES else "embed_failed"
    _emit({"ok": False, "code": safe})
    return 1


def _validate(loaded: object) -> str | None:
    if not isinstance(loaded, dict) or set(loaded) != {"texts", "model"}:
        return "invalid_request"
    texts = loaded.get("texts")
    model = loaded.get("model")
    if not isinstance(texts, list) or not 1 <= len(texts) <= _MAX_TEXTS:
        return "invalid_texts"
    for text in texts:
        if not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS or "\x00" in text:
            return "invalid_texts"
    if (
        not isinstance(model, str)
        or _MODEL_RE.fullmatch(model) is None
        or ".." in model
    ):
        return "invalid_model"
    return None


def _vector(item: object) -> list[float]:
    values = getattr(item, "values", None)
    if values is None and isinstance(item, dict):
        values = item.get("values")
    if isinstance(values, (str, bytes)) or not isinstance(values, list) or not values:
        raise ValueError("shape")
    row: list[float] = []
    for number in values:
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("type")
        value = float(number)
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        row.append(value)
    return row


def _embed(texts: list[str], model: str, api_key: str) -> list[list[float]]:
    import google.genai as genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=60000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    response = client.models.embed_content(
        model=model,
        contents=texts,
        config={"task_type": "RETRIEVAL_DOCUMENT"},
    )
    embeddings = getattr(response, "embeddings", None)
    if embeddings is None and isinstance(response, dict):
        embeddings = response.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise ValueError("count")
    return [_vector(item) for item in embeddings]


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(raw) > _MAX_STDIN_BYTES:
        return _fail("invalid_request")
    try:
        loaded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _fail("invalid_request")
    code = _validate(loaded)
    if code is not None:
        return _fail(code)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return _fail("embedding_not_configured")
    try:
        vectors = _embed(loaded["texts"], loaded["model"], api_key)
    except Exception:
        return _fail("embed_failed")
    try:
        _emit({"ok": True, "embeddings": vectors})
    except (TypeError, ValueError):
        return _fail("embed_failed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        _fail("embed_failed")
        raise SystemExit(1)
