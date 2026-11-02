"""The bridge /embed client asks again after a transient failure, and only then."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import create_autospec

import pytest

import src.dalton.conversations.embedder as embedder


def _http_error(code: int, reason: str = "") -> embedder.BridgeHTTPError:
    # What _post_embed_sync raises once it has read the bridge's error body.
    return embedder.BridgeHTTPError(code, reason)


def _ok(n: int) -> dict:
    return {"ok": True, "model": "m", "embeddings": [[0.1, 0.2]] * n}


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(embedder, "RETRY_DELAYS_S", (0.0, 0.0))


@pytest.mark.asyncio
async def test_transient_502_then_success_returns_embeddings(monkeypatch):
    post = create_autospec(embedder._post_embed_sync, side_effect=[_http_error(502), _ok(2)])
    monkeypatch.setattr(embedder, "_post_embed_sync", post)
    vecs, model = await embedder.embed_texts_via_bridge(
        ["a", "b"], embed_url="http://bridge/embed", bridge_token=""
    )
    assert len(vecs) == 2 and model == "m"
    assert post.call_count == 2


@pytest.mark.asyncio
async def test_connection_error_is_retried(monkeypatch):
    post = create_autospec(
        embedder._post_embed_sync,
        side_effect=[urllib.error.URLError("reset"), ConnectionResetError(), _ok(1)],
    )
    monkeypatch.setattr(embedder, "_post_embed_sync", post)
    vecs, _ = await embedder.embed_texts_via_bridge(["a"], embed_url="http://bridge/embed", bridge_token="")
    assert len(vecs) == 1 and post.call_count == 3


@pytest.mark.asyncio
async def test_gives_up_after_three_transient_failures(monkeypatch):
    post = create_autospec(embedder._post_embed_sync, side_effect=[_http_error(503)] * 3)
    monkeypatch.setattr(embedder, "_post_embed_sync", post)
    with pytest.raises(embedder.BridgeHTTPError):
        await embedder.embed_texts_via_bridge(["a"], embed_url="http://bridge/embed", bridge_token="")
    assert post.call_count == 3


@pytest.mark.asyncio
async def test_non_transient_status_is_not_retried(monkeypatch):
    post = create_autospec(embedder._post_embed_sync, side_effect=[_http_error(401)])
    monkeypatch.setattr(embedder, "_post_embed_sync", post)
    with pytest.raises(embedder.BridgeHTTPError):
        await embedder.embed_texts_via_bridge(["a"], embed_url="http://bridge/embed", bridge_token="")
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_bridge_missing_config_502_is_not_retried_and_reason_surfaces(monkeypatch):
    post = create_autospec(
        embedder._post_embed_sync,
        side_effect=[_http_error(502, "GEMINI_API_KEY not configured in bridge env file")],
    )
    monkeypatch.setattr(embedder, "_post_embed_sync", post)
    with pytest.raises(RuntimeError, match="not configured"):
        await embedder.embed_texts_via_bridge(["a"], embed_url="http://bridge/embed", bridge_token="")
    assert post.call_count == 1


@pytest.mark.asyncio
async def test_query_path_does_not_retry(monkeypatch):
    post = create_autospec(embedder._post_embed_sync, side_effect=[urllib.error.URLError("reset")])
    monkeypatch.setattr(embedder, "_post_embed_sync", post)
    vec = await embedder.embed_query_via_bridge("q", embed_url="http://bridge/embed", bridge_token="")
    assert vec is None  # the search falls back to keyword retrieval
    assert post.call_count == 1


def test_post_embed_sync_reads_error_body_in_thread_and_closes_it(monkeypatch):
    """The real request function turns an HTTPError into BridgeHTTPError with the
    bridge's reason, reading and closing the body itself (in the worker thread)."""
    body = io.BytesIO(json.dumps({"ok": False, "error": "company_wiki_config_missing"}).encode())
    err = urllib.error.HTTPError("http://bridge/embed", 502, "Bad Gateway", {}, body)

    def fake_urlopen(req, timeout):
        raise err

    monkeypatch.setattr(embedder.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(embedder.BridgeHTTPError) as excinfo:
        embedder._post_embed_sync("http://bridge/embed", "", {"texts": ["a"]}, 1.0)
    assert excinfo.value.code == 502
    assert excinfo.value.reason == "company_wiki_config_missing"
    assert not embedder._is_transient(excinfo.value)  # permanent: not retried
    assert body.closed
