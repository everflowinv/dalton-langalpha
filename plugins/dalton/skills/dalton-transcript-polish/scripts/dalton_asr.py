#!/usr/bin/env python3
"""Sandbox CLI for Dalton Seed-ASR audio transcription jobs (Backlog A1d).

Pure Python standard library implementation supporting subcommands:
`run`, `status`, `result`, `retry`, and `cancel`.

Security & Behavioural Invariants:
1. Reads the task credential strictly from `--ticket-file` after verifying `lstat`
   is a regular file (not a symlink) with exact mode `0600` (`rw-------`).
2. Rejects any credential passed via CLI arguments (`--ticket`, `--token`, etc.)
   or environment variables (`DALTON_TRANSCRIPTION_TICKET`, `DALTON_ASR_TICKET`, etc.).
3. Accesses only backend transcription routes (`/api/v1/dalton/transcriptions*`),
   resolving the backend base URL from existing sandbox configuration sources.
4. `run` submits the job (if still `awaiting_upload`), polls status (printing
   stage, `segment_count`, `completed_segments`, and `updated_at` without
   fabricating percentages), backs off smoothly across transient network or
   bridge-restart errors without failing the job, and on `succeeded` writes
   `raw_response.json` and `transcript.txt` to `--output-dir` as separate files
   that never overwrite each other.
5. `stdout` / `stderr` never contain the verbatim transcript text or the ticket
   credential.
6. Stopping the polling process (`SIGINT` / `SIGTERM`, e.g. the 1-hour post-turn
   background command limit) does NOT cancel the backend transcription job:
   `job_id` is preserved so `status` and `result` can retrieve it later. Only
   explicit `cancel` cancels the job, stopping unsubmitted segments and reporting
   honestly that already-submitted Volcengine segments have unverified
   cancellation/billing semantics.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

TRANSCRIPTIONS_API_PREFIX = "/api/v1/dalton/transcriptions"
UPLOAD_PART_BYTES: int = 16 * 1024 * 1024  # 16 MiB
MAX_UPLOAD_PARTS: int = 128  # 128 * 16 MiB = 2 GiB
UPLOAD_PART_MAX_RETRIES: int = 3
RAW_STREAM_CHUNK_BYTES: int = 64 * 1024

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "indeterminate"}
)
TRANSIENT_HTTP_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

FORBIDDEN_CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "DALTON_TRANSCRIPTION_TICKET",
    "DALTON_TRANSCRIPTION_TOKEN",
    "DALTON_ASR_TICKET",
    "DALTON_ASR_TOKEN",
    "TRANSCRIPTION_TICKET",
    "TRANSCRIPTION_TOKEN",
    "ASR_TICKET",
    "ASR_TOKEN",
)

FORBIDDEN_CREDENTIAL_CLI_FLAGS: tuple[str, ...] = (
    "--ticket",
    "--token",
    "--ticket-token",
    "--auth-token",
    "--api-key",
    "--secret",
    "--credential",
)

PROVIDER_CANCEL_UNVERIFIED_NOTICE = (
    "已停止后续未提交的分片；已提交给火山 Seed-ASR 的分片能否在供应商侧取消"
    "及是否仍计费未核实，已提交分片无法保证取消或免除计费。"
)

_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TICKET_TOKEN_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}")
_EGRESS_CONFIG_CANDIDATES: tuple[str, ...] = (
    "_internal/.egress_relay.json",
)


class DaltonAsrCliError(Exception):
    """Structured CLI error with an error code and sanitized message."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        exit_code: int = 1,
        job_id: str | None = None,
        status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.exit_code = exit_code
        self.job_id = job_id
        self.status = status


class _PollingInterrupted(Exception):
    """Raised when SIGINT or SIGTERM stops the polling process."""


def redact_sensitive_text(text: str, ticket_secret: str | None = None) -> str:
    """Remove any ticket token from `text` before printing to stdout or stderr."""
    cleaned = str(text or "")
    if ticket_secret:
        secret_str = str(ticket_secret).strip()
        if secret_str:
            cleaned = cleaned.replace(secret_str, "<REDACTED_TICKET>")
            if "." in secret_str:
                for part in secret_str.split("."):
                    if len(part) >= 12:
                        cleaned = cleaned.replace(part, "<REDACTED_TICKET_PART>")
    cleaned = _TICKET_TOKEN_PATTERN.sub("<REDACTED_TICKET>", cleaned)
    return cleaned


def _emit_json(
    payload: Mapping[str, Any],
    *,
    ticket_secret: str | None = None,
    stream: Any = None,
) -> None:
    out_stream = stream if stream is not None else sys.stdout
    raw_json = json.dumps(dict(payload), ensure_ascii=False)
    safe_json = redact_sensitive_text(raw_json, ticket_secret)
    out_stream.write(safe_json + "\n")
    out_stream.flush()


