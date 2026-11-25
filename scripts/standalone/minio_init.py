#!/usr/bin/env python3
"""Idempotent one-shot bucket initialization for Standalone Core MinIO.

Creates the configured chat-uploads bucket if missing and clears any public
bucket policy so objects remain strictly private behind the backend API and
short-lived HMAC sandbox download relay.
"""

from __future__ import annotations

import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default


def _ensure_private_bucket(
    client: object,
    *,
    bucket: str,
    region: str,
    endpoint: str,
) -> int:
    try:
        client.head_bucket(Bucket=bucket)  # type: ignore[attr-defined]
        print(f"[minio-init] Bucket '{bucket}' already exists.", flush=True)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in ("404", "NoSuchBucket", "NotFound") or status == 404:
            create_kwargs: dict[str, object] = {"Bucket": bucket}
            if region and region != "us-east-1":
                create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            client.create_bucket(**create_kwargs)  # type: ignore[attr-defined]
            print(f"[minio-init] Created bucket '{bucket}'.", flush=True)
        else:
            print(f"[minio-init] ERROR checking bucket '{bucket}': {exc}", file=sys.stderr)
            return 1

    # Ensure no anonymous public policy is attached.
    try:
        client.delete_bucket_policy(Bucket=bucket)  # type: ignore[attr-defined]
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code != "NoSuchBucketPolicy":
            print(
                f"[minio-init] ERROR clearing bucket policy on '{bucket}': {exc}",
                file=sys.stderr,
            )
            return 1
    except Exception as exc:
        print(
            f"[minio-init] ERROR clearing bucket policy on '{bucket}': {exc}",
            file=sys.stderr,
        )
        return 1

    client.head_bucket(Bucket=bucket)  # type: ignore[attr-defined]
    print(f"[minio-init] Verified private bucket '{bucket}' at {endpoint}.", flush=True)
    return 0


def main() -> int:
    endpoint = _env(
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "STORAGE_ENDPOINT_URL",
        default="http://minio:9000",
    ).rstrip("/")
    bucket = _env(
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "STORAGE_BUCKET_NAME",
        default="chat-uploads",
    )
    access_key = _env(
        "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
        "MINIO_ROOT_USER",
        "STORAGE_ACCESS_KEY_ID",
    )
    secret_key = _env(
        "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
        "MINIO_ROOT_PASSWORD",
        "STORAGE_SECRET_ACCESS_KEY",
    )
    region = _env(
        "DALTON_CHAT_UPLOAD_STORAGE_REGION",
        "STORAGE_REGION",
        default="us-east-1",
    )
    addressing_style = _env(
        "DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE",
        "STORAGE_ADDRESSING_STYLE",
        default="path",
    ).lower()

    if not bucket:
        print("[minio-init] ERROR: bucket name is empty.", file=sys.stderr)
        return 1
    if not access_key or not secret_key:
        print(
            "[minio-init] ERROR: MinIO credentials are missing "
            "(set MINIO_ROOT_USER / MINIO_ROOT_PASSWORD or DALTON_CHAT_UPLOAD_STORAGE_*).",
            file=sys.stderr,
        )
        return 1

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )

    rc = _ensure_private_bucket(client, bucket=bucket, region=region, endpoint=endpoint)
    if rc != 0:
        return rc

    # Optional transcription bucket initialization when DALTON_ASR_STORAGE_BUCKET_NAME is set
    # against the same MinIO endpoint (keeps default Core behavior unchanged when unset).
    asr_bucket = _env("DALTON_ASR_STORAGE_BUCKET_NAME")
    if asr_bucket and asr_bucket != bucket:
        asr_endpoint = _env("DALTON_ASR_STORAGE_ENDPOINT_URL", default=endpoint).rstrip("/")
        if asr_endpoint == endpoint:
            asr_region = _env("DALTON_ASR_STORAGE_REGION", default=region)
            rc = _ensure_private_bucket(
                client, bucket=asr_bucket, region=asr_region, endpoint=asr_endpoint
            )
            if rc != 0:
                return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
