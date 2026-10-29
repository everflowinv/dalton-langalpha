"""Host-side Volcengine Seed-ASR 2.0 and TOS S3-compatible client (Backlog A1c).

Enforces:
- Credentials read strictly from the host bridge process environment (`os.environ`),
  never from `.env` files, never printed/logged, and never passed in subprocess argv
  or child CLI environments.
- Strict 3-way validation on every Seed-ASR call: HTTP status code, provider
  `X-Api-Status-Code` header (missing header is a failure, fixing OpenClaw `:187`),
  and response JSON structure.
- Pre-submit `request_id` persistence and lost-response reconciliation: if the submit
  response is lost or ambiguous, status enters `indeterminate` and the client queries
  `API_QUERY` with the persisted `request_id` before any re-submission to prevent
  duplicate billing.
- Separate single-request timeout (`request_timeout_s`) and overall job deadline
  (`job_deadline_s`), with bounded exponential backoff for network / 429 / 5xx errors
  and immediate termination for parameter / auth / format errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import inspect
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid

import httpx

logger = logging.getLogger("researchdata-bridge")

API_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
API_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
RESOURCE_ID = "volc.seedasr.auc"

STATUS_SUCCESS = "20000000"
STATUS_PROCESSING = "20000001"
STATUS_QUEUED = "20000002"
STATUS_SILENT = "20000003"

TOS_PRESIGN_EXPIRY = 7200  # 2 hours (covers queue and fetch window)
MULTIPART_THRESHOLD = 20 * 1024 * 1024  # 20 MiB
MULTIPART_PART_SIZE = 10 * 1024 * 1024  # 10 MiB chunks

DEFAULT_REQUEST_TIMEOUT_S = 30.0
DEFAULT_JOB_DEADLINE_S = 1800.0
DEFAULT_POLL_INTERVAL_S = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF_S = 0.5
DEFAULT_MAX_BACKOFF_S = 8.0

ASR_CREDENTIAL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "VOLC_TOS_BUCKET",
        "VOLC_TOS_REGION",
        "VOLC_TOS_ENDPOINT",
    }
)

ASR_STORAGE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_ASR_STORAGE_REGION",
        "DALTON_ASR_STORAGE_ADDRESSING_STYLE",
        "DALTON_ASR_STORAGE_KEY_PREFIX",
    }
)

ASR_SUBPROCESS_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SYSTEMROOT",
)

_FORMAT_MAP: dict[str, str] = {
    ".mp3": "mp3",
    ".wav": "wav",
    ".ogg": "ogg",
    ".m4a": "m4a",
    ".flac": "flac",
    ".aac": "aac",
    ".opus": "ogg",
    ".wma": "wma",
    ".amr": "amr",
}

_CONTENT_TYPE_MAP: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
    "aac": "audio/aac",
    "wma": "audio/x-ms-wma",
    "amr": "audio/amr",
}

_RATE_LIMIT_CODES: frozenset[str] = frozenset({"45000006", "45000292"})
_AUTH_ERROR_CODES: frozenset[str] = frozenset(
    {"45000010", "45000030", "40100000", "40300000"}
)
_FORMAT_ERROR_CODES: frozenset[str] = frozenset(
    {"45000003", "45000004", "45000005", "45000151"}
)
_PARAM_ERROR_CODES: frozenset[str] = frozenset(
    {"45000001", "45000002", "40000001"}
)
_TASK_NOT_FOUND_CODES: frozenset[str] = frozenset(
    {"45000007", "45000404", "40400000"}
)

_URL_QUERY_REDACT_RE = re.compile(
    r"(?i)(https?://[^\s\"'<>?]+)\?(?=[^\s\"'<>]*(?:x-amz-|x-tos-|awsaccesskeyid=|signature=))[^\s\"'<>]*"
)
_HEADER_SECRET_REDACT_RE = re.compile(
    r"(?i)(\b(?:x-api-access-key|x-api-app-key|authorization|x-amz-credential|x-amz-signature|x-tos-credential|x-tos-signature)\s*[:=]\s*)([^\s,;\"'&]+)"
)


def detect_format(filepath_or_format: str | Path) -> str:
    """Detect Seed-ASR audio format string from a file path or format name."""
    raw = str(filepath_or_format or "").strip().lower()
    if not raw:
        return "mp3"
    if f".{raw}" in _FORMAT_MAP:
        return _FORMAT_MAP[f".{raw}"]
    ext = Path(raw).suffix.lower()
    return _FORMAT_MAP.get(ext, "mp3")


def normalize_asr_language(language: str | None) -> str | None:
    """Normalize optional ASR language hint.

    - Returns `None` when `language` is unset/empty so `audio.language` is omitted.
    - Normalizes English variants (`en`, `en-us`, `en_us`, `english`) to explicit `en-US`.
    """
    if language is None:
        return None
    cleaned = str(language).strip()
    if not cleaned:
        return None
    lower = cleaned.lower().replace("_", "-")
    if lower in ("en", "en-us", "english", "eng"):
        return "en-US"
    return cleaned


def build_asr_subprocess_env(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a strict allowlisted environment for ASR child processes (`ffmpeg` / `ffprobe`).

    Only non-secret OS runtime keys in `ASR_SUBPROCESS_ENV_ALLOWLIST` are copied;
    `SEED_ASR_*`, `VOLC_TOS_*`, `STORAGE_*`, and `DALTON_*` are never passed down.
    """
    src = base_env if base_env is not None else os.environ
    env: dict[str, str] = {}
    for key in ASR_SUBPROCESS_ENV_ALLOWLIST:
        val = src.get(key)
        if isinstance(val, str) and val.strip():
            env[key] = val
    if "PATH" not in env:
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    return env


