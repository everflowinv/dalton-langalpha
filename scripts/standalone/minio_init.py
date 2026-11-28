#!/usr/bin/env python3
"""Idempotent one-shot bucket initialization for Standalone Core MinIO.

Creates the configured chat-uploads bucket if missing and clears any public
bucket policy so objects remain strictly private behind the backend API and
short-lived HMAC sandbox download relay.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from src.dalton.storage_bucket_rules import (
        BucketRuleVerificationError,
        ROLE_MAIN,
        ROLE_SECONDARY,
        RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
        build_bucket_policy,
        merge_lifecycle_rules,
        normalize_key_prefix,
        verify_bucket_policy,
        verify_lifecycle_rules,
    )
except ImportError:
    from dalton.storage_bucket_rules import (  # type: ignore[no-redef]
        BucketRuleVerificationError,
        ROLE_MAIN,
        ROLE_SECONDARY,
        RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
        build_bucket_policy,
        merge_lifecycle_rules,
        normalize_key_prefix,
        verify_bucket_policy,
        verify_lifecycle_rules,
    )


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


def _ensure_main_avatars_policy(
    client: object,
    *,
    bucket: str,
    key_prefix: str,
    endpoint: str,
) -> int:
    """Grant anonymous s3:GetObject ONLY on current versions of ``<prefix>avatars/*``."""
    prefix = normalize_key_prefix(key_prefix)
    policy_doc = build_bucket_policy(bucket=bucket, role=ROLE_MAIN, key_prefix=prefix)
    assert policy_doc is not None
    try:
        client.put_bucket_policy(  # type: ignore[attr-defined]
            Bucket=bucket,
            Policy=json.dumps(policy_doc, separators=(",", ":")),
        )
    except Exception as exc:
        print(
            f"[minio-init] ERROR setting avatars read-only policy on '{bucket}': {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        resp = client.get_bucket_policy(Bucket=bucket)  # type: ignore[attr-defined]
    except Exception as exc:
        print(
            f"[minio-init] ERROR verifying avatars read-only policy on '{bucket}': {exc}",
            file=sys.stderr,
        )
        return 1

    if isinstance(resp, dict):
        raw = resp.get("Policy")
        actual = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, dict) else None)
        try:
            verify_bucket_policy(actual, bucket=bucket, role=ROLE_MAIN, key_prefix=prefix)
        except BucketRuleVerificationError as exc:
            print(
                f"[minio-init] ERROR avatars policy verification failed on '{bucket}': {exc}",
                file=sys.stderr,
            )
            return 1

    print(
        f"[minio-init] Verified avatars read-only policy on main bucket '{bucket}' "
        f"(prefix='{prefix}') at {endpoint}.",
        flush=True,
    )
    return 0


def _ensure_secondary_lifecycle(
    client: object,
    *,
    bucket: str,
    key_prefix: str,
    endpoint: str,
) -> int:
    """Apply and read-back verify managed secondary lifecycle rules on ``bucket``."""
    prefix = normalize_key_prefix(key_prefix)
    try:
        resp = client.get_bucket_lifecycle_configuration(Bucket=bucket)  # type: ignore[attr-defined]
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("NoSuchLifecycleConfiguration", "404"):
            resp = {}
        else:
            print(
                f"[minio-init] ERROR reading lifecycle rules on '{bucket}': {exc}",
                file=sys.stderr,
            )
            return 1
    except Exception as exc:
        print(
            f"[minio-init] ERROR reading lifecycle rules on '{bucket}': {exc}",
            file=sys.stderr,
        )
        return 1

    existing_rules = (
        list(resp.get("Rules"))
        if isinstance(resp, dict) and isinstance(resp.get("Rules"), list)
        else []
    )
    merged_rules, _preserved, _removed = merge_lifecycle_rules(
        existing_rules,
        role=ROLE_SECONDARY,
        key_prefix=prefix,
    )
    try:
        client.put_bucket_lifecycle_configuration(  # type: ignore[attr-defined]
            Bucket=bucket,
            LifecycleConfiguration={"Rules": merged_rules},
        )
    except Exception as exc:
        print(
            f"[minio-init] ERROR setting lifecycle rules on '{bucket}': {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        verify_resp = client.get_bucket_lifecycle_configuration(Bucket=bucket)  # type: ignore[attr-defined]
    except Exception as exc:
        print(
            f"[minio-init] ERROR verifying lifecycle rules on '{bucket}': {exc}",
            file=sys.stderr,
        )
        return 1

    if isinstance(verify_resp, dict):
        actual_rules = (
            list(verify_resp.get("Rules"))
            if isinstance(verify_resp.get("Rules"), list)
            else []
        )
        try:
            verify_lifecycle_rules(
                actual_rules,
                role=ROLE_SECONDARY,
                key_prefix=prefix,
            )
        except BucketRuleVerificationError as exc:
            print(
                f"[minio-init] ERROR lifecycle verification failed on '{bucket}': {exc}",
                file=sys.stderr,
            )
            return 1

    print(
        f"[minio-init] Verified secondary lifecycle rules (including '{RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE}') "
        f"on bucket '{bucket}' (prefix='{prefix}') at {endpoint}.",
        flush=True,
    )
    return 0


def _resolve_main_storage_bucket() -> str:
    if "STORAGE_BUCKET_NAME" in os.environ:
        return (os.environ.get("STORAGE_BUCKET_NAME") or "").strip()
    return (os.environ.get("S3_BUCKET_NAME") or "").strip()


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

    initialized: set[tuple[str, str]] = set()

    rc = _ensure_private_bucket(client, bucket=bucket, region=region, endpoint=endpoint)
    if rc != 0:
        return rc
    initialized.add((endpoint, bucket))

    # Optional general main-storage bucket initialization when STORAGE_BUCKET_NAME
    # (or S3_BUCKET_NAME) is set on the same MinIO endpoint. Keep all prefixes private
    # except current-version anonymous s3:GetObject on <STORAGE_KEY_PREFIX>avatars/*.
    storage_provider = (os.getenv("STORAGE_PROVIDER") or "").strip().lower()
    storage_bucket = _resolve_main_storage_bucket() if storage_provider != "none" else ""
    main_bucket_on_endpoint = ""
    if storage_bucket:
        storage_endpoint = _env(
            "STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL", default=endpoint
        ).rstrip("/")
        if storage_endpoint == endpoint:
            main_bucket_on_endpoint = storage_bucket
            if (storage_endpoint, storage_bucket) not in initialized:
                storage_region = _env("STORAGE_REGION", "S3_REGION", default=region)
                rc = _ensure_private_bucket(
                    client,
                    bucket=storage_bucket,
                    region=storage_region,
                    endpoint=storage_endpoint,
                )
                if rc != 0:
                    return rc
                initialized.add((storage_endpoint, storage_bucket))
            rc = _ensure_main_avatars_policy(
                client,
                bucket=storage_bucket,
                key_prefix=os.getenv("STORAGE_KEY_PREFIX", ""),
                endpoint=storage_endpoint,
            )
            if rc != 0:
                return rc

    # Optional transcription bucket initialization when DALTON_ASR_STORAGE_BUCKET_NAME is set
    # against the same MinIO endpoint (keeps default Core behavior unchanged when unset).
    asr_bucket = _env("DALTON_ASR_STORAGE_BUCKET_NAME")
    if asr_bucket:
        asr_endpoint = _env("DALTON_ASR_STORAGE_ENDPOINT_URL", default=endpoint).rstrip("/")
        if asr_endpoint == endpoint:
            if (asr_endpoint, asr_bucket) not in initialized:
                asr_region = _env("DALTON_ASR_STORAGE_REGION", default=region)
                rc = _ensure_private_bucket(
                    client, bucket=asr_bucket, region=asr_region, endpoint=asr_endpoint
                )
                if rc != 0:
                    return rc
                initialized.add((asr_endpoint, asr_bucket))
            if asr_bucket != main_bucket_on_endpoint:
                rc = _ensure_secondary_lifecycle(
                    client,
                    bucket=asr_bucket,
                    key_prefix=os.getenv("DALTON_ASR_STORAGE_KEY_PREFIX", ""),
                    endpoint=asr_endpoint,
                )
                if rc != 0:
                    return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
