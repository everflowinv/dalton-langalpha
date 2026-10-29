"""Unit tests for the transcription-only object store (`src/dalton/transcription_storage.py`).

The module gives A1 transcription its own `DALTON_ASR_STORAGE_*` configuration
so the global `STORAGE_*` store can stay off in production. What these tests
pin:

- the configured/not-configured decision (endpoint, bucket, AK, SK all set);
- the key-prefix rule staying byte-identical to the bridge's
  `SourceObjectStore.resolve_full_key` — the backend writes the objects the
  bridge reads, so any divergence is an object the bridge cannot find;
- the seven store helpers' boto3 call shapes against `create_autospec`
  clients (part length signed into the presigned URL, the signing endpoint
  used for part URLs, pagination and return structure of `list_uploaded_parts`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import boto3
import pytest
from botocore.exceptions import ClientError

from src.dalton import transcription_storage as tstore

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import asr_jobs  # noqa: E402  (scripts/dalton, on sys.path like the other A1 tests)

_ALL_STORE_VARS = (
    "DALTON_ASR_STORAGE_ENDPOINT_URL",
    "DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL",
    "DALTON_ASR_STORAGE_BUCKET_NAME",
    "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
    "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    "DALTON_ASR_STORAGE_REGION",
    "DALTON_ASR_STORAGE_ADDRESSING_STYLE",
    "DALTON_ASR_STORAGE_KEY_PREFIX",
)


@pytest.fixture(autouse=True)
def _fresh_client_cache() -> None:
    # Clients are cached in module globals; drop them around every test so a
    # client built under one test's env can never leak into the next.
    tstore._reset_clients_for_test()
    yield
    tstore._reset_clients_for_test()


def _clear_store_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_STORE_VARS:
        monkeypatch.delenv(var, raising=False)
    # The bridge falls back to STORAGE_KEY_PREFIX when the DALTON prefix is
    # unset; the backend module deliberately does not, so keep it absent to
    # make any drift show up as a parity failure here.
    monkeypatch.delenv("STORAGE_KEY_PREFIX", raising=False)


def _configure_store(
    monkeypatch: pytest.MonkeyPatch,
    *,
    key_prefix: str | None = None,
    signing_endpoint: str | None = None,
) -> None:
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", "asr-bucket")
    monkeypatch.setenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "sk")
    if key_prefix is not None:
        monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", key_prefix)
    if signing_endpoint is not None:
        monkeypatch.setenv("DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL", signing_endpoint)


def _autospec_s3_client() -> Any:
    # A real client built for its interface only — boto3 makes no request at
    # construction — so the double carries every operation's true signature.
    spec = boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )
    return create_autospec(spec)


# ---------------------------------------------------------------------------
# 1. `is_transcription_storage_configured`
# ---------------------------------------------------------------------------


def test_configured_when_endpoint_bucket_and_credentials_all_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch)
    assert tstore.is_transcription_storage_configured() is True


@pytest.mark.parametrize(
    "var", ["DALTON_ASR_STORAGE_ENDPOINT_URL", "DALTON_ASR_STORAGE_BUCKET_NAME",
            "DALTON_ASR_STORAGE_ACCESS_KEY_ID", "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY"],
)
def test_not_configured_when_any_required_var_missing(
    monkeypatch: pytest.MonkeyPatch, var: str
) -> None:
    _configure_store(monkeypatch)
    monkeypatch.setenv(var, "   ")
    assert tstore.is_transcription_storage_configured() is False


def test_signing_endpoint_and_prefix_do_not_make_an_unconfigured_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL", "http://h:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", "asr-audio")
    assert tstore.is_transcription_storage_configured() is False


def test_signing_endpoint_falls_back_to_plain_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch)
    assert tstore.signing_endpoint_url() == "http://minio:9000"
    monkeypatch.setenv(
        "DALTON_ASR_STORAGE_SIGNING_ENDPOINT_URL", "http://host.docker.internal:9000/"
    )
    assert tstore.signing_endpoint_url() == "http://host.docker.internal:9000"


# ---------------------------------------------------------------------------
# 2. Key-prefix parity with the bridge (`asr_jobs.SourceObjectStore`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key_prefix", "object_key"),
    [
        ("", "transcriptions/ws-1/job-1/source/call.m4a"),
        ("asr-audio", "transcriptions/ws-1/job-1/source/call.m4a"),
        ("asr-audio/", "transcriptions/ws-1/job-1/source/call.m4a"),
        ("a/b", "transcriptions/call.m4a"),
        ("/asr-audio", "/transcriptions/call.m4a"),
        ("asr-audio", "asr-audio/transcriptions/call.m4a"),
        ("asr-audio", " /transcriptions/call.m4a "),
        ("asr-audio", ""),
    ],
)
def test_resolve_full_key_matches_bridge_source_object_store(
    monkeypatch: pytest.MonkeyPatch, key_prefix: str, object_key: str
) -> None:
    """Backend and bridge must derive the exact same stored key for every input.

    The bridge's `SourceObjectStore` downloads what the backend uploads; the
    two `resolve_full_key` implementations share the env var but not the code,
    so this is the one test that catches a rule change on either side.
    """
    _clear_store_env(monkeypatch)
    monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", key_prefix)

    store = asr_jobs.SourceObjectStore(environ=os.environ)

    assert tstore.resolve_full_key(object_key) == store.resolve_full_key(object_key)


# ---------------------------------------------------------------------------
# 3. The seven store helpers against `create_autospec` boto3 clients
# ---------------------------------------------------------------------------


def test_open_multipart_upload_prefixes_key_and_returns_upload_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch, key_prefix="asr-audio")
    client = _autospec_s3_client()
    client.create_multipart_upload.return_value = {"UploadId": "mp-001"}
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    upload_id = tstore.open_multipart_upload(
        "transcriptions/ws-1/call.m4a", content_type="audio/mp4"
    )

    assert upload_id == "mp-001"
    client.create_multipart_upload.assert_called_once_with(
        Bucket="asr-bucket",
        Key="asr-audio/transcriptions/ws-1/call.m4a",
        ContentType="audio/mp4",
    )


def test_open_multipart_upload_returns_none_when_store_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch)
    client = _autospec_s3_client()
    client.create_multipart_upload.side_effect = RuntimeError("boom")
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    assert tstore.open_multipart_upload("k", content_type="audio/mp4") is None


def test_sign_upload_part_signs_length_and_key_via_signing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch, key_prefix="asr-audio")
    signing_client = _autospec_s3_client()
    signing_client.generate_presigned_url.return_value = "https://signed/part"
    monkeypatch.setattr(tstore, "_get_signing_client", lambda: signing_client)

    url = tstore.sign_upload_part(
        "transcriptions/ws-1/call.m4a",
        "mp-001",
        part_number=2,
        size=16 * 1024 * 1024,
        expires_in=900,
    )

    assert url == "https://signed/part"
    signing_client.generate_presigned_url.assert_called_once_with(
        "upload_part",
        Params={
            "Bucket": "asr-bucket",
            "Key": "asr-audio/transcriptions/ws-1/call.m4a",
            "UploadId": "mp-001",
            "PartNumber": 2,
            # The part's byte length is signed into the URL: the holder writes
            # exactly this many bytes or nothing.
            "ContentLength": 16 * 1024 * 1024,
        },
        ExpiresIn=900,
    )


def test_sign_upload_part_signs_for_signing_endpoint_not_plain_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Part URLs are signed against the SIGNING address even when it differs.

    The URL's consumer is the sandbox, which reaches MinIO at
    `host.docker.internal`, not at the backend container's `minio` service.
    """
    _configure_store(
        monkeypatch, signing_endpoint="http://host.docker.internal:9000"
    )
    built: dict[str, Any] = {}

    def _record_build(endpoint_url: str) -> Any:
        built[endpoint_url] = built.get(endpoint_url, 0) + 1
        return _autospec_s3_client()

    monkeypatch.setattr(
        tstore,
        "_build_client",
        create_autospec(tstore._build_client, side_effect=_record_build),
    )

    assert tstore.sign_upload_part("k", "mp-001", part_number=1, size=5)
    # The plain-endpoint client (open/list/complete/abort/head/delete) is a
    # separate client built from the plain endpoint.
    assert tstore.open_multipart_upload("k", content_type="audio/mp4") is not None

    assert built == {
        "http://host.docker.internal:9000": 1,
        "http://minio:9000": 1,
    }