def verify_no_credential_in_argv_or_env(
    argv: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Reject credentials passed via command-line arguments or environment variables."""
    for arg in argv:
        arg_str = str(arg or "").strip()
        if not arg_str.startswith("-"):
            continue
        flag_name = arg_str.split("=", 1)[0].strip().lower()
        if flag_name == "--ticket-file":
            continue
        if flag_name in FORBIDDEN_CREDENTIAL_CLI_FLAGS or flag_name.startswith(
            ("--ticket-", "--token-")
        ):
            raise DaltonAsrCliError(
                "credential_arg_forbidden",
                "Credentials must not be passed via command-line arguments; use --ticket-file pointing to a 0600 file.",
                exit_code=2,
            )

    env_map = environ if environ is not None else os.environ
    for env_key in FORBIDDEN_CREDENTIAL_ENV_VARS:
        val = str(env_map.get(env_key) or "").strip()
        if val:
            raise DaltonAsrCliError(
                "credential_env_forbidden",
                f"Credentials must not be passed via environment variable '{env_key}'; unset it and use --ticket-file pointing to a 0600 file.",
                exit_code=2,
            )


def load_and_verify_ticket_file(ticket_file: str | Path) -> dict[str, Any]:
    """Read and validate the `0600` JSON ticket file issued by `prepare_transcription`.

    Rejects symlinks, non-regular files, and any file whose permission bits are
    not strictly `0600`.
    """
    raw_path = str(ticket_file or "").strip()
    if not raw_path:
        raise DaltonAsrCliError(
            "missing_ticket_file",
            "--ticket-file is required.",
            exit_code=2,
        )
    path_obj = Path(raw_path)
    try:
        st = os.lstat(path_obj)
    except OSError as exc:
        raise DaltonAsrCliError(
            "ticket_file_unreadable",
            f"Cannot stat ticket file '{path_obj.name}': {exc.strerror or type(exc).__name__}",
            exit_code=2,
        ) from exc

    if stat.S_ISLNK(st.st_mode):
        raise DaltonAsrCliError(
            "ticket_file_symlink",
            f"Ticket file '{path_obj.name}' must not be a symbolic link.",
            exit_code=2,
        )
    if not stat.S_ISREG(st.st_mode):
        raise DaltonAsrCliError(
            "ticket_file_not_regular",
            f"Ticket file '{path_obj.name}' must be a regular file.",
            exit_code=2,
        )

    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        raise DaltonAsrCliError(
            "invalid_ticket_permissions",
            f"Ticket file '{path_obj.name}' permissions must be 0600 (got {oct(mode)}); refusing to read.",
            exit_code=2,
        )

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path_obj, open_flags)
    except OSError as exc:
        raise DaltonAsrCliError(
            "ticket_file_open_failed",
            f"Cannot open ticket file '{path_obj.name}': {exc.strerror or type(exc).__name__}",
            exit_code=2,
        ) from exc

    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_ino != st.st_ino or fst.st_dev != st.st_dev):
            raise DaltonAsrCliError(
                "ticket_file_changed",
                "Ticket file changed during open; refusing to read.",
                exit_code=2,
            )
        fmode = stat.S_IMODE(fst.st_mode)
        if fmode != 0o600:
            raise DaltonAsrCliError(
                "invalid_ticket_permissions",
                f"Ticket file '{path_obj.name}' permissions must be 0600 (got {oct(fmode)}); refusing to read.",
                exit_code=2,
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw_bytes = fh.read(256 * 1024)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise DaltonAsrCliError(
            "invalid_ticket_json",
            f"Ticket file '{path_obj.name}' is not valid JSON.",
            exit_code=2,
        ) from exc

    if not isinstance(parsed, dict):
        raise DaltonAsrCliError(
            "invalid_ticket_json",
            "Ticket file must contain a JSON object.",
            exit_code=2,
        )

    job_id = str(parsed.get("job_id") or "").strip()
    ticket = str(parsed.get("ticket") or "").strip()
    if not job_id or not _SAFE_JOB_ID_RE.fullmatch(job_id):
        raise DaltonAsrCliError(
            "invalid_ticket_job_id",
            "Ticket file is missing a valid 'job_id'.",
            exit_code=2,
        )
    if not ticket:
        raise DaltonAsrCliError(
            "invalid_ticket_missing_secret",
            "Ticket file is missing the 'ticket' credential.",
            exit_code=2,
        )
    return parsed


def resolve_backend_base_url(
    ticket_data: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    egress_config_paths: Sequence[str | Path] | None = None,
) -> str:
    """Resolve the backend base URL from existing sandbox configuration sources.

    Checks in order:
    1. `DALTON_API_URL`, `LANGALPHA_API_URL`, `API_BASE_URL` (standard Dalton sandbox
       script convention, matching `calendar_state.py`), then `EGRESS_RELAY_BASE_URL`.
    2. Sandbox `_internal/.egress_relay.json` (`relay_base_url`), written by the sandbox
       runtime (`mcp_client_runtime.py`).
    3. `api_base_url` / `backend_url` field in the ticket file (if present).
    4. `SERVER_BASE_URL` (rewriting `localhost`/`127.0.0.1` to `host.docker.internal`
       when inside Docker `/.dockerenv`, matching `reachability.py`).
    5. Default fallback (`http://host.docker.internal:8000` inside Docker else
       `http://127.0.0.1:8000`).
    """
    env_map = environ if environ is not None else os.environ

    for env_key in (
        "DALTON_API_URL",
        "LANGALPHA_API_URL",
        "API_BASE_URL",
        "EGRESS_RELAY_BASE_URL",
    ):
        val = str(env_map.get(env_key) or "").strip()
        if val:
            return _validate_base_url(val)

    candidates = (
        list(egress_config_paths)
        if egress_config_paths is not None
        else list(_EGRESS_CONFIG_CANDIDATES)
    )
    for cand in candidates:
        cpath = Path(cand)
        if cpath.is_file():
            try:
                cfg = json.loads(cpath.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    relay_url = str(
                        cfg.get("relay_base_url") or cfg.get("api_base_url") or ""
                    ).strip()
                    if relay_url:
                        return _validate_base_url(relay_url)
            except Exception:
                continue

    if isinstance(ticket_data, Mapping):
        for tk_key in ("api_base_url", "backend_url"):
            tk_url = str(ticket_data.get(tk_key) or "").strip()
            if tk_url:
                return _validate_base_url(tk_url)

    in_docker = Path("/.dockerenv").exists()
    srv_url = str(env_map.get("SERVER_BASE_URL") or "").strip()
    if srv_url:
        validated = _validate_base_url(srv_url)
        if in_docker:
            parsed = urllib.parse.urlsplit(validated)
            if parsed.hostname in ("127.0.0.1", "localhost"):
                port_part = f":{parsed.port}" if parsed.port else ""
                validated = urllib.parse.urlunsplit(
                    (
                        parsed.scheme,
                        f"host.docker.internal{port_part}",
                        parsed.path,
                        "",
                        "",
                    )
                ).rstrip("/")
        return validated

    if in_docker:
        return "http://host.docker.internal:8000"
    return "http://127.0.0.1:8000"


def _validate_base_url(raw_url: str) -> str:
    cleaned = str(raw_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise DaltonAsrCliError(
            "invalid_backend_url",
            f"Backend base URL must use http:// or https:// (got '{cleaned[:64]}').",
            exit_code=2,
        )
    return cleaned


def build_transcription_endpoint_url(base_url: str, sub_path: str = "") -> str:
    """Build a verified `/api/v1/dalton/transcriptions*` URL.

    Refuses any path that escapes `/api/v1/dalton/transcriptions`.
    """
    clean_base = _validate_base_url(base_url)
    base_parsed = urllib.parse.urlsplit(clean_base)
    base_path = base_parsed.path.rstrip("/")

    rel = str(sub_path or "").strip()
    if rel and not rel.startswith("/"):
        rel = "/" + rel

    if base_path.endswith(TRANSCRIPTIONS_API_PREFIX):
        full_path = f"{base_path}{rel}"
    else:
        full_path = f"{base_path}{TRANSCRIPTIONS_API_PREFIX}{rel}"

    normalized = urllib.parse.urlsplit(full_path).path
    if (
        ".." in normalized.split("/")
        or not (
            normalized == TRANSCRIPTIONS_API_PREFIX
            or normalized.startswith(TRANSCRIPTIONS_API_PREFIX + "/")
            or normalized.endswith(TRANSCRIPTIONS_API_PREFIX)
        )
    ):
        raise DaltonAsrCliError(
            "forbidden_endpoint_path",
            f"Refusing to access non-transcription endpoint path: {normalized}",
            exit_code=2,
        )

    return urllib.parse.urlunsplit(
        (base_parsed.scheme, base_parsed.netloc, normalized, "", "")
    )


class BackendTranscriptionClient:
    """Stdlib HTTP client restricted to `/api/v1/dalton/transcriptions*` with smooth retry/backoff."""

    def __init__(
        self,
        base_url: str,
        ticket: str,
        *,
        job_id: str,
        request_timeout_s: float = 30.0,
        initial_backoff_s: float = 0.5,
        max_backoff_s: float = 8.0,
        max_transient_retries: int = 8,
        sleep_fn: Callable[[float], None] = time.sleep,
        urlopen_fn: Callable[..., Any] | None = None,
        on_retry_fn: Callable[[int, str, float], None] | None = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self._ticket = str(ticket).strip()
        self.job_id = str(job_id).strip()
        self.request_timeout_s = max(1.0, float(request_timeout_s))
        self.initial_backoff_s = max(0.0, float(initial_backoff_s))
        self.max_backoff_s = max(0.0, float(max_backoff_s))
        self.max_transient_retries = max(1, int(max_transient_retries))
        self._sleep = sleep_fn
        self._on_retry = on_retry_fn
        if urlopen_fn is not None:
            self._urlopen = urlopen_fn
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            self._urlopen = opener.open

    def request_json(
        self,
        method: str,
        sub_path: str = "",
        body: Mapping[str, Any] | None = None,
        *,
        max_retries: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Perform an HTTP request to `/api/v1/dalton/transcriptions{sub_path}` with transient error backoff."""
        url = build_transcription_endpoint_url(self.base_url, sub_path)
        headers = {
            "Accept": "application/json",
            "X-Dalton-Transcription-Ticket": self._ticket,
            "Authorization": f"Bearer {self._ticket}",
        }
        data_bytes: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(dict(body), ensure_ascii=False).encode("utf-8")

        retry_limit = (
            max(0, int(max_retries))
            if max_retries is not None
            else self.max_transient_retries
        )
        attempt_idx = 0
        while True:
            req = urllib.request.Request(
                url=url,
                data=data_bytes,
                headers=headers,
                method=method.upper(),
            )
            try:
                with self._urlopen(req, timeout=self.request_timeout_s) as resp:
                    status_code = int(getattr(resp, "status", None) or resp.getcode() or 200)
                    raw_body = resp.read().decode("utf-8", errors="replace")
                    parsed = json.loads(raw_body) if raw_body.strip() else {}
                    if not isinstance(parsed, dict):
                        parsed = {"ok": False, "data": parsed}
                    if status_code in TRANSIENT_HTTP_STATUS_CODES and attempt_idx < retry_limit:
                        attempt_idx += 1
                        backoff = self._compute_backoff(attempt_idx)
                        if self._on_retry is not None:
                            self._on_retry(
                                attempt_idx, f"HTTP {status_code}", backoff
                            )
                        self._sleep(backoff)
                        continue
                    return status_code, parsed

            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                try:
                    parsed_err = json.loads(err_body) if err_body.strip() else {}
                    if not isinstance(parsed_err, dict):
                        parsed_err = {"detail": str(parsed_err)}
                except Exception:
                    parsed_err = {
                        "detail": redact_sensitive_text(
                            err_body.strip()[:240] or f"HTTP {status_code}",
                            self._ticket,
                        )
                    }
                if status_code in TRANSIENT_HTTP_STATUS_CODES and attempt_idx < retry_limit:
                    attempt_idx += 1
                    backoff = self._compute_backoff(attempt_idx)
                    if self._on_retry is not None:
                        self._on_retry(attempt_idx, f"HTTP {status_code}", backoff)
                    self._sleep(backoff)
                    continue
                return status_code, parsed_err

            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
                OSError,
            ) as exc:
                if isinstance(exc, _PollingInterrupted):
                    raise
                reason = redact_sensitive_text(
                    f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}",
                    self._ticket,
                )[:160]
                if attempt_idx < retry_limit:
                    attempt_idx += 1
                    backoff = self._compute_backoff(attempt_idx)
                    if self._on_retry is not None:
                        self._on_retry(attempt_idx, reason, backoff)
                    self._sleep(backoff)
                    continue
                raise DaltonAsrCliError(
                    "backend_unreachable",
                    f"Backend transcription endpoint unreachable after {attempt_idx + 1} attempts ({reason})",
                    job_id=self.job_id,
                ) from exc

    def put_presigned_part(self, upload_url: str, data: bytes) -> tuple[int, str]:
        """Upload one raw binary part (`PUT`) to a presigned S3 upload URL."""
        cleaned_url = str(upload_url or "").strip()
        parsed = urllib.parse.urlsplit(cleaned_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise DaltonAsrCliError(
                "invalid_upload_url",
                "Presigned upload URL must use http:// or https://.",
                job_id=self.job_id,
            )
        req = urllib.request.Request(
            url=cleaned_url,
            data=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(data)),
            },
            method="PUT",
        )
        with self._urlopen(req, timeout=self.request_timeout_s) as resp:
            status_code = int(getattr(resp, "status", None) or resp.getcode() or 200)
            etag = ""
            hdrs = getattr(resp, "headers", None)
            if hdrs is not None and hasattr(hdrs, "get"):
                etag = str(hdrs.get("ETag") or hdrs.get("etag") or "").strip()
            return status_code, etag

    def stream_raw_result_to_dir(
        self,
        job_id: str,
        output_dir: Path,
        *,
        max_retries: int | None = None,
    ) -> Path | None:
        """Stream `/api/v1/dalton/transcriptions/{job_id}/result/raw` atomically to `output_dir / 'raw_response.json'`.

        Returns the path to `raw_response.json` when available (`200`), or `None` when
        the raw provider response is unavailable (`404`). Never writes `null` to fake
        a missing raw file.
        """
        url = build_transcription_endpoint_url(self.base_url, f"/{job_id}/result/raw")
        headers = {
            "Accept": "application/json",
            "X-Dalton-Transcription-Ticket": self._ticket,
            "Authorization": f"Bearer {self._ticket}",
        }
        retry_limit = (
            max(0, int(max_retries))
            if max_retries is not None
            else self.max_transient_retries
        )
        attempt_idx = 0
        while True:
            req = urllib.request.Request(url=url, headers=headers, method="GET")
            try:
                with self._urlopen(req, timeout=self.request_timeout_s) as resp:
                    status_code = int(getattr(resp, "status", None) or resp.getcode() or 200)
                    if status_code in TRANSIENT_HTTP_STATUS_CODES and attempt_idx < retry_limit:
                        attempt_idx += 1
                        backoff = self._compute_backoff(attempt_idx)
                        if self._on_retry is not None:
                            self._on_retry(attempt_idx, f"HTTP {status_code}", backoff)
                        self._sleep(backoff)
                        continue
                    if status_code == 404:
                        return None
                    if status_code >= 400:
                        raise DaltonAsrCliError(
                            f"http_{status_code}",
                            f"Raw result download returned HTTP {status_code}",
                            job_id=self.job_id,
                        )

                    def _copy_stream(fh: Any) -> None:
                        while True:
                            chunk = resp.read(RAW_STREAM_CHUNK_BYTES)
                            if not chunk:
                                break
                            fh.write(chunk)

                    return _atomic_write_stream_in_dir(
                        output_dir,
                        "raw_response.json",
                        _copy_stream,
                        job_id=self.job_id,
                    )

            except urllib.error.HTTPError as exc:
                status_code = int(exc.code)
                if status_code == 404:
                    return None
                if status_code in TRANSIENT_HTTP_STATUS_CODES and attempt_idx < retry_limit:
                    attempt_idx += 1
                    backoff = self._compute_backoff(attempt_idx)
                    if self._on_retry is not None:
                        self._on_retry(attempt_idx, f"HTTP {status_code}", backoff)
                    self._sleep(backoff)
                    continue
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                raise DaltonAsrCliError(
                    f"http_{status_code}",
                    redact_sensitive_text(
                        err_body.strip()[:240] or f"HTTP {status_code}",
                        self._ticket,
                    ),
                    job_id=self.job_id,
                ) from exc

            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
                OSError,
            ) as exc:
                if isinstance(exc, (_PollingInterrupted, DaltonAsrCliError)):
                    raise
                reason = redact_sensitive_text(
                    f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}",
                    self._ticket,
                )[:160]
                if attempt_idx < retry_limit:
                    attempt_idx += 1
                    backoff = self._compute_backoff(attempt_idx)
                    if self._on_retry is not None:
                        self._on_retry(attempt_idx, reason, backoff)
                    self._sleep(backoff)
                    continue
                raise DaltonAsrCliError(
                    "backend_unreachable",
                    f"Backend raw result endpoint unreachable after {attempt_idx + 1} attempts ({reason})",
                    job_id=self.job_id,
                ) from exc

    def _compute_backoff(self, attempt_idx: int) -> float:
        if self.initial_backoff_s <= 0.0:
            return 0.0
        raw = self.initial_backoff_s * (2 ** max(0, attempt_idx - 1))
        return round(min(self.max_backoff_s, raw), 3)


