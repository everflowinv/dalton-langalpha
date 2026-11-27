"""Contracts for the content-bound presigned PUT and the transfer-mode switch.

The presign binds digest, length and type into the signature; that binding is
what makes a sandbox-held URL safe to use against a shared content-addressed
prefix. Verified live against an S3-compatible store (a mismatched body is
rejected with BadDigest) before these were written; they pin the parameters.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import create_autospec, patch

import src.utils.storage as storage
from src.dalton import share_media  # DALTON-HOOK(share-redaction)
from src.utils.storage import s3_compatible

SHA = "a" * 64


class _PresignS3ClientSpec:
    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, Any] | None = None,
        ExpiresIn: int = 3600,
    ) -> str:
        raise NotImplementedError

    def get_bucket_ownership_controls(self, *, Bucket: str) -> dict[str, Any]:
        raise NotImplementedError


def test_presign_binds_digest_length_and_type(monkeypatch):
    monkeypatch.delenv("DALTON_STORAGE_DISABLE_OBJECT_ACL", raising=False)
    monkeypatch.delenv("STORAGE_DISABLE_OBJECT_ACL", raising=False)
    monkeypatch.setenv("DALTON_STORAGE_OBJECT_ACL", "auto")
    monkeypatch.setattr(
        s3_compatible.StorageConfig, "ENDPOINT_URL", "https://s3.us-east-1.amazonaws.com"
    )
    monkeypatch.setattr(s3_compatible.StorageConfig, "BUCKET_NAME", "test-bucket")
    share_media.reset_bucket_acl_cache_for_test()

    client = create_autospec(_PresignS3ClientSpec, instance=True)
    client.get_bucket_ownership_controls.return_value = {
        "OwnershipControls": {"Rules": [{"ObjectOwnership": "ObjectWriter"}]}
    }
    client.generate_presigned_url.return_value = "https://store/blobs/x?sig"
    try:
        with patch.object(s3_compatible, "_get_client", return_value=client):
            out = s3_compatible.get_signed_upload_url(
                f"blobs/{SHA}",
                sha256_hex=SHA,
                content_length=12,
                content_type="application/octet-stream",
                expires_in=600,
            )
    finally:
        share_media.reset_bucket_acl_cache_for_test()
    assert out is not None
    url, headers = out
    assert url == "https://store/blobs/x?sig"
    kwargs = client.generate_presigned_url.call_args.kwargs
    assert client.generate_presigned_url.call_args.args == ("put_object",)
    params = kwargs["Params"]
    expected_b64 = base64.b64encode(bytes.fromhex(SHA)).decode()
    assert params["Key"] == f"blobs/{SHA}"
    assert params["ContentLength"] == 12
    assert params["ContentType"] == "application/octet-stream"
    assert params["ChecksumSHA256"] == expected_b64
    # DALTON-HOOK(share-redaction): presigned single PUT binds private ACL for non-avatar keys
    assert params["ACL"] == "private"
    assert kwargs["ExpiresIn"] == 600
    # The uploader must send exactly what was signed.
    assert headers == {
        "Content-Type": "application/octet-stream",
        "x-amz-checksum-sha256": expected_b64,
        "x-amz-acl": "private",
    }


def test_presign_failure_returns_none_not_raise():
    with patch.object(s3_compatible, "_get_client", side_effect=RuntimeError("no creds")):
        assert s3_compatible.get_signed_upload_url("k", sha256_hex=SHA, content_length=1, content_type="x") is None


def test_transfer_mode_auto_is_direct_only_for_daytona_on_s3(monkeypatch):
    monkeypatch.setattr(storage, "BLOB_TRANSFER_MODE", "auto")
    monkeypatch.setattr(storage, "STORAGE_PROVIDER", "s3")
    assert storage.get_blob_transfer_mode("daytona") == "direct"
    assert storage.get_blob_transfer_mode("e2b") == "direct"
    assert storage.get_blob_transfer_mode("docker") == "relay"
    assert storage.get_blob_transfer_mode(None) == "relay"


def test_transfer_mode_relay_for_stores_that_cannot_bind_content(monkeypatch):
    # Even when asked for direct: every push there falls back to relay.
    for mode in ("auto", "direct"):
        monkeypatch.setattr(storage, "BLOB_TRANSFER_MODE", mode)
        for provider in ("oss", "none"):
            monkeypatch.setattr(storage, "STORAGE_PROVIDER", provider)
            assert storage.get_blob_transfer_mode("daytona") == "relay"


def test_transfer_mode_explicit_overrides_auto(monkeypatch):
    monkeypatch.setattr(storage, "STORAGE_PROVIDER", "s3")
    monkeypatch.setattr(storage, "BLOB_TRANSFER_MODE", "relay")
    assert storage.get_blob_transfer_mode("daytona") == "relay"
    monkeypatch.setattr(storage, "BLOB_TRANSFER_MODE", "direct")
    assert storage.get_blob_transfer_mode("docker") == "direct"
    monkeypatch.setattr(storage, "BLOB_TRANSFER_MODE", "bogus")
    assert storage.get_blob_transfer_mode("daytona") == "direct"