def test_list_uploaded_parts_paginates_sorts_and_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch, key_prefix="asr-audio")
    client = _autospec_s3_client()
    client.list_parts.side_effect = [
        {
            "IsTruncated": True,
            "NextPartNumberMarker": 1,
            "Parts": [{"PartNumber": 2, "Size": 10, "ETag": '"e2"'}],
        },
        {
            "IsTruncated": False,
            "Parts": [{"PartNumber": 1, "Size": 20, "ETag": '"e1"'}],
        },
    ]
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    parts = tstore.list_uploaded_parts("k", "mp-001")

    assert parts == [
        {"part_number": 1, "size": 20, "etag": '"e1"'},
        {"part_number": 2, "size": 10, "etag": '"e2"'},
    ]
    assert client.list_parts.call_args_list[0].kwargs["PartNumberMarker"] == 0
    assert client.list_parts.call_args_list[1].kwargs["PartNumberMarker"] == 1
    assert client.list_parts.call_args_list[1].kwargs["Key"] == "asr-audio/k"


def test_list_uploaded_parts_returns_none_when_store_cannot_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch)
    client = _autospec_s3_client()
    client.list_parts.side_effect = ClientError(
        {"Error": {"Code": "NoSuchUpload"}}, "ListParts"
    )
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    assert tstore.list_uploaded_parts("k", "mp-001") is None