def _extract_error_message(resp_data: Mapping[str, Any], status_code: int) -> str:
    for key in ("detail", "error", "message"):
        val = resp_data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:240]
    return f"HTTP {status_code}"


def _prepare_safe_output_dir(output_dir: Path, *, job_id: str | None = None) -> Path:
    """Ensure `output_dir` exists as a real directory and is never a symbolic link."""
    dir_obj = Path(output_dir)
    if os.path.lexists(dir_obj):
        st = os.lstat(dir_obj)
        if stat.S_ISLNK(st.st_mode):
            raise DaltonAsrCliError(
                "output_dir_symlink",
                f"Output directory '{dir_obj.name}' must not be a symbolic link.",
                exit_code=2,
                job_id=job_id,
            )
        if not stat.S_ISDIR(st.st_mode):
            raise DaltonAsrCliError(
                "output_dir_not_directory",
                f"Output path '{dir_obj.name}' is not a directory.",
                exit_code=2,
                job_id=job_id,
            )
        return dir_obj

    missing: list[Path] = []
    cur = dir_obj
    while not os.path.lexists(cur):
        missing.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    if os.path.lexists(cur):
        st_anc = os.lstat(cur)
        if stat.S_ISLNK(st_anc.st_mode):
            raise DaltonAsrCliError(
                "output_dir_symlink",
                f"Output directory ancestor '{cur.name}' must not be a symbolic link.",
                exit_code=2,
                job_id=job_id,
            )
        if not stat.S_ISDIR(st_anc.st_mode):
            raise DaltonAsrCliError(
                "output_dir_not_directory",
                f"Output directory ancestor '{cur.name}' is not a directory.",
                exit_code=2,
                job_id=job_id,
            )

    for to_make in reversed(missing):
        try:
            os.mkdir(to_make, 0o755)
        except FileExistsError:
            pass
        st_made = os.lstat(to_make)
        if stat.S_ISLNK(st_made.st_mode) or not stat.S_ISDIR(st_made.st_mode):
            raise DaltonAsrCliError(
                "output_dir_symlink",
                f"Output directory '{to_make.name}' must be a real directory, not a symbolic link.",
                exit_code=2,
                job_id=job_id,
            )
    return dir_obj


