"""Offline unit tests for ``tests/fakes/header_echo.py``."""

from __future__ import annotations

from tests.fakes.header_echo import build_echo_payload


def test_build_echo_payload_lowercases_and_preserves_raw_headers() -> None:
    payload = build_echo_payload(
        "get",
        "/__probe/headers?foo=bar",
        [
            ("Host", "127.0.0.1:8088"),
            ("X-Probe-Sentinel", "keep-me"),
            ("X-Multi", "a"),
            ("x-multi", "b"),
        ],
    )
    assert payload["method"] == "GET"
    assert payload["path"] == "/__probe/headers?foo=bar"
    assert payload["headers"]["host"] == "127.0.0.1:8088"
    assert payload["headers"]["x-probe-sentinel"] == "keep-me"
    assert payload["headers"]["x-multi"] == "a, b"
    assert "x-service-token" not in payload["headers"]
    assert "x-user-id" not in payload["headers"]
    assert len(payload["raw_headers"]) == 4
