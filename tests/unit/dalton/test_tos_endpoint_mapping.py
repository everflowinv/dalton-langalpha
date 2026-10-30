"""Unit tests for the native → S3-compatible TOS endpoint mapping (task BL).

Production TOS credentials were copied from OpenClaw, which dials the *native*
TOS endpoint (`tos-cn-beijing.volces.com`, the Volcengine `tos` SDK endpoint).
The bridge uploads through boto3, which speaks the S3 protocol — served only at
`tos-s3-<region>.volces.com` / `tos-s3-<region>.ivolces.com` (native endpoints
answer 400). These tests pin `_normalize_tos_endpoint`'s mapping matrix, the
S3-compatible default, and the one-time INFO log emitted at first client build
when a native endpoint is remapped.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import seed_asr_client as sac  # noqa: E402


def _test_credentials(endpoint: str) -> sac.SeedAsrCredentials:
    return sac.SeedAsrCredentials(
        app_id="test_seed_app_id_secret_999",
        access_key="test_seed_access_key_secret_888",
        tos_ak="AKLTTosAccessKeySecret777",
        tos_sk="SKLTTosSecretKeyValue666",
        tos_bucket="dalton-asr-test-bucket",
        tos_region="cn-beijing",
        tos_endpoint=endpoint,
    )


@pytest.mark.parametrize(
    ("endpoint", "expected_url", "expected_remap"),
    [
        # Native public endpoints (any region) gain https and the s3 host.
        (
            "tos-cn-beijing.volces.com",
            "https://tos-s3-cn-beijing.volces.com",
            ("tos-cn-beijing.volces.com", "tos-s3-cn-beijing.volces.com"),
        ),
        (
            "tos-ap-southeast-1.volces.com",
            "https://tos-s3-ap-southeast-1.volces.com",
            ("tos-ap-southeast-1.volces.com", "tos-s3-ap-southeast-1.volces.com"),
        ),
        # Native internal endpoints map on the ivolces.com suffix too.
        (
            "tos-cn-beijing.ivolces.com",
            "https://tos-s3-cn-beijing.ivolces.com",
            ("tos-cn-beijing.ivolces.com", "tos-s3-cn-beijing.ivolces.com"),
        ),
        # Host detection is case-insensitive; the rebuilt host is lowercase.
        (
            "TOS-CN-BEIJING.VOLCES.COM",
            "https://tos-s3-cn-beijing.volces.com",
            ("tos-cn-beijing.volces.com", "tos-s3-cn-beijing.volces.com"),
        ),
        # Scheme and port survive the mapping.
        (
            "https://tos-cn-beijing.volces.com",
            "https://tos-s3-cn-beijing.volces.com",
            ("tos-cn-beijing.volces.com", "tos-s3-cn-beijing.volces.com"),
        ),
        (
            "http://tos-cn-beijing.ivolces.com",
            "http://tos-s3-cn-beijing.ivolces.com",
            ("tos-cn-beijing.ivolces.com", "tos-s3-cn-beijing.ivolces.com"),
        ),
        (
            "tos-cn-beijing.volces.com:8443",
            "https://tos-s3-cn-beijing.volces.com:8443",
            ("tos-cn-beijing.volces.com", "tos-s3-cn-beijing.volces.com"),
        ),
    ],
)
def test_native_tos_endpoint_remapped_to_s3_compatible(
    endpoint: str, expected_url: str, expected_remap: tuple[str, str]
) -> None:
    """Native `tos-<region>.volces.com` / `tos-<region>.ivolces.com` hosts are remapped to `tos-s3-…`, keeping scheme, port, and virtual addressing."""
    url, style, remap = sac._normalize_tos_endpoint(endpoint)
    assert url == expected_url
    assert style == "virtual"
    assert remap == expected_remap


@pytest.mark.parametrize(
    ("endpoint", "expected_url", "expected_style"),
    [
        # Already S3-compatible — with or without scheme, port, either suffix.
        (
            "tos-s3-cn-beijing.volces.com",
            "https://tos-s3-cn-beijing.volces.com",
            "virtual",
        ),
        (
            "https://tos-s3-cn-beijing.volces.com",
            "https://tos-s3-cn-beijing.volces.com",
            "virtual",
        ),
        (
            "tos-s3-cn-beijing.volces.com:8443",
            "https://tos-s3-cn-beijing.volces.com:8443",
            "virtual",
        ),
        (
            "tos-s3-ap-southeast-1.ivolces.com",
            "https://tos-s3-ap-southeast-1.ivolces.com",
            "virtual",
        ),
        # Custom domains — including ones that merely look TOS-ish.
        ("tos.example.com", "https://tos.example.com", "virtual"),
        (
            "tos-beijing.example.com",
            "https://tos-beijing.example.com",
            "virtual",
        ),
        ("custom.volces.com", "https://custom.volces.com", "virtual"),
        (
            "https://my-tos.cdn.example.com",
            "https://my-tos.cdn.example.com",
            "virtual",
        ),
        ("my-minio.internal:9000", "https://my-minio.internal:9000", "virtual"),
        # Localhost / IPs keep path addressing.
        ("localhost", "https://localhost", "path"),
        ("http://127.0.0.1:9000", "http://127.0.0.1:9000", "path"),
        ("192.168.1.5", "https://192.168.1.5", "path"),
    ],
)
def test_non_native_endpoints_pass_through_unchanged(
    endpoint: str, expected_url: str, expected_style: str
) -> None:
    """`tos-s3-…` hosts, custom domains, localhost and IPs are never remapped."""
    url, style, remap = sac._normalize_tos_endpoint(endpoint)
    assert url == expected_url
    assert style == expected_style
    assert remap is None


def test_unconfigured_endpoint_defaults_to_s3_compatible() -> None:
    """A missing / empty `VOLC_TOS_ENDPOINT` yields the S3-compatible default everywhere it is resolved."""
    assert sac.DEFAULT_TOS_ENDPOINT == "tos-s3-cn-beijing.volces.com"
    url, style, remap = sac._normalize_tos_endpoint("")
    assert (url, style, remap) == (
        "https://tos-s3-cn-beijing.volces.com",
        "virtual",
        None,
    )
    # Dataclass default.
    creds = sac.SeedAsrCredentials(
        app_id="app",
        access_key="ak",
        tos_ak="tak",
        tos_sk="tsk",
        tos_bucket="bucket",
    )
    assert creds.tos_endpoint == "tos-s3-cn-beijing.volces.com"
    # from_env fallback for unset, empty, and whitespace-only values.
    base_env: dict[str, Any] = {
        "SEED_ASR_APP_ID": "app",
        "SEED_ASR_ACCESS_KEY": "ak",
        "VOLC_TOS_AK": "tak",
        "VOLC_TOS_SK": "tsk",
        "VOLC_TOS_BUCKET": "bucket",
    }
    for raw in (None, "", "   "):
        env = dict(base_env)
        if raw is None:
            env.pop("VOLC_TOS_ENDPOINT", None)
        else:
            env["VOLC_TOS_ENDPOINT"] = raw
        assert sac.SeedAsrCredentials.from_env(env).tos_endpoint == (
            "tos-s3-cn-beijing.volces.com"
        )


def test_native_endpoint_build_logs_remap_info_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First client build on a native endpoint logs one INFO naming both hosts — never credentials — and the cached second build logs nothing new."""
    client = sac.VolcTosClient(_test_credentials("tos-cn-beijing.volces.com"))
    with caplog.at_level(logging.INFO, logger="researchdata-bridge"):
        built = client._get_s3_client()
        assert built is not None
        # Second call returns the cached client; no additional remap record.
        assert client._get_s3_client() is built
    remap_records = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "native TOS endpoint" in r.getMessage()
    ]
    assert len(remap_records) == 1
    message = remap_records[0].getMessage()
    assert "tos-cn-beijing.volces.com" in message
    assert "tos-s3-cn-beijing.volces.com" in message
    for record in caplog.records:
        assert "AKLTTosAccessKeySecret777" not in record.getMessage()
        assert "SKLTTosSecretKeyValue666" not in record.getMessage()


def test_s3_compatible_endpoint_build_has_no_remap_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An already S3-compatible endpoint builds a client with no remap INFO record."""
    with caplog.at_level(logging.INFO, logger="researchdata-bridge"):
        built = sac.VolcTosClient(
            _test_credentials("tos-s3-cn-beijing.volces.com")
        )._get_s3_client()
        assert built is not None
    assert not [
        r for r in caplog.records if "native TOS endpoint" in r.getMessage()
    ]