def _atomic_write_stream_in_dir(
    output_dir: Path,
    filename: str,
    write_fn: Callable[[Any], None],
    *,
    job_id: str | None = None,
) -> Path:
    """Write a file in `output_dir` via an exclusive temporary file followed by `os.replace`.

    Because `os.replace` replaces the directory entry itself inside `output_dir`,
    it never follows an existing symlink at `output_dir / filename`.
    """
    safe_dir = _prepare_safe_output_dir(output_dir, job_id=job_id)
    tmp_name = f".{filename}.{os.getpid()}.{time.time_ns()}.tmp"
    tmp_path = safe_dir / tmp_name
    target_path = safe_dir / filename
    open_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    try:
        fd = os.open(tmp_path, open_flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            write_fn(fh)
            fh.flush()
        os.replace(tmp_path, target_path)
        return target_path
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.lexists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def _atomic_write_bytes_in_dir(
    output_dir: Path,
    filename: str,
    payload_bytes: bytes,
    *,
    job_id: str | None = None,
) -> Path:
    return _atomic_write_stream_in_dir(
        output_dir,
        filename,
        lambda fh: fh.write(payload_bytes),
        job_id=job_id,
    )


def resolve_workspace_root_for_ticket(
    ticket_file: str | Path | None,
    ticket_data: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    explicit_root: str | Path | None = None,
) -> Path:
    """Determine the workspace root directory for resolving `source_path`."""
    if explicit_root is not None:
        return Path(explicit_root)
    env_map = environ if environ is not None else os.environ
    env_ws = str(env_map.get("DALTON_WORKSPACE_ROOT") or "").strip()
    if env_ws:
        return Path(env_ws)

    raw_src = str(ticket_data.get("source_path") or "").strip()
    rel_ok = (
        bool(raw_src)
        and not raw_src.startswith(("/", "\\"))
        and not Path(raw_src).is_absolute()
        and ".." not in Path(raw_src).parts
    )
    cwd = Path.cwd()
    if rel_ok and os.path.lexists(cwd / raw_src):
        return cwd

    if ticket_file is not None:
        tf_path = Path(ticket_file)
        parts = tf_path.parts
        if ".agents" in parts:
            idx = parts.index(".agents")
            if idx > 0:
                ws_cand = Path(*parts[:idx])
                if not rel_ok or os.path.lexists(ws_cand / raw_src):
                    return ws_cand
        if rel_ok and os.path.lexists(tf_path.parent / raw_src):
            return tf_path.parent
    return cwd


def resolve_and_verify_source_audio_path(
    raw_source_path: Any,
    *,
    workspace_root: str | Path | None = None,
    job_id: str | None = None,
) -> Path:
    """Validate `source_path` from the ticket (relative to `workspace_root`, rejecting absolute paths, `..`, and symlinks)."""
    rel_str = str(raw_source_path or "").strip()
    if not rel_str:
        raise DaltonAsrCliError(
            "missing_source_path",
            "Ticket is missing 'source_path' required for audio upload.",
            exit_code=2,
            job_id=job_id,
        )
    if (
        rel_str.startswith(("/", "\\"))
        or "\x00" in rel_str
        or Path(rel_str).is_absolute()
        or (len(rel_str) >= 2 and rel_str[1] == ":")
    ):
        raise DaltonAsrCliError(
            "invalid_source_path",
            "Ticket 'source_path' must be relative to workspace root; absolute paths are refused.",
            exit_code=2,
            job_id=job_id,
        )

    norm_str = rel_str.replace("\\", "/")
    raw_segments = [seg for seg in norm_str.split("/") if seg != ""]
    if not raw_segments or any(seg in (".", "..") for seg in raw_segments):
        raise DaltonAsrCliError(
            "invalid_source_path",
            "Ticket 'source_path' must not contain '..' or traversal segments.",
            exit_code=2,
            job_id=job_id,
        )

    base_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
    if os.path.lexists(base_root):
        st_root = os.lstat(base_root)
        if stat.S_ISLNK(st_root.st_mode):
            raise DaltonAsrCliError(
                "invalid_source_path",
                "Workspace root must not be a symbolic link.",
                exit_code=2,
                job_id=job_id,
            )

    cur = base_root
    st_cur: os.stat_result | None = None
    for seg in raw_segments:
        cur = cur / seg
        try:
            st_cur = os.lstat(cur)
        except OSError as exc:
            raise DaltonAsrCliError(
                "source_file_unreadable",
                f"Cannot stat source audio file '{rel_str}': {exc.strerror or type(exc).__name__}",
                exit_code=2,
                job_id=job_id,
            ) from exc
        if stat.S_ISLNK(st_cur.st_mode):
            raise DaltonAsrCliError(
                "source_path_symlink",
                f"Source audio path '{rel_str}' must not be or traverse a symbolic link.",
                exit_code=2,
                job_id=job_id,
            )

    if st_cur is None or not stat.S_ISREG(st_cur.st_mode):
        raise DaltonAsrCliError(
            "source_file_not_regular",
            f"Source audio file '{rel_str}' must be a regular file.",
            exit_code=2,
            job_id=job_id,
        )
    return cur


def upload_source_audio_if_needed(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    workspace_root: str | Path | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    open_file_fn: Callable[[Path], Any] | None = None,
    ticket_secret: str,
    stream: Any = None,
) -> dict[str, Any]:
    """Stream-upload `source_path` in fixed 16 MiB parts with resume, incremental SHA-256 verification, and 3x part retry."""
    job_id = str(ticket_data["job_id"])
    source_file = resolve_and_verify_source_audio_path(
        ticket_data.get("source_path"),
        workspace_root=workspace_root,
        job_id=job_id,
    )

    code_start, resp_start = client.request_json("POST", f"/{job_id}/upload/start", body={})
    if code_start >= 400 or resp_start.get("ok") is False:
        raise DaltonAsrCliError(
            f"http_{code_start}",
            _extract_error_message(resp_start, code_start),
            job_id=job_id,
        )

    if bool(resp_start.get("already_completed")) or bool(resp_start.get("completed")):
        return resp_start

    upload_id = str(resp_start.get("upload_id") or "").strip()
    if not upload_id:
        raise DaltonAsrCliError(
            "missing_upload_id",
            "Backend did not return an upload_id for multipart upload.",
            job_id=job_id,
        )
    part_size = int(resp_start.get("part_size") or UPLOAD_PART_BYTES)
    if part_size <= 0:
        part_size = UPLOAD_PART_BYTES
    expected_size = int(
        resp_start.get("source_size_bytes")
        or ticket_data.get("source_size_bytes")
        or 0
    )
    part_count = int(
        resp_start.get("part_count")
        or (max(1, (expected_size + part_size - 1) // part_size) if expected_size > 0 else 1)
    )
    if part_count > MAX_UPLOAD_PARTS:
        raise DaltonAsrCliError(
            "upload_too_many_parts",
            f"Audio exceeds maximum allowed part count ({MAX_UPLOAD_PARTS} parts = 2 GiB).",
            job_id=job_id,
        )

    raw_uploaded = (
        resp_start.get("uploaded_parts")
        if isinstance(resp_start.get("uploaded_parts"), list)
        else []
    )
    uploaded_by_part: dict[int, dict[str, Any]] = {}
    for item in raw_uploaded:
        if not isinstance(item, Mapping):
            continue
        pnum = int(item.get("part_number") or item.get("PartNumber") or 0)
        etag = str(item.get("etag") or item.get("ETag") or "").strip()
        psize = int(item.get("size") or item.get("Size") or 0)
        if pnum >= 1 and etag:
            uploaded_by_part[pnum] = {
                "part_number": pnum,
                "etag": etag,
                "size": psize,
            }

    hasher = hashlib.sha256()
    total_bytes_read = 0
    parts_seen = 0

    def _default_open_binary(path_obj: Path) -> Any:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path_obj, flags)
        return os.fdopen(fd, "rb")

    opener = open_file_fn if open_file_fn is not None else _default_open_binary
    with opener(source_file) as fh:
        while True:
            chunk = fh.read(part_size)
            if not chunk:
                break
            parts_seen += 1
            total_bytes_read += len(chunk)
            hasher.update(chunk)

            if parts_seen > part_count:
                client.request_json(
                    "POST",
                    f"/{job_id}/upload/abort",
                    body={"upload_id": upload_id, "reason": "source_size_mismatch"},
                )
                raise DaltonAsrCliError(
                    "source_size_mismatch",
                    f"Local audio file has more than {part_count} parts; multipart upload aborted.",
                    job_id=job_id,
                )

            existing = uploaded_by_part.get(parts_seen)
            if existing is not None and int(existing.get("size") or 0) == len(chunk):
                _emit_json(
                    {
                        "event": "upload_progress",
                        "job_id": job_id,
                        "part_number": parts_seen,
                        "uploaded_parts": parts_seen,
                        "total_parts": part_count,
                        "resumed": True,
                        "progress": f"已上传 {parts_seen} / {part_count} 片",
                    },
                    ticket_secret=ticket_secret,
                    stream=stream,
                )
                continue

            for retry_idx in range(UPLOAD_PART_MAX_RETRIES + 1):
                code_sign, resp_sign = client.request_json(
                    "POST",
                    f"/{job_id}/upload/part",
                    body={"part_number": parts_seen, "upload_id": upload_id},
                )
                if code_sign >= 400 or resp_sign.get("ok") is False:
                    raise DaltonAsrCliError(
                        f"http_{code_sign}",
                        _extract_error_message(resp_sign, code_sign),
                        job_id=job_id,
                    )
                upload_url = str(resp_sign.get("upload_url") or "").strip()
                try:
                    put_code, _etag = client.put_presigned_part(upload_url, chunk)
                    if 200 <= put_code < 300:
                        break
                    raise urllib.error.URLError(f"HTTP {put_code}")
                except (
                    urllib.error.HTTPError,
                    urllib.error.URLError,
                    TimeoutError,
                    ConnectionError,
                    http.client.HTTPException,
                    OSError,
                ) as exc:
                    if isinstance(exc, (_PollingInterrupted, DaltonAsrCliError)):
                        raise
                    if retry_idx < UPLOAD_PART_MAX_RETRIES:
                        backoff = client._compute_backoff(retry_idx + 1)
                        sleep_fn(backoff)
                        continue
                    raise DaltonAsrCliError(
                        "upload_part_failed",
                        f"Failed to PUT upload part {parts_seen}/{part_count} after {retry_idx + 1} attempts.",
                        job_id=job_id,
                    ) from exc

            _emit_json(
                {
                    "event": "upload_progress",
                    "job_id": job_id,
                    "part_number": parts_seen,
                    "uploaded_parts": parts_seen,
                    "total_parts": part_count,
                    "resumed": False,
                    "progress": f"已上传 {parts_seen} / {part_count} 片",
                },
                ticket_secret=ticket_secret,
                stream=stream,
            )

    expected_sha256 = str(ticket_data.get("source_sha256") or "").strip().lower()
    computed_sha256 = hasher.hexdigest().lower()
    if expected_sha256 and computed_sha256 != expected_sha256:
        client.request_json(
            "POST",
            f"/{job_id}/upload/abort",
            body={"upload_id": upload_id, "reason": "source_sha256_mismatch"},
        )
        raise DaltonAsrCliError(
            "source_sha256_mismatch",
            "Source audio SHA-256 does not match ticket source_sha256; upload aborted without completing.",
            job_id=job_id,
        )

    code_comp, resp_comp = client.request_json(
        "POST",
        f"/{job_id}/upload/complete",
        body={"upload_id": upload_id},
    )
    if code_comp >= 400 or resp_comp.get("ok") is False:
        raise DaltonAsrCliError(
            f"http_{code_comp}",
            _extract_error_message(resp_comp, code_comp),
            job_id=job_id,
        )
    return resp_comp


def write_transcription_outputs(
    output_dir: Path,
    result_response: Mapping[str, Any],
    *,
    raw_json_path: Path | None = None,
    raw_response_available: bool | None = None,
) -> dict[str, Any]:
    """Atomically write transcription outputs (`raw_response.json`, `transcript.txt`, `transcription_meta.json`) to `output_dir`.

    1. Rejects `output_dir` if it is a symbolic link.
    2. Writes temporary files inside `output_dir` and commits via `os.replace` so
       existing symlinks inside `output_dir` are never followed.
    3. If `raw_json_path` was already streamed from `/result/raw`, preserves it and
       records `raw_response_available=True`. When raw provider JSON is unavailable,
       never writes `null` to fake `raw_response.json`, and sets
       `raw_response_available=False` in `transcription_meta.json`.
    """
    job_id = str(result_response.get("job_id") or "").strip()
    safe_dir = _prepare_safe_output_dir(output_dir, job_id=job_id or None)
    result_obj = (
        result_response.get("result")
        if isinstance(result_response.get("result"), Mapping)
        else {}
    )

    merged_text = str(result_obj.get("text") or "")
    utterances = (
        list(result_obj.get("utterances"))
        if isinstance(result_obj.get("utterances"), list)
        else []
    )
    segments = (
        list(result_obj.get("segments"))
        if isinstance(result_obj.get("segments"), list)
        else []
    )
    provider_req_ids = (
        list(result_obj.get("provider_request_ids"))
        if isinstance(result_obj.get("provider_request_ids"), list)
        else []
    )
    duration_s = float(result_obj.get("duration_seconds") or 0.0)
    language = result_obj.get("language")
    usage = (
        dict(result_response.get("usage"))
        if isinstance(result_response.get("usage"), Mapping)
        else (
            dict(result_obj.get("usage"))
            if isinstance(result_obj.get("usage"), Mapping)
            else {}
        )
    )

    final_raw_path: Path | None = raw_json_path
    if final_raw_path is not None:
        has_raw = True
    elif raw_response_available is False:
        has_raw = False
    else:
        embedded_raw = result_obj.get("raw_response")
        if embedded_raw is not None:
            has_raw = True
            raw_bytes = (
                json.dumps(embedded_raw, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            final_raw_path = _atomic_write_bytes_in_dir(
                safe_dir,
                "raw_response.json",
                raw_bytes,
                job_id=job_id or None,
            )
        else:
            has_raw = False
            final_raw_path = None

    transcript_text = (
        (merged_text + "\n")
        if merged_text and not merged_text.endswith("\n")
        else merged_text
    )
    transcript_path = _atomic_write_bytes_in_dir(
        safe_dir,
        "transcript.txt",
        transcript_text.encode("utf-8"),
        job_id=job_id or None,
    )

    meta_payload = {
        "job_id": job_id,
        "status": str(result_response.get("status") or "succeeded"),
        "stage": "succeeded",
        "language": language,
        "duration_seconds": duration_s,
        "segment_count": len(segments) if segments else 1,
        "utterance_count": len(utterances),
        "provider_request_ids": provider_req_ids,
        "usage": usage,
        "raw_response_available": has_raw,
        "raw_json_path": str(final_raw_path) if final_raw_path is not None else None,
        "transcript_path": str(transcript_path),
    }
    meta_bytes = (json.dumps(meta_payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    meta_path = _atomic_write_bytes_in_dir(
        safe_dir,
        "transcription_meta.json",
        meta_bytes,
        job_id=job_id or None,
    )

    return {
        "output_dir": str(safe_dir),
        "raw_response_available": has_raw,
        "raw_json_path": str(final_raw_path) if final_raw_path is not None else None,
        "transcript_path": str(transcript_path),
        "meta_path": str(meta_path),
    }


def _format_job_progress_summary(
    job: Mapping[str, Any],
    *,
    event: str = "status",
    bridge_sync_status: str | None = None,
) -> dict[str, Any]:
    """Build a concise progress/status dict without fabricating any completion percentage."""
    status_str = str(job.get("status") or "unknown")
    stage_str = str(job.get("stage") or status_str)
    seg_cnt = int(job.get("segment_count") or 0)
    comp_cnt = int(job.get("completed_segments") or 0)
    summary: dict[str, Any] = {
        "event": event,
        "job_id": str(job.get("job_id") or ""),
        "status": status_str,
        "stage": stage_str,
        "segment_count": seg_cnt,
        "completed_segments": comp_cnt,
        "updated_at": job.get("updated_at"),
    }
    if bridge_sync_status:
        summary["bridge_sync_status"] = bridge_sync_status
    if job.get("error_category"):
        summary["error_category"] = str(job.get("error_category"))
    if job.get("error_message"):
        summary["error"] = str(job.get("error_message"))[:200]
    return summary


def _parse_hotwords_arg(raw_hotwords: str | Sequence[str] | None) -> list[str] | None:
    if raw_hotwords is None:
        return None
    if isinstance(raw_hotwords, str):
        items = [w.strip() for w in raw_hotwords.replace("，", ",").split(",") if w.strip()]
        return items
    return [str(w).strip() for w in raw_hotwords if str(w).strip()]


def cmd_status(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    ticket_secret: str,
    stream: Any = None,
) -> int:
    job_id = str(ticket_data["job_id"])
    code, resp = client.request_json("GET", f"/{job_id}")
    if code >= 400 or resp.get("ok") is False:
        err_msg = _extract_error_message(resp, code)
        _emit_json(
            {
                "ok": False,
                "job_id": job_id,
                "error_code": f"http_{code}",
                "error": err_msg,
            },
            ticket_secret=ticket_secret,
            stream=stream,
        )
        return 1

    job = resp.get("job") if isinstance(resp.get("job"), Mapping) else resp
    summary = _format_job_progress_summary(
        job,
        event="status",
        bridge_sync_status=str(resp.get("bridge_sync_status") or ""),
    )
    summary["ok"] = True
    _emit_json(summary, ticket_secret=ticket_secret, stream=stream)
    return 0


def cmd_result(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    output_dir: Path,
    ticket_secret: str,
    stream: Any = None,
) -> int:
    job_id = str(ticket_data["job_id"])
    _prepare_safe_output_dir(output_dir, job_id=job_id)
    code, resp = client.request_json("GET", f"/{job_id}/result")
    if code >= 400 or resp.get("ok") is False:
        err_msg = _extract_error_message(resp, code)
        _emit_json(
            {
                "ok": False,
                "job_id": job_id,
                "status": str(resp.get("status") or "not_ready"),
                "error_code": f"http_{code}",
                "error": err_msg,
            },
            ticket_secret=ticket_secret,
            stream=stream,
        )
        return 1

    raw_path = client.stream_raw_result_to_dir(job_id, output_dir)
    paths = write_transcription_outputs(
        output_dir,
        resp,
        raw_json_path=raw_path,
        raw_response_available=raw_path is not None,
    )
    result_obj = resp.get("result") if isinstance(resp.get("result"), Mapping) else {}
    segments = (
        result_obj.get("segments")
        if isinstance(result_obj.get("segments"), list)
        else []
    )
    _emit_json(
        {
            "ok": True,
            "event": "result_saved",
            "job_id": job_id,
            "status": "succeeded",
            "stage": "succeeded",
            "segment_count": len(segments) if segments else 1,
            "completed_segments": len(segments) if segments else 1,
            "duration_seconds": float(result_obj.get("duration_seconds") or 0.0),
            **paths,
        },
        ticket_secret=ticket_secret,
        stream=stream,
    )
    return 0


def cmd_cancel(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    ticket_secret: str,
    stream: Any = None,
) -> int:
    job_id = str(ticket_data["job_id"])
    code, resp = client.request_json("POST", f"/{job_id}/cancel", body={})
    if code >= 400 or resp.get("ok") is False:
        err_msg = _extract_error_message(resp, code)
        _emit_json(
            {
                "ok": False,
                "job_id": job_id,
                "error_code": f"http_{code}",
                "error": err_msg,
            },
            ticket_secret=ticket_secret,
            stream=stream,
        )
        return 1

    job = resp.get("job") if isinstance(resp.get("job"), Mapping) else {}
    raw_cancel = (
        resp.get("cancellation")
        if isinstance(resp.get("cancellation"), Mapping)
        else (
            (job.get("usage") or {}).get("cancellation")
            if isinstance(job.get("usage"), Mapping)
            and isinstance((job.get("usage") or {}).get("cancellation"), Mapping)
            else {}
        )
    )
    req_ids = [
        str(r).strip()
        for r in (
            raw_cancel.get("submitted_request_ids")
            or job.get("provider_request_ids")
            or []
        )
        if str(r).strip()
    ]
    submitted_cnt = int(
        raw_cancel.get("submitted_segments_count")
        if raw_cancel.get("submitted_segments_count") is not None
        else len(req_ids)
    )
    unsubmitted_cnt = int(
        raw_cancel.get("unsubmitted_cancelled_count")
        if raw_cancel.get("unsubmitted_cancelled_count") is not None
        else max(
            0,
            int(job.get("segment_count") or 0)
            - int(job.get("completed_segments") or 0)
            - submitted_cnt,
        )
    )
    notice = str(raw_cancel.get("notice") or PROVIDER_CANCEL_UNVERIFIED_NOTICE)
    _emit_json(
        {
            "ok": True,
            "event": "cancelled",
            "job_id": job_id,
            "status": str(job.get("status") or "cancelled"),
            "stage": str(job.get("stage") or "cancelled"),
            "submitted_segments_count": submitted_cnt,
            "submitted_request_ids": req_ids,
            "unsubmitted_cancelled_count": unsubmitted_cnt,
            "provider_cancel_verified": False,
            "billing_verified": False,
            "notice": notice,
        },
        ticket_secret=ticket_secret,
        stream=stream,
    )
    return 0


def cmd_retry(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    output_dir: Path | None = None,
    wait: bool = False,
    poll_interval_s: float = 2.0,
    max_wait_s: float = 0.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    ticket_secret: str,
    stream: Any = None,
) -> int:
    job_id = str(ticket_data["job_id"])
    code, resp = client.request_json("POST", f"/{job_id}/retry", body={})
    if code >= 400 or resp.get("ok") is False:
        err_msg = _extract_error_message(resp, code)
        _emit_json(
            {
                "ok": False,
                "job_id": job_id,
                "error_code": f"http_{code}",
                "error": err_msg,
            },
            ticket_secret=ticket_secret,
            stream=stream,
        )
        return 1

    job = resp.get("job") if isinstance(resp.get("job"), Mapping) else {}
    if not wait or output_dir is None:
        summary = _format_job_progress_summary(job, event="retried")
        summary["ok"] = True
        summary["attempt"] = int(
            (job.get("usage") or {}).get("attempt") or job.get("attempt") or 2
        )
        _emit_json(summary, ticket_secret=ticket_secret, stream=stream)
        return 0

    return _poll_until_done_and_save(
        client,
        ticket_data,
        initial_job=job,
        output_dir=output_dir,
        poll_interval_s=poll_interval_s,
        max_wait_s=max_wait_s,
        sleep_fn=sleep_fn,
        ticket_secret=ticket_secret,
        stream=stream,
    )


def cmd_run(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    output_dir: Path,
    workspace_root: str | Path | None = None,
    open_file_fn: Callable[[Path], Any] | None = None,
    language: str | None = None,
    hotwords: str | Sequence[str] | None = None,
    poll_interval_s: float = 2.0,
    max_wait_s: float = 0.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    ticket_secret: str,
    stream: Any = None,
) -> int:
    """Upload audio (if `awaiting_upload`), submit, poll progress, and atomically write raw JSON + merged transcript on completion."""
    job_id = str(ticket_data["job_id"])
    _prepare_safe_output_dir(output_dir, job_id=job_id)

    # Step 1: Check current status first so an already-submitted or completed job is not re-submitted
    code_st, resp_st = client.request_json("GET", f"/{job_id}")
    if code_st >= 400 or resp_st.get("ok") is False:
        err_msg = _extract_error_message(resp_st, code_st)
        _emit_json(
            {
                "ok": False,
                "job_id": job_id,
                "error_code": f"http_{code_st}",
                "error": err_msg,
            },
            ticket_secret=ticket_secret,
            stream=stream,
        )
        return 1

    job: Mapping[str, Any] = (
        resp_st.get("job") if isinstance(resp_st.get("job"), Mapping) else resp_st
    )
    cur_status = str(job.get("status") or "awaiting_upload")

    # Step 2: Upload source audio in 16 MiB parts and submit if still in `awaiting_upload`
    if cur_status == "awaiting_upload":
        upload_source_audio_if_needed(
            client,
            ticket_data,
            workspace_root=workspace_root,
            sleep_fn=sleep_fn,
            open_file_fn=open_file_fn,
            ticket_secret=ticket_secret,
            stream=stream,
        )

        submit_body: dict[str, Any] = {
            "submit": True,
            "job_id": job_id,
        }
        for key in ("workspace_id", "source_object_key", "source_size_bytes", "source_sha256"):
            if ticket_data.get(key) is not None:
                submit_body[key] = ticket_data[key]
        eff_lang = language or ticket_data.get("language")
        if eff_lang:
            submit_body["language"] = str(eff_lang).strip()
        eff_hw = _parse_hotwords_arg(hotwords) or _parse_hotwords_arg(
            ticket_data.get("hotwords")
        )
        if eff_hw is not None:
            submit_body["hotwords"] = eff_hw

        code_sub, resp_sub = client.request_json("POST", "", body=submit_body)
        if code_sub >= 400 or resp_sub.get("ok") is False:
            err_msg = _extract_error_message(resp_sub, code_sub)
            _emit_json(
                {
                    "ok": False,
                    "job_id": job_id,
                    "error_code": f"http_{code_sub}",
                    "error": err_msg,
                },
                ticket_secret=ticket_secret,
                stream=stream,
            )
            return 1
        job = (
            resp_sub.get("job")
            if isinstance(resp_sub.get("job"), Mapping)
            else resp_sub
        )
        _emit_json(
            _format_job_progress_summary(job, event="submitted"),
            ticket_secret=ticket_secret,
            stream=stream,
        )

    return _poll_until_done_and_save(
        client,
        ticket_data,
        initial_job=job,
        output_dir=output_dir,
        poll_interval_s=poll_interval_s,
        max_wait_s=max_wait_s,
        sleep_fn=sleep_fn,
        ticket_secret=ticket_secret,
        stream=stream,
    )


def _poll_until_done_and_save(
    client: BackendTranscriptionClient,
    ticket_data: Mapping[str, Any],
    *,
    initial_job: Mapping[str, Any],
    output_dir: Path,
    poll_interval_s: float,
    max_wait_s: float,
    sleep_fn: Callable[[float], None],
    ticket_secret: str,
    stream: Any = None,
) -> int:
    job_id = str(ticket_data["job_id"])
    job: Mapping[str, Any] = initial_job
    last_progress_key: tuple[str, str, int, int] | None = None
    start_mono = time.monotonic()
    poll_wait = max(0.0, float(poll_interval_s))
    consecutive_poll_errors = 0

    try:
        while True:
            cur_status = str(job.get("status") or "queued")
            cur_stage = str(job.get("stage") or cur_status)
            seg_cnt = int(job.get("segment_count") or 0)
            comp_cnt = int(job.get("completed_segments") or 0)
            prog_key = (cur_status, cur_stage, comp_cnt, seg_cnt)

            if prog_key != last_progress_key:
                last_progress_key = prog_key
                _emit_json(
                    _format_job_progress_summary(job, event="progress"),
                    ticket_secret=ticket_secret,
                    stream=stream,
                )

            if cur_status == "succeeded":
                return cmd_result(
                    client,
                    ticket_data,
                    output_dir=output_dir,
                    ticket_secret=ticket_secret,
                    stream=stream,
                )

            if cur_status in ("failed", "cancelled", "indeterminate"):
                err_cat = str(job.get("error_category") or cur_status)
                err_msg = str(
                    job.get("error_message")
                    or f"Transcription job ended in status '{cur_status}'"
                )[:240]
                _emit_json(
                    {
                        "ok": False,
                        "event": "terminal",
                        "job_id": job_id,
                        "status": cur_status,
                        "stage": cur_stage,
                        "segment_count": seg_cnt,
                        "completed_segments": comp_cnt,
                        "error_category": err_cat,
                        "error": err_msg,
                    },
                    ticket_secret=ticket_secret,
                    stream=stream,
                )
                return 1

            if max_wait_s > 0.0 and (time.monotonic() - start_mono) >= max_wait_s:
                raise _PollingInterrupted("max_wait_s reached")

            sleep_fn(poll_wait)

            try:
                code_p, resp_p = client.request_json("GET", f"/{job_id}")
            except DaltonAsrCliError as exc:
                if exc.error_code == "backend_unreachable":
                    consecutive_poll_errors += 1
                    if consecutive_poll_errors > client.max_transient_retries:
                        raise
                    backoff = client._compute_backoff(consecutive_poll_errors)
                    _emit_json(
                        {
                            "event": "poll_retry",
                            "job_id": job_id,
                            "reason": exc.message,
                            "attempt": consecutive_poll_errors,
                            "backoff_s": backoff,
                        },
                        ticket_secret=ticket_secret,
                        stream=stream,
                    )
                    sleep_fn(backoff)
                    continue
                raise

            if code_p >= 400 or resp_p.get("ok") is False:
                err_msg = _extract_error_message(resp_p, code_p)
                _emit_json(
                    {
                        "ok": False,
                        "job_id": job_id,
                        "error_code": f"http_{code_p}",
                        "error": err_msg,
                    },
                    ticket_secret=ticket_secret,
                    stream=stream,
                )
                return 1

            consecutive_poll_errors = 0
            bridge_sync = str(resp_p.get("bridge_sync_status") or "ok")
            if isinstance(resp_p.get("job"), Mapping):
                job = resp_p["job"]
            if bridge_sync in ("unreachable", "degraded"):
                _emit_json(
                    {
                        "event": "bridge_reconnecting",
                        "job_id": job_id,
                        "status": str(job.get("status") or ""),
                        "stage": str(job.get("stage") or ""),
                        "bridge_sync_status": bridge_sync,
                    },
                    ticket_secret=ticket_secret,
                    stream=stream,
                )

    except (KeyboardInterrupt, _PollingInterrupted):
        # Stopping the polling command (e.g. 1-hour post-turn background command limit)
        # NEVER cancels the transcription job; job_id stays active on the backend.
        _emit_json(
            {
                "ok": False,
                "interrupted": True,
                "job_id": job_id,
                "status": str(job.get("status") or "transcribing"),
                "stage": str(job.get("stage") or "transcribing"),
                "message": (
                    "Polling command stopped without cancelling the transcription job. "
                    "Use 'status' or 'result' with --ticket-file to retrieve it."
                ),
            },
            ticket_secret=ticket_secret,
            stream=stream,
        )
        return 130


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dalton_asr.py",
        description="Sandbox CLI for Dalton Seed-ASR transcription jobs (run / status / result / retry / cancel).",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def _add_common_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--ticket-file",
            required=True,
            help="Path to the 0600 JSON ticket file issued by prepare_transcription.",
        )
        sub.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            help="Per-request HTTP timeout in seconds (default: 30).",
        )

    p_run = subparsers.add_parser(
        "run",
        help="Submit transcription job (if needed), poll progress, and save raw JSON + merged transcript to --output-dir.",
    )
    _add_common_args(p_run)
    p_run.add_argument(
        "--output-dir",
        required=True,
        help="Directory where raw_response.json and transcript.txt will be written.",
    )
    p_run.add_argument(
        "--language",
        default=None,
        help="Optional BCP-47 language code override (e.g. zh-CN, en-US).",
    )
    p_run.add_argument(
        "--hotwords",
        default=None,
        help="Optional comma-separated hotwords override.",
    )
    p_run.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between status polls (default: 2.0).",
    )
    p_run.add_argument(
        "--max-wait",
        type=float,
        default=0.0,
        help="Optional max polling wait in seconds (0 = until job finishes).",
    )

    p_status = subparsers.add_parser(
        "status",
        help="Query current transcription job status.",
    )
    _add_common_args(p_status)

    p_result = subparsers.add_parser(
        "result",
        help="Fetch completed transcription result and save raw_response.json + transcript.txt to --output-dir.",
    )
    _add_common_args(p_result)
    p_result.add_argument(
        "--output-dir",
        required=True,
        help="Directory where raw_response.json and transcript.txt will be written.",
    )

    p_retry = subparsers.add_parser(
        "retry",
        help="Retry a failed or indeterminate transcription job.",
    )
    _add_common_args(p_retry)
    p_retry.add_argument(
        "--output-dir",
        default=None,
        help="Optional directory to save outputs if --wait is also passed.",
    )
    p_retry.add_argument(
        "--wait",
        action="store_true",
        help="Poll until completion after triggering retry (requires --output-dir).",
    )
    p_retry.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between status polls when --wait is enabled.",
    )

    p_cancel = subparsers.add_parser(
        "cancel",
        help="Cancel unsubmitted segments of a transcription job.",
    )
    _add_common_args(p_cancel)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    workspace_root: str | Path | None = None,
    open_file_fn: Callable[[Path], Any] | None = None,
    urlopen_fn: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    initial_backoff_s: float = 0.5,
    max_backoff_s: float = 8.0,
    max_transient_retries: int = 8,
    stream: Any = None,
) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    ticket_secret_for_redact: str | None = None
    job_id_for_interrupt: str | None = None
    subcommand_for_interrupt: str | None = None
    out_stream = stream if stream is not None else sys.stdout

    def _sigterm_handler(_signum: int, _frame: Any) -> None:
        raise _PollingInterrupted("SIGTERM received")

    prev_sigterm = None
    try:
        try:
            prev_sigterm = signal.signal(signal.SIGTERM, _sigterm_handler)
        except ValueError:
            # signal.signal only works in main thread
            prev_sigterm = None

        verify_no_credential_in_argv_or_env(raw_argv, environ=environ)
        parser = _build_arg_parser()
        args = parser.parse_args(raw_argv)
        subcommand_for_interrupt = str(args.subcommand or "")

        ticket_data = load_and_verify_ticket_file(args.ticket_file)
        ticket_secret_for_redact = str(ticket_data["ticket"])
        job_id = str(ticket_data["job_id"])
        job_id_for_interrupt = job_id
        base_url = resolve_backend_base_url(ticket_data, environ=environ)
        resolved_ws_root = resolve_workspace_root_for_ticket(
            args.ticket_file,
            ticket_data,
            environ=environ,
            explicit_root=workspace_root,
        )

        def _on_retry(attempt_idx: int, reason: str, backoff: float) -> None:
            _emit_json(
                {
                    "event": "poll_retry",
                    "job_id": job_id,
                    "attempt": attempt_idx,
                    "reason": reason,
                    "backoff_s": backoff,
                },
                ticket_secret=ticket_secret_for_redact,
                stream=out_stream,
            )

        client = BackendTranscriptionClient(
            base_url=base_url,
            ticket=ticket_secret_for_redact,
            job_id=job_id,
            request_timeout_s=float(getattr(args, "timeout", 30.0)),
            initial_backoff_s=initial_backoff_s,
            max_backoff_s=max_backoff_s,
            max_transient_retries=max_transient_retries,
            sleep_fn=sleep_fn,
            urlopen_fn=urlopen_fn,
            on_retry_fn=_on_retry,
        )

        cmd = args.subcommand
        if cmd == "run":
            return cmd_run(
                client,
                ticket_data,
                output_dir=Path(args.output_dir),
                workspace_root=resolved_ws_root,
                open_file_fn=open_file_fn,
                language=args.language,
                hotwords=args.hotwords,
                poll_interval_s=float(args.poll_interval),
                max_wait_s=float(args.max_wait),
                sleep_fn=sleep_fn,
                ticket_secret=ticket_secret_for_redact,
                stream=out_stream,
            )
        if cmd == "status":
            return cmd_status(
                client,
                ticket_data,
                ticket_secret=ticket_secret_for_redact,
                stream=out_stream,
            )
        if cmd == "result":
            return cmd_result(
                client,
                ticket_data,
                output_dir=Path(args.output_dir),
                ticket_secret=ticket_secret_for_redact,
                stream=out_stream,
            )
        if cmd == "retry":
            out_dir = Path(args.output_dir) if args.output_dir else None
            return cmd_retry(
                client,
                ticket_data,
                output_dir=out_dir,
                wait=bool(args.wait),
                poll_interval_s=float(args.poll_interval),
                sleep_fn=sleep_fn,
                ticket_secret=ticket_secret_for_redact,
                stream=out_stream,
            )
        if cmd == "cancel":
            return cmd_cancel(
                client,
                ticket_data,
                ticket_secret=ticket_secret_for_redact,
                stream=out_stream,
            )

        raise DaltonAsrCliError("unknown_subcommand", f"Unknown subcommand: {cmd}", exit_code=2)

    except (KeyboardInterrupt, _PollingInterrupted):
        _emit_json(
            {
                "ok": False,
                "interrupted": True,
                "job_id": job_id_for_interrupt,
                "subcommand": subcommand_for_interrupt,
                "message": (
                    "Command stopped by signal without cancelling the transcription job. "
                    "Use 'status' or 'result' with --ticket-file to retrieve it."
                ),
            },
            ticket_secret=ticket_secret_for_redact,
            stream=out_stream,
        )
        return 130
    except DaltonAsrCliError as exc:
        _emit_json(
            {
                "ok": False,
                "job_id": exc.job_id or job_id_for_interrupt,
                "error_code": exc.error_code,
                "error": exc.message,
            },
            ticket_secret=ticket_secret_for_redact,
            stream=out_stream,
        )
        return exc.exit_code
    finally:
        if prev_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, prev_sigterm)
            except ValueError:
                pass


__all__ = [
    "BackendTranscriptionClient",
    "DaltonAsrCliError",
    "FORBIDDEN_CREDENTIAL_CLI_FLAGS",
    "FORBIDDEN_CREDENTIAL_ENV_VARS",
    "MAX_UPLOAD_PARTS",
    "PROVIDER_CANCEL_UNVERIFIED_NOTICE",
    "TRANSCRIPTIONS_API_PREFIX",
    "UPLOAD_PART_BYTES",
    "UPLOAD_PART_MAX_RETRIES",
    "build_transcription_endpoint_url",
    "cmd_cancel",
    "cmd_result",
    "cmd_retry",
    "cmd_run",
    "cmd_status",
    "load_and_verify_ticket_file",
    "main",
    "redact_sensitive_text",
    "resolve_and_verify_source_audio_path",
    "resolve_backend_base_url",
    "resolve_workspace_root_for_ticket",
    "upload_source_audio_if_needed",
    "verify_no_credential_in_argv_or_env",
    "write_transcription_outputs",
]


if __name__ == "__main__":
    raise SystemExit(main())
