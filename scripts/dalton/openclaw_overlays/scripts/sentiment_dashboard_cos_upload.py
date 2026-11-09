#!/usr/bin/env python3
"""Upload the generated sentiment dashboard to Tencent COS.

Supports DALTON_COS_UPLOAD_MODE:
- 'skip': validate local HTML and emit sha256/bytes without importing the COS SDK,
  reading credentials, calling macOS Keychain, or making network requests.
- 'prod': compress, upload to Tencent COS, and verify the public URL sha256.
  Reads TENCENT_COS_SECRET_ID and TENCENT_COS_SECRET_KEY as a pair from the
  environment first, falling back to macOS Keychain only on a non-Dalton host
  when both environment variables are unset.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import TYPE_CHECKING
import urllib.request

if TYPE_CHECKING:
    from qcloud_cos import CosS3Client


WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_HTML = WORKSPACE / "research-outputs" / "sentiment-dashboard" / "index.html"
BUCKET = "everflow-1320643462"
REGION = "ap-hongkong"
KEY = "index.html"
ORIGIN_WEBSITE_URL = f"https://{BUCKET}.cos-website.{REGION}.myqcloud.com/"
PUBLIC_URL = "https://eve.lumos.space/"
KEYCHAIN_ACCOUNT = "everflow"
KEYCHAIN_SECRET_ID = "com.openclaw.tencent-cos.sentiment-dashboard.secret-id"
KEYCHAIN_SECRET_KEY = "com.openclaw.tencent-cos.sentiment-dashboard.secret-key"

ENV_UPLOAD_MODE = "DALTON_COS_UPLOAD_MODE"
ENV_COS_SECRET_ID = "TENCENT_COS_SECRET_ID"
ENV_COS_SECRET_KEY = "TENCENT_COS_SECRET_KEY"
DALTON_RUNNER_ENV_VARS = (
    "DALTON_RUN_ID",
    "DALTON_JOB_KEY",
    "DALTON_NO_DIRECT_SEND",
    "DALTON_NOTIFY_DRY_RUN",
    "DALTON_OUTBOX_FILE",
    "DALTON_PYTHON_BIN",
)


def is_dalton_runner_env() -> bool:
    """Return True when executing inside the Dalton data-task runner environment."""
    return any(bool(os.environ.get(name, "").strip()) for name in DALTON_RUNNER_ENV_VARS)


def resolve_upload_mode() -> str:
    """Resolve and validate the COS upload mode ('skip' or 'prod').

    Rules:
    - Explicit 'skip' or 'prod' in DALTON_COS_UPLOAD_MODE is used directly.
    - Any other non-empty DALTON_COS_UPLOAD_MODE value is rejected.
    - When DALTON_COS_UPLOAD_MODE is unset or empty:
      * In the Dalton runner environment (or when the variable is defined as empty),
        raise ValueError so a task container never uploads by accident.
      * On a standalone non-Dalton host with DALTON_COS_UPLOAD_MODE unset,
        default to 'prod' to preserve host upload behavior.
    """
    raw = os.environ.get(ENV_UPLOAD_MODE)
    if raw is not None and raw.strip():
        mode = raw.strip().lower()
        if mode in ("skip", "prod"):
            return mode
        raise ValueError(
            f"Unsupported {ENV_UPLOAD_MODE}={raw.strip()!r}; expected 'skip' or 'prod'."
        )
    if raw is not None or is_dalton_runner_env():
        raise ValueError(
            f"{ENV_UPLOAD_MODE} must be explicitly set to 'skip' or 'prod' in the Dalton runner environment."
        )
    return "prod"


def keychain(service: str) -> str:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"Missing macOS Keychain credential {service}. Install the rotated COS uploader key first."
        ) from exc
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Empty macOS Keychain credential {service}.")
    return value


def resolve_credentials() -> tuple[str, str]:
    """Resolve (secret_id, secret_key) for Tencent COS upload.

    Reads TENCENT_COS_SECRET_ID and TENCENT_COS_SECRET_KEY as a pair from the
    environment first. Half-configured environment credentials are always rejected
    without falling back to Keychain. When both environment variables are absent,
    only a non-Dalton host falls back to macOS Keychain; the Dalton runner
    environment raises an explicit missing-credentials error.
    """
    has_id_var = ENV_COS_SECRET_ID in os.environ
    has_key_var = ENV_COS_SECRET_KEY in os.environ
    secret_id = os.environ.get(ENV_COS_SECRET_ID, "").strip()
    secret_key = os.environ.get(ENV_COS_SECRET_KEY, "").strip()

    if secret_id and secret_key:
        return secret_id, secret_key

    if secret_id or secret_key or has_id_var or has_key_var:
        missing = [
            name
            for name, val in (
                (ENV_COS_SECRET_ID, secret_id),
                (ENV_COS_SECRET_KEY, secret_key),
            )
            if not val
        ]
        raise RuntimeError(
            f"Incomplete Tencent COS environment credentials: missing {', '.join(missing)}. "
            f"Both {ENV_COS_SECRET_ID} and {ENV_COS_SECRET_KEY} must be set together."
        )

    if is_dalton_runner_env():
        raise RuntimeError(
            f"Missing Tencent COS credentials in Dalton runner environment: "
            f"set both {ENV_COS_SECRET_ID} and {ENV_COS_SECRET_KEY}."
        )

    return keychain(KEYCHAIN_SECRET_ID), keychain(KEYCHAIN_SECRET_KEY)


def client() -> CosS3Client:
    secret_id, secret_key = resolve_credentials()
    from qcloud_cos import CosConfig, CosS3Client

    config = CosConfig(
        Region=REGION,
        SecretId=secret_id,
        SecretKey=secret_key,
        Scheme="https",
    )
    return CosS3Client(config)


def configure_website(cos: CosS3Client) -> bool:
    from qcloud_cos import CosServiceError

    try:
        current = cos.get_bucket_website(Bucket=BUCKET)
        suffix = (current.get("IndexDocument") or {}).get("Suffix")
        if suffix == KEY:
            return False
    except CosServiceError as exc:
        if exc.get_error_code() != "NoSuchWebsiteConfiguration":
            raise
    cos.put_bucket_website(
        Bucket=BUCKET,
        WebsiteConfiguration={"IndexDocument": {"Suffix": KEY}},
    )
    return True


def validate_html(path: Path) -> tuple[bytes, str]:
    if not path.is_file():
        raise FileNotFoundError(f"Dashboard HTML not found: {path}")
    body = path.read_bytes()
    if not body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError(f"Not a complete HTML document: {path}")
    local_sha256 = hashlib.sha256(body).hexdigest()
    return body, local_sha256


def upload(path: Path) -> dict[str, object]:
    mode = resolve_upload_mode()
    body, local_sha256 = validate_html(path)
    if mode == "skip":
        return {
            "ok": True,
            "status": "skipped",
            "mode": "skip",
            "sha256": local_sha256,
            "bytes": len(body),
        }
    # Compress HTML before upload to reduce transfer size.
    payload = gzip.compress(body, compresslevel=6, mtime=0)
    cos = client()
    website_changed = configure_website(cos)
    response = cos.put_object(
        Bucket=BUCKET,
        Key=KEY,
        Body=payload,
        ContentType="text/html; charset=utf-8",
        ContentEncoding="gzip",
        ContentDisposition="inline",
        # Cache the dashboard for five minutes.
        CacheControl="public, max-age=300",
        ACL="public-read",
    )
    etag = str(response.get("ETag", "")).strip('"')
    request = urllib.request.Request(
        PUBLIC_URL,
        headers={"User-Agent": "OpenClaw sentiment-dashboard verifier"},
    )
    with urllib.request.urlopen(request, timeout=30) as remote:
        remote_body = remote.read()
        content_type = remote.headers.get("Content-Type", "")
        content_encoding = remote.headers.get("Content-Encoding", "")
        content_disposition = remote.headers.get("Content-Disposition", "")
        cache_control = remote.headers.get("Cache-Control", "")
        force_download = remote.headers.get("x-cos-force-download", "").lower() == "true"
    # urllib 不会自动解压，回读拿到的是 gzip 原文，先还原再比 sha256。
    if content_encoding.lower() == "gzip":
        remote_body = gzip.decompress(remote_body)
    remote_sha256 = hashlib.sha256(remote_body).hexdigest()
    if remote_sha256 != local_sha256:
        raise RuntimeError(
            f"COS verification mismatch: local={local_sha256} remote={remote_sha256}"
        )
    return {
        "ok": True,
        "bucket": BUCKET,
        "region": REGION,
        "key": KEY,
        "bytes": len(body),
        "bytes_gzip": len(payload),
        "etag": etag,
        "sha256": local_sha256,
        "content_type": content_type,
        "content_encoding": content_encoding,
        "cache_control": cache_control,
        "content_disposition": content_disposition,
        "force_download": force_download,
        "browser_renderable": not force_download and "attachment" not in content_disposition.lower(),
        "origin_website_url": ORIGIN_WEBSITE_URL,
        "public_url": PUBLIC_URL,
        "website_configured": website_changed,
        "warnings": [
            "COS default domain forces HTML download; bind a custom domain before using this URL in Feishu."
        ] if force_download or "attachment" in content_disposition.lower() else [],
    }


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    out = text
    for env_name in (ENV_COS_SECRET_ID, ENV_COS_SECRET_KEY):
        raw = os.environ.get(env_name, "")
        for candidate in (raw, raw.strip()):
            if candidate and candidate in out:
                out = out.replace(candidate, "[REDACTED]")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload sentiment dashboard index.html to Tencent COS.")
    parser.add_argument("--file", type=Path, default=DEFAULT_HTML)
    args = parser.parse_args()
    try:
        result = upload(args.file.resolve())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": _redact_secrets(str(exc)),
                    "hint": "Check DALTON_COS_UPLOAD_MODE, COS environment/Keychain credentials, bucket website setting, and public-read access for index.html.",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