def has_asr_credentials_in_env(
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True if `env` contains any `SEED_ASR_*`, `VOLC_TOS_*`, or `DALTON_ASR_STORAGE_*` variable."""
    src = env if env is not None else os.environ
    for k in src:
        upper = k.upper()
        if (
            k in ASR_CREDENTIAL_ENV_KEYS
            or k in ASR_STORAGE_ENV_KEYS
            or upper.startswith("SEED_ASR_")
            or upper.startswith("VOLC_TOS_")
            or upper.startswith("DALTON_ASR_STORAGE_")
        ):
            return True
    return False


def strip_asr_credentials_from_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a copy of `env` with all Seed-ASR, Volcengine TOS, and ASR storage variables removed."""
    src = env if env is not None else os.environ
    out: dict[str, str] = {}
    for k, v in src.items():
        upper = k.upper()
        if (
            k in ASR_CREDENTIAL_ENV_KEYS
            or k in ASR_STORAGE_ENV_KEYS
            or upper.startswith("SEED_ASR_")
            or upper.startswith("VOLC_TOS_")
            or upper.startswith("DALTON_ASR_STORAGE_")
        ):
            continue
        out[k] = v
    return out


@dataclass(frozen=True)
class SeedAsrCredentials:
    """Host-only Seed-ASR and Volcengine TOS credentials loaded strictly from `os.environ`."""

    app_id: str = field(repr=False)
    access_key: str = field(repr=False)
    tos_ak: str = field(repr=False)
    tos_sk: str = field(repr=False)
    tos_bucket: str
    tos_region: str = "cn-beijing"
    tos_endpoint: str = "tos-cn-beijing.volces.com"

    def __repr__(self) -> str:
        return (
            "SeedAsrCredentials("
            "app_id='[REDACTED]', "
            "access_key='[REDACTED]', "
            "tos_ak='[REDACTED]', "
            "tos_sk='[REDACTED]', "
            f"tos_bucket={self.tos_bucket!r}, "
            f"tos_region={self.tos_region!r}, "
            f"tos_endpoint={self.tos_endpoint!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    @classmethod
    def is_configured(cls, environ: Mapping[str, str] | None = None) -> bool:
        """Return True when all required Seed-ASR and TOS credentials are present in `environ`."""
        env = environ if environ is not None else os.environ
        required = (
            "SEED_ASR_APP_ID",
            "SEED_ASR_ACCESS_KEY",
            "VOLC_TOS_AK",
            "VOLC_TOS_SK",
            "VOLC_TOS_BUCKET",
        )
        return all(bool(str(env.get(k) or "").strip()) for k in required)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> SeedAsrCredentials:
        """Load credentials strictly from process environment `environ` (never from `.env` files)."""
        env = environ if environ is not None else os.environ
        app_id = str(env.get("SEED_ASR_APP_ID") or "").strip()
        access_key = str(env.get("SEED_ASR_ACCESS_KEY") or "").strip()
        tos_ak = str(env.get("VOLC_TOS_AK") or "").strip()
        tos_sk = str(env.get("VOLC_TOS_SK") or "").strip()
        tos_bucket = str(env.get("VOLC_TOS_BUCKET") or "").strip()
        tos_region = str(env.get("VOLC_TOS_REGION") or "cn-beijing").strip() or "cn-beijing"
        tos_endpoint = (
            str(env.get("VOLC_TOS_ENDPOINT") or "tos-cn-beijing.volces.com").strip()
            or "tos-cn-beijing.volces.com"
        )

        missing: list[str] = []
        if not app_id:
            missing.append("SEED_ASR_APP_ID")
        if not access_key:
            missing.append("SEED_ASR_ACCESS_KEY")
        if not tos_ak:
            missing.append("VOLC_TOS_AK")
        if not tos_sk:
            missing.append("VOLC_TOS_SK")
        if not tos_bucket:
            missing.append("VOLC_TOS_BUCKET")
        if missing:
            raise SeedAsrError(
                category="asr_credentials_missing",
                message=f"Missing required ASR environment variables: {', '.join(missing)}",
                retryable=False,
                submit_uncertain=False,
            )
        return cls(
            app_id=app_id,
            access_key=access_key,
            tos_ak=tos_ak,
            tos_sk=tos_sk,
            tos_bucket=tos_bucket,
            tos_region=tos_region,
            tos_endpoint=tos_endpoint,
        )


def redact_asr_secrets(
    text: str | None,
    credentials: SeedAsrCredentials | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Redact ASR/TOS credential values and presigned URL query strings from `text`."""
    if not text:
        return ""
    out = str(text)
    out = _URL_QUERY_REDACT_RE.sub(r"\1?[REDACTED]", out)
    out = _HEADER_SECRET_REDACT_RE.sub(r"\1[REDACTED]", out)

    secret_values: list[str] = []
    if credentials is not None:
        secret_values.extend(
            [
                credentials.app_id,
                credentials.access_key,
                credentials.tos_ak,
                credentials.tos_sk,
            ]
        )
    env = environ if environ is not None else os.environ
    for key in (
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "STORAGE_ACCESS_KEY_ID",
        "STORAGE_SECRET_ACCESS_KEY",
        "DALTON_BRIDGE_TOKEN",
        "DALTON_BRIDGE_ADMIN_TOKEN",
    ):
        val = str(env.get(key) or "").strip()
        if val:
            secret_values.append(val)

    for secret in sorted(set(secret_values), key=len, reverse=True):
        if secret and len(secret) >= 3 and secret in out:
            out = out.replace(secret, "[REDACTED]")
    return out


class SeedAsrError(Exception):
    """Structured error raised by `SeedAsrClient` and `VolcTosClient`."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        http_status: int | None = None,
        provider_status: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        submit_uncertain: bool = False,
        task_not_found: bool = False,
        phase: str = "submit",
    ) -> None:
        sanitized_msg = redact_asr_secrets(message)
        super().__init__(sanitized_msg)
        self.category = str(category)
        self.message = sanitized_msg
        self.http_status = http_status
        self.provider_status = provider_status
        self.request_id = request_id
        self.retryable = bool(retryable)
        self.submit_uncertain = bool(submit_uncertain)
        self.task_not_found = bool(task_not_found)
        self.phase = "query" if str(phase).strip().lower() == "query" else "submit"


class SeedAsrIndeterminateError(SeedAsrError):
    """Raised when a submission outcome or in-flight task status cannot be determined."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        request_id: str,
        http_status: int | None = None,
        provider_status: str | None = None,
        phase: str = "query",
    ) -> None:
        super().__init__(
            category,
            message,
            http_status=http_status,
            provider_status=provider_status,
            request_id=request_id,
            retryable=True,
            submit_uncertain=True,
            phase=phase,
        )


@dataclass
class SubmitOutcome:
    """Validated result of a Seed-ASR `submit` call."""

    request_id: str
    provider_status_code: str
    response: dict[str, Any]


@dataclass
class QueryOutcome:
    """Validated result of a Seed-ASR `query` call."""

    request_id: str
    state: str  # "succeeded" | "processing" | "queued"
    provider_status_code: str
    response: dict[str, Any]
    is_silent: bool = False


def build_submit_payload(
    *,
    audio_url: str,
    audio_format: str,
    language: str | None = None,
    hotwords: list[str] | None = None,
    uid: str = "dalton-asr",
) -> dict[str, Any]:
    """Construct the Seed-ASR 2.0 `bigmodel/submit` JSON payload.

    - Includes `enable_speaker_info: True`, `ssd_version: "300"`, `enable_itn/punc/ddc`,
      and `show_utterances: True`.
    - Sets `audio.language` ONLY when `language` is provided; English is normalized to `en-US`.
    """
    audio_block: dict[str, Any] = {
        "format": detect_format(audio_format),
        "url": str(audio_url).strip(),
    }
    norm_lang = normalize_asr_language(language)
    if norm_lang is not None:
        audio_block["language"] = norm_lang

    request_block: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "enable_speaker_info": True,
        "ssd_version": "300",
        "show_utterances": True,
    }
    if hotwords:
        hw_list = [{"word": str(w).strip()} for w in hotwords if str(w).strip()]
        if hw_list:
            request_block["corpus"] = {
                "context": json.dumps({"hotwords": hw_list}, ensure_ascii=False)
            }

    return {
        "user": {"uid": str(uid or "dalton-asr").strip() or "dalton-asr"},
        "audio": audio_block,
        "request": request_block,
    }


def _normalize_tos_endpoint(endpoint: str) -> tuple[str, str]:
    """Normalize TOS endpoint into `(endpoint_url_with_scheme, addressing_style)`."""
    cleaned = str(endpoint or "tos-cn-beijing.volces.com").strip().rstrip("/")
    if "://" not in cleaned:
        endpoint_url = f"https://{cleaned}"
    else:
        endpoint_url = cleaned
    host_part = endpoint_url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    if host_part in ("localhost", "127.0.0.1", "::1") or re.fullmatch(
        r"\d+\.\d+\.\d+\.\d+", host_part
    ):
        return endpoint_url, "path"
    return endpoint_url, "virtual"


class S3ClientProtocol:
    """Protocol class representing the S3-compatible client operations used by `VolcTosClient` and `SourceObjectStore`."""

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: Any,
        ContentLength: int | None = None,
        ContentType: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def create_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        ContentType: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def upload_part(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        PartNumber: int,
        Body: bytes,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def complete_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
        MultipartUpload: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def abort_multipart_upload(
        self,
        *,
        Bucket: str,
        Key: str,
        UploadId: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, Any] | None = None,
        ExpiresIn: int = 3600,
    ) -> str:
        raise NotImplementedError

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        raise NotImplementedError


class VolcTosClient:
    """S3-compatible Volcengine TOS client for temporary audio segment upload, presigning, and cleanup."""

    def __init__(
        self,
        credentials: SeedAsrCredentials,
        *,
        s3_client: Any | None = None,
        presign_expiry_s: int = TOS_PRESIGN_EXPIRY,
        multipart_threshold: int = MULTIPART_THRESHOLD,
        multipart_part_size: int = MULTIPART_PART_SIZE,
    ) -> None:
        self.credentials = credentials
        self.presign_expiry_s = max(60, int(presign_expiry_s))
        self.multipart_threshold = max(1, int(multipart_threshold))
        self.multipart_part_size = max(5 * 1024 * 1024, int(multipart_part_size))
        self._s3_client = s3_client

    def _get_s3_client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        # Imported here, not at module top: launchd runs the bridge with the
        # system Homebrew Python, which must import this module (and start)
        # even when boto3 is not installed and transcription is disabled.
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise SeedAsrError(
                "asr_boto3_missing",
                "boto3 未安装：录音转写需要在桥的 Python 环境安装 boto3 "
                "(boto3 is not installed; install it in the bridge's Python "
                "to enable audio transcription)",
                retryable=False,
            ) from exc
        endpoint_url, addressing_style = _normalize_tos_endpoint(
            self.credentials.tos_endpoint
        )
        self._s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=self.credentials.tos_ak,
            aws_secret_access_key=self.credentials.tos_sk,
            region_name=self.credentials.tos_region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return self._s3_client

    def upload_and_presign(
        self,
        filepath: str | Path,
        *,
        object_key: str | None = None,
        expires_in: int | None = None,
    ) -> tuple[str, str]:
        """Upload `filepath` to TOS (multipart when > 20 MiB in 10 MiB parts) and return `(presigned_get_url, object_key)`."""
        path = Path(filepath)
        if not path.is_file():
            raise SeedAsrError(
                "tos_upload_file_missing",
                f"Audio segment file not found: {path.name}",
                retryable=False,
            )

        file_size = path.stat().st_size
        if file_size <= 0:
            raise SeedAsrError(
                "tos_upload_file_empty",
                f"Audio segment file is empty: {path.name}",
                retryable=False,
            )

        ext = path.suffix.lower() or ".mp3"
        fmt = detect_format(ext)
        content_type = _CONTENT_TYPE_MAP.get(fmt, "application/octet-stream")
        resolved_key = object_key or f"asr-temp/{uuid.uuid4().hex}{ext}"
        bucket = self.credentials.tos_bucket
        expiry = int(expires_in if expires_in is not None else self.presign_expiry_s)
        client = self._get_s3_client()

        try:
            if file_size > self.multipart_threshold:
                mp = client.create_multipart_upload(
                    Bucket=bucket,
                    Key=resolved_key,
                    ContentType=content_type,
                )
                upload_id = str(mp.get("UploadId") or "")
                if not upload_id:
                    raise SeedAsrError(
                        "tos_multipart_init_failed",
                        "TOS create_multipart_upload returned no UploadId",
                        retryable=True,
                    )
                parts: list[dict[str, Any]] = []
                try:
                    with path.open("rb") as fh:
                        part_num = 1
                        while True:
                            chunk = fh.read(self.multipart_part_size)
                            if not chunk:
                                break
                            part_resp = client.upload_part(
                                Bucket=bucket,
                                Key=resolved_key,
                                UploadId=upload_id,
                                PartNumber=part_num,
                                Body=chunk,
                            )
                            etag = part_resp.get("ETag") if isinstance(part_resp, dict) else None
                            if not etag:
                                raise SeedAsrError(
                                    "tos_multipart_part_failed",
                                    f"TOS upload_part {part_num} returned no ETag",
                                    retryable=True,
                                )
                            parts.append({"PartNumber": part_num, "ETag": etag})
                            part_num += 1
                    client.complete_multipart_upload(
                        Bucket=bucket,
                        Key=resolved_key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )
                except Exception:
                    try:
                        client.abort_multipart_upload(
                            Bucket=bucket,
                            Key=resolved_key,
                            UploadId=upload_id,
                        )
                    except Exception as abort_exc:
                        logger.warning(
                            "TOS abort_multipart_upload failed for key=%s: %s",
                            resolved_key,
                            type(abort_exc).__name__,
                        )
                    raise
            else:
                with path.open("rb") as fh:
                    client.put_object(
                        Bucket=bucket,
                        Key=resolved_key,
                        Body=fh,
                        ContentLength=file_size,
                        ContentType=content_type,
                    )

            signed_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": resolved_key},
                ExpiresIn=expiry,
            )
            if not isinstance(signed_url, str) or not signed_url.startswith(
                ("http://", "https://")
            ):
                raise SeedAsrError(
                    "tos_presign_invalid",
                    "TOS generate_presigned_url did not return a valid HTTP(S) URL",
                    retryable=False,
                )
            return signed_url, resolved_key
        except SeedAsrError:
            raise
        except Exception as exc:
            raise SeedAsrError(
                "tos_upload_failed",
                f"TOS upload/presign failed ({type(exc).__name__}): {redact_asr_secrets(str(exc), self.credentials)}",
                retryable=True,
            ) from exc

    def delete_object(self, object_key: str) -> bool:
        """Delete temporary TOS object `object_key`. Treats missing object as success and never raises."""
        cleaned = str(object_key or "").strip()
        if not cleaned:
            return True
        try:
            client = self._get_s3_client()
            client.delete_object(Bucket=self.credentials.tos_bucket, Key=cleaned)
            return True
        except Exception as exc:
            msg = str(exc)
            if any(
                token in msg
                for token in ("NoSuchKey", "404", "NotFound", "not found")
            ):
                return True
            logger.warning(
                "TOS cleanup failed for key=%s (%s): %s",
                cleaned,
                type(exc).__name__,
                redact_asr_secrets(msg, self.credentials),
            )
            return False


def _extract_header_case_insensitive(
    headers: Mapping[str, str], target_lower: str
) -> str:
    for k, v in headers.items():
        if str(k).lower() == target_lower:
            return str(v).strip()
    return ""


def _classify_provider_failure(
    status_code: str,
    message: str,
    *,
    http_status: int,
    request_id: str,
    is_submit: bool,
) -> SeedAsrError:
    """Classify a non-20000000 provider status code into retryable vs immediate error categories."""
    phase = "submit" if is_submit else "query"
    detail = f"status={status_code}, message={message or 'none'}"
    if status_code in _RATE_LIMIT_CODES:
        return SeedAsrError(
            "asr_rate_limited",
            f"Seed-ASR rate limited ({detail})",
            http_status=http_status,
            provider_status=status_code,
            request_id=request_id,
            retryable=True,
            submit_uncertain=False,
            phase=phase,
        )
    if status_code in _AUTH_ERROR_CODES or status_code.startswith(("401", "403")):
        return SeedAsrError(
            "asr_auth_error",
            f"Seed-ASR authentication/authorization error ({detail})",
            http_status=http_status,
            provider_status=status_code,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase=phase,
        )
    if status_code in _FORMAT_ERROR_CODES:
        return SeedAsrError(
            "asr_format_error",
            f"Seed-ASR audio format/download error ({detail})",
            http_status=http_status,
            provider_status=status_code,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase=phase,
        )
    if status_code in _TASK_NOT_FOUND_CODES:
        return SeedAsrError(
            "asr_task_not_found",
            f"Seed-ASR task not found ({detail})",
            http_status=http_status,
            provider_status=status_code,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            task_not_found=True,
            phase=phase,
        )
    if status_code in _PARAM_ERROR_CODES or status_code.startswith("4"):
        return SeedAsrError(
            "asr_invalid_parameter",
            f"Seed-ASR parameter/request error ({detail})",
            http_status=http_status,
            provider_status=status_code,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase=phase,
        )
    if status_code.startswith("5"):
        return SeedAsrError(
            "asr_provider_server_error",
            f"Seed-ASR server error ({detail})",
            http_status=http_status,
            provider_status=status_code,
            request_id=request_id,
            retryable=True,
            submit_uncertain=is_submit,
            phase=phase,
        )
    return SeedAsrError(
        "asr_provider_error",
        f"Seed-ASR unexpected status ({detail})",
        http_status=http_status,
        provider_status=status_code,
        request_id=request_id,
        retryable=False,
        submit_uncertain=is_submit,
        phase=phase,
    )


def _validate_query_result_structure(
    parsed: Any, *, request_id: str, http_status: int, provider_status: str
) -> dict[str, Any]:
    """Validate that a `20000000` query response is a dict with a valid `result` block."""
    if not isinstance(parsed, dict):
        raise SeedAsrError(
            "asr_invalid_response_structure",
            "Seed-ASR query response is not a JSON object",
            http_status=http_status,
            provider_status=provider_status,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase="query",
        )
    result = parsed.get("result")
    if not isinstance(result, dict):
        raise SeedAsrError(
            "asr_invalid_response_structure",
            "Seed-ASR query response missing 'result' object",
            http_status=http_status,
            provider_status=provider_status,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase="query",
        )
    has_text = "text" in result
    has_utterances = "utterances" in result
    if not has_text and not has_utterances:
        raise SeedAsrError(
            "asr_invalid_response_structure",
            "Seed-ASR query 'result' must contain 'text' or 'utterances'",
            http_status=http_status,
            provider_status=provider_status,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase="query",
        )
    if has_text and result["text"] is not None and not isinstance(result["text"], str):
        raise SeedAsrError(
            "asr_invalid_response_structure",
            "Seed-ASR query 'result.text' must be a string",
            http_status=http_status,
            provider_status=provider_status,
            request_id=request_id,
            retryable=False,
            submit_uncertain=False,
            phase="query",
        )
    if has_utterances and result["utterances"] is not None:
        utts = result["utterances"]
        if not isinstance(utts, list) or not all(isinstance(u, dict) for u in utts):
            raise SeedAsrError(
                "asr_invalid_response_structure",
                "Seed-ASR query 'result.utterances' must be a list of objects",
                http_status=http_status,
                provider_status=provider_status,
                request_id=request_id,
                retryable=False,
                submit_uncertain=False,
                phase="query",
            )
    return parsed


async def _maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


def should_clear_request_id_on_error(
    exc: SeedAsrError, *, submit_accepted: bool
) -> bool:
    """Determine whether a persisted `provider_request_id` can be cleared after `exc`.

    A `provider_request_id` may ONLY be cleared in two cases:
    (a) Query (`exc.phase == "query"`) returned a provider business status code
        (`X-Api-Status-Code` in `_TASK_NOT_FOUND_CODES`, e.g. 45000007 / 45000404 /
        40400000) explicitly confirming that the task does not exist (`exc.task_not_found`);
    (b) The submit call itself (`exc.phase == "submit"`) returned a definite pre-acceptance
        rejection (`not exc.submit_uncertain`, such as HTTP 400/401/403/404/422, provider
        45000xxx parameter/auth/format rejection, or pre-creation rate-limit/deadline/persistence
        failure), AND the segment was never confirmed accepted (`submit_accepted=False`).

    All other query-side errors (bare HTTP 400/401/403/404/422 without a `task_not_found`
    business status code, missing status header, malformed JSON, 45000151 format/download
    error on query, HTTP 200 + 20000000 with invalid result structure, or exhausted
    network/429/5xx retries) return `False` so the original `provider_request_id` and
    TOS object are retained in `indeterminate`.
    """
    if exc.phase == "query":
        return bool(exc.task_not_found)
    if submit_accepted:
        return False
    return not exc.submit_uncertain


class SeedAsrClient:
    """Async Seed-ASR 2.0 HTTP client with strict validation, pre-submit request ID persistence, and query-first recovery."""

    def __init__(
        self,
        credentials: SeedAsrCredentials,
        *,
        http_client: httpx.AsyncClient | None = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        job_deadline_s: float = DEFAULT_JOB_DEADLINE_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff_s: float = DEFAULT_INITIAL_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.credentials = credentials
        self._http_client = http_client
        self.request_timeout_s = max(0.05, float(request_timeout_s))
        self.job_deadline_s = max(0.1, float(job_deadline_s))
        self.poll_interval_s = max(0.0, float(poll_interval_s))
        self.max_retries = max(0, int(max_retries))
        self.initial_backoff_s = max(0.0, float(initial_backoff_s))
        self.max_backoff_s = max(0.01, float(max_backoff_s))
        self._sleep = sleep_fn

    def _build_headers(self, request_id: str) -> dict[str, str]:
        return {
            "X-Api-App-Key": self.credentials.app_id,
            "X-Api-Access-Key": self.credentials.access_key,
            "X-Api-Resource-Id": RESOURCE_ID,
            "X-Api-Request-Id": str(request_id).strip(),
            "X-Api-Sequence": "-1",
            "Content-Type": "application/json",
        }

    async def _post_json(
        self, url: str, *, request_id: str, payload: dict[str, Any], is_submit: bool
    ) -> tuple[int, dict[str, str], str]:
        phase = "submit" if is_submit else "query"
        headers = self._build_headers(request_id)
        try:
            if self._http_client is not None:
                resp = await asyncio.wait_for(
                    self._http_client.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=self.request_timeout_s,
                    ),
                    timeout=self.request_timeout_s + 1.0,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self.request_timeout_s, trust_env=False
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            return int(resp.status_code), resp_headers, resp.text
        except SeedAsrError:
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise SeedAsrError(
                "asr_request_timeout",
                f"Seed-ASR {'submit' if is_submit else 'query'} timed out after {self.request_timeout_s}s ({type(exc).__name__})",
                request_id=request_id,
                retryable=True,
                submit_uncertain=is_submit,
                phase=phase,
            ) from exc
        except Exception as exc:
            raise SeedAsrError(
                "asr_network_error",
                f"Seed-ASR {'submit' if is_submit else 'query'} network error ({type(exc).__name__}): {redact_asr_secrets(str(exc), self.credentials)}",
                request_id=request_id,
                retryable=True,
                submit_uncertain=is_submit,
                phase=phase,
            ) from exc

    def _validate_http_and_headers(
        self,
        http_status: int,
        headers: Mapping[str, str],
        body_text: str,
        *,
        request_id: str,
        is_submit: bool,
    ) -> tuple[str, str]:
        phase = "submit" if is_submit else "query"
        status_code = _extract_header_case_insensitive(headers, "x-api-status-code")
        message = _extract_header_case_insensitive(headers, "x-api-message")

        if http_status in (401, 403):
            raise SeedAsrError(
                "asr_auth_error",
                f"Seed-ASR HTTP {http_status} authentication/authorization error",
                http_status=http_status,
                provider_status=status_code or None,
                request_id=request_id,
                retryable=False,
                submit_uncertain=False,
                phase=phase,
            )
        if http_status in (400, 404, 422):
            if not is_submit and status_code in _TASK_NOT_FOUND_CODES:
                raise SeedAsrError(
                    "asr_task_not_found",
                    f"Seed-ASR HTTP {http_status} task not found (status={status_code}, message={message or body_text[:120]})",
                    http_status=http_status,
                    provider_status=status_code,
                    request_id=request_id,
                    retryable=False,
                    submit_uncertain=False,
                    task_not_found=True,
                    phase=phase,
                )
            raise SeedAsrError(
                "asr_invalid_parameter",
                f"Seed-ASR HTTP {http_status} client error ({message or body_text[:120]})",
                http_status=http_status,
                provider_status=status_code or None,
                request_id=request_id,
                retryable=False,
                submit_uncertain=False,
                task_not_found=False,
                phase=phase,
            )
        if http_status == 429:
            raise SeedAsrError(
                "asr_rate_limited",
                f"Seed-ASR HTTP 429 rate limited ({message or 'too many requests'})",
                http_status=http_status,
                provider_status=status_code or None,
                request_id=request_id,
                retryable=True,
                submit_uncertain=False,
                phase=phase,
            )
        if http_status >= 500:
            raise SeedAsrError(
                "asr_http_server_error",
                f"Seed-ASR HTTP {http_status} server error",
                http_status=http_status,
                provider_status=status_code or None,
                request_id=request_id,
                retryable=True,
                submit_uncertain=is_submit,
                phase=phase,
            )
        if http_status != 200:
            raise SeedAsrError(
                "asr_http_error",
                f"Seed-ASR unexpected HTTP status {http_status}",
                http_status=http_status,
                provider_status=status_code or None,
                request_id=request_id,
                retryable=False,
                submit_uncertain=is_submit,
                phase=phase,
            )
        if not status_code:
            raise SeedAsrError(
                "asr_missing_status_code",
                "Seed-ASR response is missing required X-Api-Status-Code header",
                http_status=http_status,
                provider_status=None,
                request_id=request_id,
                retryable=False,
                submit_uncertain=is_submit,
                phase=phase,
            )
        return status_code, message

    async def submit_once(
        self,
        *,
        request_id: str,
        audio_url: str,
        audio_format: str,
        language: str | None = None,
        hotwords: list[str] | None = None,
        uid: str = "dalton-asr",
    ) -> SubmitOutcome:
        """Perform a single Seed-ASR `submit` request with strict 3-way validation."""
        payload = build_submit_payload(
            audio_url=audio_url,
            audio_format=audio_format,
            language=language,
            hotwords=hotwords,
            uid=uid,
        )
        http_status, headers, body_text = await self._post_json(
            API_SUBMIT, request_id=request_id, payload=payload, is_submit=True
        )
        status_code, message = self._validate_http_and_headers(
            http_status,
            headers,
            body_text,
            request_id=request_id,
            is_submit=True,
        )
        if status_code != STATUS_SUCCESS:
            raise _classify_provider_failure(
                status_code,
                message,
                http_status=http_status,
                request_id=request_id,
                is_submit=True,
            )

        parsed_body: dict[str, Any] = {}
        if body_text and body_text.strip():
            try:
                raw_json = json.loads(body_text)
            except Exception as exc:
                raise SeedAsrError(
                    "asr_invalid_response_structure",
                    "Seed-ASR submit returned malformed JSON body",
                    http_status=http_status,
                    provider_status=status_code,
                    request_id=request_id,
                    retryable=False,
                    submit_uncertain=True,
                    phase="submit",
                ) from exc
            if not isinstance(raw_json, dict):
                raise SeedAsrError(
                    "asr_invalid_response_structure",
                    "Seed-ASR submit body must be a JSON object",
                    http_status=http_status,
                    provider_status=status_code,
                    request_id=request_id,
                    retryable=False,
                    submit_uncertain=True,
                    phase="submit",
                )
            parsed_body = raw_json

        return SubmitOutcome(
            request_id=request_id,
            provider_status_code=status_code,
            response=parsed_body,
        )

    async def query_once(self, request_id: str) -> QueryOutcome:
        """Perform a single read-only Seed-ASR `query` request with strict 3-way validation."""
        http_status, headers, body_text = await self._post_json(
            API_QUERY, request_id=request_id, payload={}, is_submit=False
        )
        status_code, message = self._validate_http_and_headers(
            http_status,
            headers,
            body_text,
            request_id=request_id,
            is_submit=False,
        )

        if status_code in (STATUS_PROCESSING, STATUS_QUEUED):
            parsed_in_prog: dict[str, Any] = {}
            if body_text and body_text.strip():
                try:
                    raw_prog = json.loads(body_text)
                    if not isinstance(raw_prog, dict):
                        raise ValueError("Expected JSON object")
                    parsed_in_prog = raw_prog
                except Exception as exc:
                    raise SeedAsrError(
                        "asr_invalid_response_structure",
                        "Seed-ASR processing query returned invalid JSON body",
                        http_status=http_status,
                        provider_status=status_code,
                        request_id=request_id,
                        retryable=False,
                        phase="query",
                    ) from exc
            return QueryOutcome(
                request_id=request_id,
                state="processing" if status_code == STATUS_PROCESSING else "queued",
                provider_status_code=status_code,
                response=parsed_in_prog,
                is_silent=False,
            )

        if status_code == STATUS_SILENT:
            parsed_silent: dict[str, Any] = {}
            if body_text and body_text.strip():
                try:
                    raw_sil = json.loads(body_text)
                    if not isinstance(raw_sil, dict):
                        raise ValueError("Expected JSON object")
                    parsed_silent = raw_sil
                except Exception as exc:
                    raise SeedAsrError(
                        "asr_invalid_response_structure",
                        "Seed-ASR silent response returned invalid JSON body",
                        http_status=http_status,
                        provider_status=status_code,
                        request_id=request_id,
                        retryable=False,
                        phase="query",
                    ) from exc
            res_block = parsed_silent.get("result")
            if not isinstance(res_block, dict):
                parsed_silent["result"] = {"text": "", "utterances": []}
            else:
                res_block.setdefault("text", "")
                res_block.setdefault("utterances", [])
            parsed_silent["silent"] = True
            return QueryOutcome(
                request_id=request_id,
                state="succeeded",
                provider_status_code=status_code,
                response=parsed_silent,
                is_silent=True,
            )

        if status_code == STATUS_SUCCESS:
            if not body_text or not body_text.strip():
                raise SeedAsrError(
                    "asr_invalid_response_structure",
                    "Seed-ASR succeeded query returned an empty body",
                    http_status=http_status,
                    provider_status=status_code,
                    request_id=request_id,
                    retryable=False,
                    phase="query",
                )
            try:
                raw_ok = json.loads(body_text)
            except Exception as exc:
                raise SeedAsrError(
                    "asr_invalid_response_structure",
                    "Seed-ASR succeeded query returned malformed JSON",
                    http_status=http_status,
                    provider_status=status_code,
                    request_id=request_id,
                    retryable=False,
                    phase="query",
                ) from exc
            validated = _validate_query_result_structure(
                raw_ok,
                request_id=request_id,
                http_status=http_status,
                provider_status=status_code,
            )
            return QueryOutcome(
                request_id=request_id,
                state="succeeded",
                provider_status_code=status_code,
                response=validated,
                is_silent=False,
            )

        raise _classify_provider_failure(
            status_code,
            message,
            http_status=http_status,
            request_id=request_id,
            is_submit=False,
        )

    async def query_with_backoff(
        self,
        request_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> QueryOutcome:
        """Query `request_id` with bounded exponential backoff on transient network / 429 / 5xx errors."""
        attempt = 0
        while True:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise SeedAsrIndeterminateError(
                    "asr_poll_deadline_exceeded",
                    f"Overall job deadline exceeded while querying request_id={request_id}",
                    request_id=request_id,
                )
            try:
                return await self.query_once(request_id)
            except SeedAsrError as exc:
                if not exc.retryable:
                    raise
                if attempt >= self.max_retries:
                    raise SeedAsrIndeterminateError(
                        exc.category,
                        f"Seed-ASR query exhausted retries ({attempt + 1} attempts) for request_id={request_id}: {exc.message}",
                        request_id=request_id,
                        http_status=exc.http_status,
                        provider_status=exc.provider_status,
                    ) from exc
                backoff = min(
                    self.max_backoff_s,
                    self.initial_backoff_s * (2**attempt),
                )
                if deadline_monotonic is not None:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise SeedAsrIndeterminateError(
                            "asr_poll_deadline_exceeded",
                            f"Overall job deadline exceeded during backoff for request_id={request_id}",
                            request_id=request_id,
                        ) from exc
                    backoff = min(backoff, max(0.0, remaining))
                attempt += 1
                if backoff > 0:
                    await self._sleep(backoff)

    async def poll_until_complete(
        self,
        request_id: str,
        *,
        deadline_monotonic: float | None = None,
        initial_outcome: QueryOutcome | None = None,
    ) -> QueryOutcome:
        """Poll `API_QUERY` for `request_id` until `succeeded` or `deadline_monotonic`."""
        eff_deadline = (
            deadline_monotonic
            if deadline_monotonic is not None
            else (time.monotonic() + self.job_deadline_s)
        )
        outcome = initial_outcome
        while True:
            if outcome is not None:
                cur = outcome
                outcome = None
            else:
                if time.monotonic() >= eff_deadline:
                    raise SeedAsrIndeterminateError(
                        "asr_poll_deadline_exceeded",
                        f"Overall job deadline ({self.job_deadline_s}s) exceeded while waiting for transcription",
                        request_id=request_id,
                    )
                cur = await self.query_with_backoff(
                    request_id, deadline_monotonic=eff_deadline
                )

            if cur.state == "succeeded":
                return cur

            remaining = eff_deadline - time.monotonic()
            if remaining <= 0:
                raise SeedAsrIndeterminateError(
                    "asr_poll_deadline_exceeded",
                    f"Overall job deadline ({self.job_deadline_s}s) exceeded while provider status={cur.provider_status_code}",
                    request_id=request_id,
                    provider_status=cur.provider_status_code,
                )
            sleep_s = min(self.poll_interval_s, remaining)
            if sleep_s > 0:
                await self._sleep(sleep_s)

    async def transcribe_segment(
        self,
        *,
        audio_url: str,
        audio_format: str,
        persist_request_id_fn: Callable[[str], Awaitable[bool | None] | bool | None],
        language: str | None = None,
        hotwords: list[str] | None = None,
        uid: str = "dalton-asr",
        request_id: str | None = None,
        already_submitted: bool = False,
        on_status_change_fn: Callable[[str, str], Awaitable[None] | None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> QueryOutcome:
        """Submit (or resume) a single segment and poll until completion.

        Guarantees:
        1. Deadline is checked BEFORE generating or persisting a new `request_id`.
        2. `persist_request_id_fn(request_id)` is awaited and verified BEFORE any `API_SUBMIT` call.
        3. When `already_submitted=True` (resuming after restart or retrying an
           `indeterminate` segment), `API_SUBMIT` is NOT called; `API_QUERY` is called
           first with the persisted `request_id`.
        4. When `API_SUBMIT` loses its response or fails with `submit_uncertain=True`
           (timeout, connection reset, 5xx), status is set to `indeterminate` and
           `API_QUERY` is queried with the same `request_id` before any re-submission.
        """
        eff_deadline = (
            deadline_monotonic
            if deadline_monotonic is not None
            else (time.monotonic() + self.job_deadline_s)
        )
        initial_query_outcome: QueryOutcome | None = None

        if already_submitted:
            req_id = str(request_id or "").strip()
            if not req_id:
                raise SeedAsrError(
                    "asr_missing_request_id",
                    "Cannot query already-submitted segment without a persisted request_id",
                    retryable=False,
                    submit_uncertain=False,
                    phase="query",
                )
            # Query first with the persisted request_id; never blindly re-submit
            try:
                probe = await self.query_with_backoff(
                    req_id, deadline_monotonic=eff_deadline
                )
                if on_status_change_fn is not None:
                    await _maybe_await(on_status_change_fn("transcribing", req_id))
                if probe.state == "succeeded":
                    return probe
                initial_query_outcome = probe
            except SeedAsrIndeterminateError:
                if on_status_change_fn is not None:
                    await _maybe_await(on_status_change_fn("indeterminate", req_id))
                raise
            except SeedAsrError as exc:
                if not should_clear_request_id_on_error(exc, submit_accepted=True):
                    if on_status_change_fn is not None:
                        await _maybe_await(on_status_change_fn("indeterminate", req_id))
                    raise SeedAsrIndeterminateError(
                        exc.category,
                        exc.message,
                        request_id=req_id,
                        http_status=exc.http_status,
                        provider_status=exc.provider_status,
                    ) from exc
                raise
        else:
            # Step 0: Check job deadline BEFORE generating or persisting a new request_id
            if time.monotonic() >= eff_deadline:
                raise SeedAsrError(
                    "asr_job_deadline_exceeded",
                    "Overall job deadline exceeded before segment submission",
                    request_id=None,
                    retryable=True,
                    submit_uncertain=False,
                    phase="submit",
                )

            req_id = str(request_id or uuid.uuid4()).strip()

            # Step 1: Persist request_id and verify write succeeded BEFORE calling API_SUBMIT
            try:
                persist_ok = await _maybe_await(persist_request_id_fn(req_id))
            except SeedAsrError:
                raise
            except Exception as exc:
                raise SeedAsrError(
                    "asr_state_persist_failed",
                    f"Failed persisting request_id before submit ({type(exc).__name__}): {redact_asr_secrets(str(exc), self.credentials)}",
                    request_id=req_id,
                    retryable=True,
                    submit_uncertain=False,
                    phase="submit",
                ) from exc
            if persist_ok is False:
                raise SeedAsrError(
                    "asr_state_persist_failed",
                    "Failed persisting request_id to state file before submit",
                    request_id=req_id,
                    retryable=True,
                    submit_uncertain=False,
                    phase="submit",
                )

            # Step 2: Submit with bounded backoff ONLY for definite pre-creation rate-limits (429 / 45000292)
            submit_attempt = 0
            while True:
                if time.monotonic() >= eff_deadline:
                    # Every prior attempt (if any) was a pre-creation rate-limit with submit_uncertain=False;
                    # the request_id was never accepted by the provider.
                    raise SeedAsrError(
                        "asr_job_deadline_exceeded",
                        "Overall job deadline exceeded before segment submission was accepted",
                        request_id=req_id,
                        retryable=True,
                        submit_uncertain=False,
                        phase="submit",
                    )
                try:
                    await self.submit_once(
                        request_id=req_id,
                        audio_url=audio_url,
                        audio_format=audio_format,
                        language=language,
                        hotwords=hotwords,
                        uid=uid,
                    )
                    if on_status_change_fn is not None:
                        await _maybe_await(on_status_change_fn("transcribing", req_id))
                    break
                except SeedAsrError as submit_exc:
                    # Definite pre-task-creation rate limit (submit_uncertain is False, retryable is True)
                    if (
                        submit_exc.retryable
                        and not submit_exc.submit_uncertain
                        and submit_attempt < self.max_retries
                    ):
                        backoff = min(
                            self.max_backoff_s,
                            self.initial_backoff_s * (2**submit_attempt),
                        )
                        remaining = eff_deadline - time.monotonic()
                        if remaining <= 0:
                            raise SeedAsrError(
                                "asr_job_deadline_exceeded",
                                "Overall job deadline exceeded during submit rate-limit backoff",
                                request_id=req_id,
                                retryable=True,
                                submit_uncertain=False,
                                phase="submit",
                            ) from submit_exc
                        backoff = min(backoff, max(0.0, remaining))
                        submit_attempt += 1
                        if backoff > 0:
                            await self._sleep(backoff)
                        continue

                    # Definite non-uncertain client/auth/param/format/rate-limit error -> fail immediately
                    if not submit_exc.submit_uncertain:
                        raise

                    # Ambiguous submit outcome (lost response, timeout, connection drop, 5xx, missing status code):
                    # Mark indeterminate and query API_QUERY with the persisted req_id!
                    if on_status_change_fn is not None:
                        await _maybe_await(on_status_change_fn("indeterminate", req_id))

                    try:
                        probe = await self.query_with_backoff(
                            req_id, deadline_monotonic=eff_deadline
                        )
                    except SeedAsrIndeterminateError as indet_exc:
                        raise SeedAsrIndeterminateError(
                            submit_exc.category,
                            f"Submit outcome uncertain ({submit_exc.message}) and query reconciliation remained indeterminate ({indet_exc.message})",
                            request_id=req_id,
                            http_status=indet_exc.http_status or submit_exc.http_status,
                            provider_status=indet_exc.provider_status
                            or submit_exc.provider_status,
                        ) from indet_exc
                    except SeedAsrError as query_exc:
                        # Task may already have been accepted; ONLY business-code `task_not_found`
                        # (case (a)) may clear the request ID. All other query errors stay indeterminate.
                        if query_exc.phase == "query" and query_exc.task_not_found:
                            raise query_exc from submit_exc
                        raise SeedAsrIndeterminateError(
                            submit_exc.category,
                            f"Submit outcome uncertain ({submit_exc.message}) and query reconciliation failed ({query_exc.message})",
                            request_id=req_id,
                            http_status=query_exc.http_status or submit_exc.http_status,
                            provider_status=query_exc.provider_status
                            or submit_exc.provider_status,
                        ) from query_exc

                    # Query succeeded! The provider DID receive our submission; do NOT re-submit.
                    if on_status_change_fn is not None:
                        await _maybe_await(on_status_change_fn("transcribing", req_id))
                    if probe.state == "succeeded":
                        return probe
                    initial_query_outcome = probe
                    break

        try:
            return await self.poll_until_complete(
                req_id,
                deadline_monotonic=eff_deadline,
                initial_outcome=initial_query_outcome,
            )
        except SeedAsrIndeterminateError:
            if on_status_change_fn is not None:
                await _maybe_await(on_status_change_fn("indeterminate", req_id))
            raise
        except SeedAsrError as exc:
            if not should_clear_request_id_on_error(exc, submit_accepted=True):
                if on_status_change_fn is not None:
                    await _maybe_await(on_status_change_fn("indeterminate", req_id))
                raise SeedAsrIndeterminateError(
                    exc.category,
                    exc.message,
                    request_id=req_id,
                    http_status=exc.http_status,
                    provider_status=exc.provider_status,
                ) from exc
            raise


__all__ = [
    "API_QUERY",
    "API_SUBMIT",
    "ASR_CREDENTIAL_ENV_KEYS",
    "ASR_STORAGE_ENV_KEYS",
    "ASR_SUBPROCESS_ENV_ALLOWLIST",
    "DEFAULT_INITIAL_BACKOFF_S",
    "DEFAULT_JOB_DEADLINE_S",
    "DEFAULT_MAX_BACKOFF_S",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "MULTIPART_PART_SIZE",
    "MULTIPART_THRESHOLD",
    "QueryOutcome",
    "RESOURCE_ID",
    "S3ClientProtocol",
    "STATUS_PROCESSING",
    "STATUS_QUEUED",
    "STATUS_SILENT",
    "STATUS_SUCCESS",
    "SeedAsrClient",
    "SeedAsrCredentials",
    "SeedAsrError",
    "SeedAsrIndeterminateError",
    "SubmitOutcome",
    "TOS_PRESIGN_EXPIRY",
    "VolcTosClient",
    "build_asr_subprocess_env",
    "build_submit_payload",
    "detect_format",
    "normalize_asr_language",
    "redact_asr_secrets",
    "should_clear_request_id_on_error",
    "strip_asr_credentials_from_env",
]