def test_complete_multipart_upload_sorts_parts_by_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch, key_prefix="asr-audio")
    client = _autospec_s3_client()
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    ok = tstore.complete_multipart_upload(
        "k", "mp-001", [(2, '"e2"'), (1, '"e1"')]
    )

    assert ok is True
    client.complete_multipart_upload.assert_called_once_with(
        Bucket="asr-bucket",
        Key="asr-audio/k",
        UploadId="mp-001",
        MultipartUpload={
            "Parts": [
                {"PartNumber": 1, "ETag": '"e1"'},
                {"PartNumber": 2, "ETag": '"e2"'},
            ]
        },
    )


def test_complete_multipart_upload_returns_false_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch)
    client = _autospec_s3_client()
    client.complete_multipart_upload.side_effect = RuntimeError("boom")
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    assert tstore.complete_multipart_upload("k", "mp-001", [(1, '"e1"')]) is False


def test_abort_multipart_upload_success_and_missing_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch)
    client = _autospec_s3_client()
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    assert tstore.abort_multipart_upload("k", "mp-001") is True
    client.abort_multipart_upload.assert_called_once_with(
        Bucket="asr-bucket", Key="k", UploadId="mp-001"
    )

    # An upload the store no longer knows counts as aborted: it holds no parts.
    client.abort_multipart_upload.side_effect = ClientError(
        {"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload"
    )
    assert tstore.abort_multipart_upload("k", "mp-001") is True

    client.abort_multipart_upload.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "AbortMultipartUpload"
    )
    assert tstore.abort_multipart_upload("k", "mp-001") is False


def test_object_size_reads_content_length_and_hides_missing_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch, key_prefix="asr-audio")
    client = _autospec_s3_client()
    client.head_object.return_value = {"ContentLength": 123456}
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    assert tstore.object_size("k") == 123456
    client.head_object.assert_called_once_with(
        Bucket="asr-bucket", Key="asr-audio/k"
    )

    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )
    assert tstore.object_size("k") is None


def test_delete_object_reports_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_store(monkeypatch, key_prefix="asr-audio")
    client = _autospec_s3_client()
    monkeypatch.setattr(tstore, "_get_client", lambda: client)

    assert tstore.delete_object("k") is True
    client.delete_object.assert_called_once_with(
        Bucket="asr-bucket", Key="asr-audio/k"
    )

    client.delete_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "DeleteObject"
    )
    assert tstore.delete_object("k") is False
