"""Direct backend embedding client for Gemini and OpenAI with deterministic retry/backoff.

Batching & parity contract (``researchdata-connectors.md`` §2.5 & §6.2):
- Maximum batch size: ``MAX_BATCH_SIZE = 100`` texts per upstream request.
- Gemini (``google.genai``):
  * Maps stored ``model_label`` (``gemini-embedding-2-preview``) to API model
    ``gemini-embedding-2`` via ``resolve_api_model_name()``.
  * Constructs one ``types.Content(parts=[types.Part(text=t)])`` per text item
    so ``gemini-embedding-2`` never folds a list of plain strings into a single
    vector (matching ``company_wiki_cli.py:219-229``).
  * For Everflow 3072-d, sends ``config={"task_type": task_type}`` verbatim
    (omitting ``output_dimensionality``) so cosine similarity vs the legacy
    host bridge equals 1.0. For non-3072-d (e.g. Core 1536-d), includes
    ``output_dimensionality``.
- OpenAI / OpenAI-compatible HTTP (``POST {base_url}/embeddings``):
  * Sends ``{"model": api_model_name, "input": batch}`` (plus ``"dimensions"``
    for ``text-embedding-3-*`` models) with ``Authorization: Bearer <key>``.
  * Sorts response items by ``index`` and validates vector count and length.

Retry & backoff policy table:
+----------+--------------------------+------------------------+----------------------+
| Provider | Failure class            | Backoff delays (s)     | Max attempts (total) |
+----------+--------------------------+------------------------+----------------------+
| gemini   | 429 / RESOURCE_EXHAUSTED | (5.0, 10.0, 20.0, 40.0)| 5                    |
| gemini   | 500/502/503/504/network  | (2.0, 5.0, 10.0)       | 4                    |
| openai   | 429 / rate_limit_exceeded| (2.0, 5.0, 10.0, 20.0) | 5 (honors Retry-After|
|          |                          |                        | capped at 60s)       |
| openai   | 500/502/503/504/network  | (1.0, 2.0, 5.0)        | 4                    |
| any      | 400/401/403/404/422/cfg/ | () — fail immediately  | 1                    |
|          | short count / dim error  |                        |                      |
+----------+--------------------------+------------------------+----------------------+
Interactive query calls pass ``retry=False`` (1 attempt total) so keyword search
fallback is never blocked by retry sleeps.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

import httpx

from src.dalton.embeddings.config import (
    DEFAULT_OPENAI_BASE_URL,
    EmbeddingConfig,
    EmbeddingConfigError,
    resolve_embedding_api_key,
    resolve_embedding_config,
    validate_embedding_task_type,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

MAX_BATCH_SIZE = 100
MAX_RETRY_AFTER_S = 60.0
DEFAULT_MAX_DIRECT_CONCURRENCY = 2
MAX_DIRECT_CONCURRENCY_UPPER_BOUND = 16

_LOOP_EMBED_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    tuple[int, asyncio.Semaphore],
] = weakref.WeakKeyDictionary()


def resolve_max_direct_concurrency() -> int:
    """Resolve bounded direct embedding concurrency (default 2, matching legacy bridge ``embed_sem``)."""
    raw = os.getenv("DALTON_EMBED_MAX_CONCURRENCY", "").strip()
    if not raw:
        return DEFAULT_MAX_DIRECT_CONCURRENCY
    try:
        parsed = int(raw)
        if parsed >= 1:
            return min(parsed, MAX_DIRECT_CONCURRENCY_UPPER_BOUND)
    except ValueError:
        pass
    logger.warning(
        "[dalton.embeddings] Ignoring invalid DALTON_EMBED_MAX_CONCURRENCY=%r; using %d",
        raw,
        DEFAULT_MAX_DIRECT_CONCURRENCY,
    )
    return DEFAULT_MAX_DIRECT_CONCURRENCY


def _get_direct_embed_semaphore() -> asyncio.Semaphore:
    """Return an ``asyncio.Semaphore`` bound to the current running event loop."""
    limit = resolve_max_direct_concurrency()
    loop = asyncio.get_running_loop()
    cached = _LOOP_EMBED_SEMAPHORES.get(loop)
    if cached is None or cached[0] != limit:
        sem = asyncio.Semaphore(limit)
        _LOOP_EMBED_SEMAPHORES[loop] = (limit, sem)
        return sem
    return cached[1]

GEMINI_RATE_LIMIT_DELAYS_S: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0)
GEMINI_TRANSIENT_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 10.0)

OPENAI_RATE_LIMIT_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0)
OPENAI_TRANSIENT_DELAYS_S: tuple[float, ...] = (1.0, 2.0, 5.0)

TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({500, 502, 503, 504})
PERMANENT_HTTP_STATUSES: frozenset[int] = frozenset({400, 401, 403, 404, 422})

_HTTP_CODE_RE = re.compile(r"\b(400|401|403|404|422|429|500|502|503|504)\b")


class EmbeddingError(RuntimeError):
    """Base error for backend embedding calls."""


class EmbeddingResponseError(EmbeddingError):
    """Raised when provider output has a mismatched vector count or dimension."""


class EmbeddingProviderHTTPError(EmbeddingError):
    """Non-2xx HTTP response from an embedding provider."""

    def __init__(
        self,
        provider: str,
        code: int,
        reason: str = "",
        *,
        retry_after_s: float | None = None,
    ) -> None:
        detail = f"{provider} embedding HTTP {code}: {reason}" if reason else f"{provider} embedding HTTP {code}"
        super().__init__(detail)
        self.provider = provider
        self.code = int(code)
        self.reason = reason
        self.retry_after_s = retry_after_s


def _parse_retry_after(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        val = float(raw.strip())
    except ValueError:
        return None
    if val < 0:
        return None
    return min(val, MAX_RETRY_AFTER_S)


def _classify_failure(exc: BaseException) -> tuple[str, float | None]:
    """Return ``(category, retry_after_s)`` where category is ``rate_limit``, ``transient``, or ``permanent``."""
    if isinstance(exc, (EmbeddingConfigError, EmbeddingResponseError)):
        return "permanent", None

    if isinstance(exc, EmbeddingProviderHTTPError):
        if exc.code == 429:
            return "rate_limit", exc.retry_after_s
        if exc.code in TRANSIENT_HTTP_STATUSES:
            return "transient", exc.retry_after_s
        return "permanent", None

    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError, TimeoutError, ConnectionError)):
        return "transient", None

    # Inspect SDK exceptions (e.g. google.genai.errors.APIError)
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 429:
            return "rate_limit", None
        if status_code in TRANSIENT_HTTP_STATUSES:
            return "transient", None
        if status_code in PERMANENT_HTTP_STATUSES:
            return "permanent", None

    msg = str(exc)
    upper = msg.upper()
    if "429" in msg or "RESOURCE_EXHAUSTED" in upper or "RATE_LIMIT" in upper or "RATE LIMIT" in upper:
        return "rate_limit", None

    code_match = _HTTP_CODE_RE.search(msg)
    if code_match:
        code = int(code_match.group(1))
        if code == 429:
            return "rate_limit", None
        if code in TRANSIENT_HTTP_STATUSES:
            return "transient", None
        if code in PERMANENT_HTTP_STATUSES:
            return "permanent", None

    if any(tok in upper for tok in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "TIMED OUT", "CONNECTION RESET", "TEMPORARILY")):
        return "transient", None

    return "permanent", None


def _retry_delays_for(provider: str, category: str) -> tuple[float, ...]:
    if category == "rate_limit":
        return GEMINI_RATE_LIMIT_DELAYS_S if provider == "gemini" else OPENAI_RATE_LIMIT_DELAYS_S
    if category == "transient":
        return GEMINI_TRANSIENT_DELAYS_S if provider == "gemini" else OPENAI_TRANSIENT_DELAYS_S
    return ()


async def _run_with_backoff(
    provider: str,
    fn: Callable[[], Awaitable[_T]],
    *,
    retry: bool,
) -> _T:
    """Execute one batch call with provider-specific retry/backoff policy."""
    rate_limit_attempt = 0
    transient_attempt = 0
    total_attempt = 0
    while True:
        total_attempt += 1
        try:
            return await fn()
        except Exception as exc:
            if not retry:
                raise
            category, retry_after_s = _classify_failure(exc)
            delays = _retry_delays_for(provider, category)
            if not delays:
                raise
            if category == "rate_limit":
                if rate_limit_attempt >= len(delays):
                    raise
                delay = delays[rate_limit_attempt]
                if retry_after_s is not None:
                    delay = min(max(delay, retry_after_s), MAX_RETRY_AFTER_S)
                rate_limit_attempt += 1
            else:
                if transient_attempt >= len(delays):
                    raise
                delay = delays[transient_attempt]
                transient_attempt += 1
            logger.warning(
                "[dalton.embeddings] %s attempt %d failed (%s: %s); retrying in %.1fs",
                provider,
                total_attempt,
                category,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)


def _validate_batch_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
    expected_dimensions: int,
    provider: str,
) -> list[list[float]]:
    if len(vectors) != expected_count:
        raise EmbeddingResponseError(
            f"{provider} embedding returned {len(vectors)} vectors for {expected_count} texts"
        )
    out: list[list[float]] = []
    for idx, vec in enumerate(vectors):
        floats = [float(x) for x in vec]
        if len(floats) != expected_dimensions:
            raise EmbeddingResponseError(
                f"{provider} embedding vector[{idx}] has {len(floats)} dimensions, "
                f"expected {expected_dimensions}"
            )
        out.append(floats)
    return out


def _build_gemini_batch_contents(texts: Sequence[str]) -> list[Any]:
    """Construct one ``google.genai.types.Content`` per text item.

    ``gemini-embedding-2`` folds a list of plain strings into a single vector
    (see ``company_wiki_cli.py:219-225``), so every batch item MUST be its own
    ``Content(parts=[Part(text=t)])``.
    """
    from google.genai import types

    return [types.Content(parts=[types.Part(text=t)]) for t in texts]


def _create_gemini_client(api_key: str, *, timeout_s: float | None = None) -> Any:
    from google import genai
    from google.genai import types

    if timeout_s is not None:
        timeout_ms = max(1, int(round(float(timeout_s) * 1000)))
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
    return genai.Client(api_key=api_key)


def _call_gemini_embed_sync(
    batch: Sequence[str],
    *,
    api_model_name: str,
    task_type: str,
    dimensions: int,
    api_key: str,
    include_output_dimensionality: bool,
    timeout_s: float = 60.0,
) -> list[list[float]]:
    client = _create_gemini_client(api_key, timeout_s=timeout_s)
    contents = _build_gemini_batch_contents(batch)
    embed_cfg: dict[str, Any] = {"task_type": task_type}
    if include_output_dimensionality:
        embed_cfg["output_dimensionality"] = int(dimensions)

    result = client.models.embed_content(
        model=api_model_name,
        contents=contents,
        config=embed_cfg,
    )
    raw_embeddings = getattr(result, "embeddings", None)
    if not isinstance(raw_embeddings, list):
        raise EmbeddingResponseError("gemini embedding response missing embeddings list")
    vectors = [list(getattr(item, "values", ())) for item in raw_embeddings]
    return _validate_batch_vectors(
        vectors,
        expected_count=len(batch),
        expected_dimensions=dimensions,
        provider="gemini",
    )


def _extract_openai_error_reason(resp: httpx.Response) -> str:
    try:
        parsed = resp.json()
    except ValueError:
        return resp.text[:200].strip()
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:200]
        if err:
            return str(err)[:200]
    return ""


async def _call_openai_embed_async(
    batch: Sequence[str],
    *,
    api_model_name: str,
    dimensions: int,
    api_key: str,
    base_url: str,
    timeout_s: float,
) -> list[list[float]]:
    endpoint = f"{(base_url or DEFAULT_OPENAI_BASE_URL).rstrip('/')}/embeddings"
    payload: dict[str, Any] = {
        "model": api_model_name,
        "input": list(batch),
    }
    if api_model_name.startswith("text-embedding-3"):
        payload["dimensions"] = int(dimensions)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(endpoint, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise EmbeddingProviderHTTPError(
            "openai",
            resp.status_code,
            _extract_openai_error_reason(resp),
            retry_after_s=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    try:
        parsed = resp.json()
    except ValueError as exc:
        raise EmbeddingResponseError("openai embedding returned invalid JSON") from exc
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        raise EmbeddingResponseError("openai embedding response missing data list")
    ordered = sorted(
        (item for item in data if isinstance(item, dict)),
        key=lambda item: int(item.get("index", 0)),
    )
    vectors = [item.get("embedding") or [] for item in ordered]
    return _validate_batch_vectors(
        vectors,
        expected_count=len(batch),
        expected_dimensions=dimensions,
        provider="openai",
    )


async def _acquire_semaphore(
    sem: asyncio.Semaphore,
    *,
    deadline: float | None,
) -> float | None:
    """Acquire ``sem``, optionally bounding the queue wait by monotonic ``deadline``.

    Returns remaining seconds (strictly positive) when ``deadline`` is set, or
    ``None`` for unbounded queue waits (batch document embedding). If the
    deadline expires right after acquiring, releases the slot before raising
    ``TimeoutError``.
    """
    if deadline is None:
        await sem.acquire()
        return None
    wait_budget = deadline - time.monotonic()
    if wait_budget <= 0:
        raise TimeoutError("Embedding request timed out before acquiring concurrency slot")
    async with asyncio.timeout(wait_budget):
        await sem.acquire()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        sem.release()
        raise TimeoutError("Embedding request timed out while waiting for concurrency slot")
    return remaining


async def embed_texts_direct(
    texts: Sequence[str],
    *,
    task_type: str | None = None,
    config: EmbeddingConfig | None = None,
    api_key: str | None = None,
    timeout_s: float = 120.0,
    retry: bool = True,
    include_queue_in_timeout: bool = False,
) -> tuple[list[list[float]], str]:
    """Embed ``texts`` directly from the backend in batches of up to 100.

    Returns ``(embeddings, model_label)`` where ``model_label`` is the stored
    logical model identifier (e.g. ``gemini-embedding-2-preview``).
    When ``include_queue_in_timeout=True`` (used by ``embed_query_direct``),
    waiting for a concurrency semaphore slot counts against ``timeout_s``.
    """
    cfg = config if config is not None else resolve_embedding_config()
    if not texts:
        return [], cfg.model_label

    key = api_key.strip() if isinstance(api_key, str) else resolve_embedding_api_key(cfg.provider)
    if not key:
        expected_env = "GEMINI_API_KEY" if cfg.provider == "gemini" else "OPENAI_API_KEY"
        raise EmbeddingConfigError(
            f"Missing embedding API key for provider={cfg.provider!r} "
            f"(set DALTON_EMBEDDING_API_KEY or {expected_env})"
        )

    eff_task_type = validate_embedding_task_type(
        cfg.task_type_doc if task_type is None else task_type,
        provider=cfg.provider,
        source="task_type",
    )

    # For Everflow Gemini 3072-d, omit output_dimensionality to preserve exact
    # parity with company_wiki_cli.py:221-225; include it when requesting
    # reduced dimensions (e.g. Core 1536-d).
    include_output_dim = cfg.provider == "gemini" and cfg.dimensions != 3072

    sem = _get_direct_embed_semaphore()
    all_vectors: list[list[float]] = []
    text_list = [str(t) for t in texts]
    for start in range(0, len(text_list), MAX_BATCH_SIZE):
        batch = text_list[start : start + MAX_BATCH_SIZE]
        if cfg.provider == "gemini":

            async def _invoke_gemini(b: list[str] = batch) -> list[list[float]]:
                deadline = (time.monotonic() + float(timeout_s)) if include_queue_in_timeout else None
                remaining = await _acquire_semaphore(sem, deadline=deadline)
                call_timeout_s = timeout_s if remaining is None else remaining
                released = False
                fut: asyncio.Future[list[list[float]]] | None = None

                def _release_on_done(done_fut: asyncio.Future[list[list[float]]]) -> None:
                    nonlocal released
                    if not done_fut.cancelled():
                        _ = done_fut.exception()
                    if not released:
                        released = True
                        sem.release()

                try:
                    loop = asyncio.get_running_loop()
                    ctx = contextvars.copy_context()
                    fut = loop.run_in_executor(
                        None,
                        functools.partial(
                            ctx.run,
                            _call_gemini_embed_sync,
                            b,
                            api_model_name=cfg.api_model_name,
                            task_type=eff_task_type,
                            dimensions=cfg.dimensions,
                            api_key=key,
                            include_output_dimensionality=include_output_dim,
                            timeout_s=call_timeout_s,
                        ),
                    )
                    fut.add_done_callback(_release_on_done)
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=call_timeout_s)
                finally:
                    if (fut is None or fut.done()) and not released:
                        released = True
                        sem.release()

            batch_vecs = await _run_with_backoff("gemini", _invoke_gemini, retry=retry)
        elif cfg.provider == "openai":

            async def _invoke_openai(b: list[str] = batch) -> list[list[float]]:
                deadline = (time.monotonic() + float(timeout_s)) if include_queue_in_timeout else None
                remaining = await _acquire_semaphore(sem, deadline=deadline)
                call_timeout_s = timeout_s if remaining is None else remaining
                try:
                    coro = _call_openai_embed_async(
                        b,
                        api_model_name=cfg.api_model_name,
                        dimensions=cfg.dimensions,
                        api_key=key,
                        base_url=cfg.base_url,
                        timeout_s=call_timeout_s,
                    )
                    if deadline is not None:
                        return await asyncio.wait_for(coro, timeout=call_timeout_s)
                    return await coro
                finally:
                    sem.release()

            batch_vecs = await _run_with_backoff("openai", _invoke_openai, retry=retry)
        else:
            raise EmbeddingConfigError(f"Unsupported embedding provider: {cfg.provider!r}")

        all_vectors.extend(batch_vecs)

    return all_vectors, cfg.model_label


async def embed_query_direct(
    query: str,
    *,
    config: EmbeddingConfig | None = None,
    api_key: str | None = None,
    timeout_s: float = 60.0,
    retry: bool = False,
) -> list[float] | None:
    """Embed a single search query directly using ``task_type_query`` (no retry by default).

    The ``timeout_s`` budget covers both semaphore queue wait and upstream execution.
    """
    q = (query or "").strip()
    if not q:
        return None
    cfg = config if config is not None else resolve_embedding_config()
    vecs, _ = await embed_texts_direct(
        [q],
        task_type=cfg.task_type_query,
        config=cfg,
        api_key=api_key,
        timeout_s=timeout_s,
        retry=retry,
        include_queue_in_timeout=True,
    )
    return vecs[0] if vecs else None
