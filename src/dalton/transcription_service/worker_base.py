"""Shared open-source ASR worker primitives (storage download, ffprobe/ffmpeg, merging, protocol).

Self-contained under ``src/dalton/transcription_service/`` so Standalone Core
builds never depend on ``scripts/dalton/asr_jobs.py`` or proprietary Seed-ASR
code. Keeps ``boto3`` strictly lazy-imported and keeps ``SourceObjectStore.resolve_full_key``
byte-identical to ``src/dalton/transcription_storage.py`` and ``scripts/dalton/asr_jobs.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("dalton-transcription")

SPLIT_THRESHOLD_SECONDS: float = 1800.0
SEGMENT_TIME_SECONDS: int = 600
DEFAULT_STREAM_CHUNK_BYTES: int = 1024 * 1024  # 1 MiB
DEFAULT_DISK_HEADROOM_BYTES: int = 64 * 1024 * 1024  # 64 MiB
DEFAULT_FFPROBE_TIMEOUT_S: float = 60.0
DEFAULT_FFMPEG_TIMEOUT_S: float = 600.0

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
    ".mp4": "mp4",
    ".webm": "webm",
}

_URL_QUERY_REDACT_RE = re.compile(
    r"(?i)(https?://[^\s\"'<>?]+)\?(?=[^\s\"'<>]*(?:x-amz-|x-tos-|awsaccesskeyid=|signature=))[^\s\"'<>]*"
)
_HEADER_SECRET_REDACT_RE = re.compile(
    r"(?i)(\b(?:x-api-access-key|x-api-app-key|authorization|x-amz-credential|x-amz-signature|x-tos-credential|x-tos-signature|x-dalton-transcription-token)\s*[:=]\s*)([^\s,;\"'&]+)"
)
_BEARER_REDACT_RE = re.compile(r"(?i)(\bBearer\s+)([^\s,;\"'&]+)")

_SECRET_ENV_KEYS: tuple[str, ...] = (
    "DALTON_TRANSCRIPTION_TOKEN",
    "DALTON_WHISPER_API_KEY",
    "OPENAI_API_KEY",
    "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
    "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "SEED_ASR_APP_ID",
    "SEED_ASR_ACCESS_KEY",
    "VOLC_TOS_AK",
    "VOLC_TOS_SK",
)

# Global transcode concurrency = 1 across the transcription service process
_GLOBAL_TRANSCODE_SEMAPHORE = asyncio.Semaphore(1)


def redact_asr_secrets(
    text: str | None,
    *,
    extra_secrets: tuple[str, ...] | list[str] = (),
    environ: Mapping[str, str] | None = None,
) -> str:
    """Redact API keys, storage credentials, and presigned query strings from ``text``."""
    if not text:
        return ""
    out = str(text)
    out = _URL_QUERY_REDACT_RE.sub(r"\1?[REDACTED]", out)
    out = _HEADER_SECRET_REDACT_RE.sub(r"\1[REDACTED]", out)
    out = _BEARER_REDACT_RE.sub(r"\1[REDACTED]", out)
    env = os.environ if environ is None else environ
    secrets_to_scrub: list[str] = list(extra_secrets)
    for key in _SECRET_ENV_KEYS:
        val = str(env.get(key) or "").strip()
        if val:
            secrets_to_scrub.append(val)
    for secret in sorted({s for s in secrets_to_scrub if len(s) >= 4}, key=len, reverse=True):
        out = out.replace(secret, "[REDACTED]")
    return out


def detect_format(filepath_or_format: str | Path) -> str:
    """Detect audio format string from a file path or format name."""
    raw = str(filepath_or_format or "").strip().lower()
    if not raw:
        return "mp3"
    if f".{raw}" in _FORMAT_MAP:
        return _FORMAT_MAP[f".{raw}"]
    ext = Path(raw).suffix.lower()
    return _FORMAT_MAP.get(ext, "mp3")


def normalize_asr_language(language: str | None) -> str | None:
    """Normalize optional ASR language hint (``en``/``en-us`` -> ``en-US``)."""
    if language is None:
        return None
    cleaned = str(language).strip()
    if not cleaned:
        return None
    lower = cleaned.lower().replace("_", "-")
    if lower in ("en", "en-us", "english", "eng"):
        return "en-US"
    return cleaned


def normalize_whisper_language(language: str | None) -> str | None:
    """Convert optional BCP-47/display language hint into ISO-639-1 code for Whisper API."""
    norm = normalize_asr_language(language)
    if not norm:
        return None
    lower = norm.lower().replace("_", "-")
    if lower in ("zh", "zh-cn", "zh-tw", "zh-hk", "zh-hans", "zh-hant", "chinese"):
        return "zh"
    if lower.startswith("en-"):
        return "en"
    primary = lower.split("-", 1)[0]
    if len(primary) == 2 and primary.isalpha():
        return primary
    return None


def build_asr_subprocess_env(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a strict allowlisted environment for child processes (``ffmpeg`` / ``ffprobe``)."""
    src = base_env if base_env is not None else os.environ
    env: dict[str, str] = {}
    for key in ASR_SUBPROCESS_ENV_ALLOWLIST:
        val = src.get(key)
        if isinstance(val, str) and val.strip():
            env[key] = val
    if "PATH" not in env:
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    return env


class AsrWorkerError(Exception):
    """Structured error raised during local ASR worker stages."""

    def __init__(self, category: str, message: str) -> None:
        sanitized = redact_asr_secrets(message)
        super().__init__(sanitized)
        self.category = str(category)
        self.message = sanitized


@runtime_checkable
class AsrWorkerProtocol(Protocol):
    """Contract implemented by all transcription workers (Whisper, Fake, private Seed-ASR)."""

    async def run_job(
        self,
        job_state: dict[str, Any],
        *,
        on_progress: Callable[[dict[str, Any]], Awaitable[bool | None]],
    ) -> dict[str, Any]: ...

    async def cleanup_job_resources(
        self,
        job_id: str,
        segments: list[dict[str, Any]] | None = None,
    ) -> None: ...


def compute_file_sha256(
    path: Path, *, chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES
) -> tuple[str, int]:
    """Compute SHA-256 hex digest and byte length of ``path`` using fixed-size streaming reads."""
    hasher = hashlib.sha256()
    total = 0
    step = max(4096, int(chunk_size))
    with path.open("rb") as fh:
        while True:
            buf = fh.read(step)
            if not buf:
                break
            hasher.update(buf)
            total += len(buf)
    return hasher.hexdigest().lower(), total


def check_disk_admission(
    work_dir: Path,
    source_size_bytes: int,
    *,
    headroom_bytes: int = DEFAULT_DISK_HEADROOM_BYTES,
    disk_usage_fn: Callable[[Path | str], Any] = shutil.disk_usage,
) -> int:
    """Verify ``work_dir`` has at least ``source_size_bytes * 2 + headroom_bytes`` free space."""
    work_dir.mkdir(parents=True, exist_ok=True)
    required_bytes = int(source_size_bytes) * 2 + max(0, int(headroom_bytes))
    usage = disk_usage_fn(work_dir)
    free_bytes = int(getattr(usage, "free", 0))
    if free_bytes < required_bytes:
        raise AsrWorkerError(
            "insufficient_disk_space",
            f"Insufficient disk space in transcription temp dir: free={free_bytes} bytes, "
            f"required={required_bytes} bytes (2x source + headroom)",
        )
    return free_bytes


class SourceObjectStore:
    """Streaming downloader for source audio objects from the Dalton S3-compatible store."""

    def __init__(
        self,
        *,
        s3_client: Any | None = None,
        bucket: str | None = None,
        key_prefix: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = environ if environ is not None else os.environ
        self._s3_client = s3_client
        self.endpoint_url = (
            str(
                env.get("DALTON_ASR_STORAGE_ENDPOINT_URL")
                or env.get("STORAGE_ENDPOINT_URL")
                or env.get("S3_ENDPOINT_URL")
                or "http://127.0.0.1:9000"
            )
            .strip()
            .rstrip("/")
        )
        self.bucket = str(
            bucket
            if bucket is not None
            else (
                env.get("DALTON_ASR_STORAGE_BUCKET_NAME")
                or env.get("STORAGE_BUCKET_NAME")
                or env.get("S3_BUCKET_NAME")
                or ""
            )
        ).strip()
        self.access_key = str(
            env.get("DALTON_ASR_STORAGE_ACCESS_KEY_ID")
            or env.get("STORAGE_ACCESS_KEY_ID")
            or env.get("MINIO_ROOT_USER")
            or env.get("AWS_ACCESS_KEY_ID")
            or ""
        ).strip()
        self.secret_key = str(
            env.get("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY")
            or env.get("STORAGE_SECRET_ACCESS_KEY")
            or env.get("MINIO_ROOT_PASSWORD")
            or env.get("AWS_SECRET_ACCESS_KEY")
            or ""
        ).strip()
        self.region = (
            str(
                env.get("DALTON_ASR_STORAGE_REGION")
                or env.get("STORAGE_REGION")
                or env.get("S3_REGION")
                or "us-east-1"
            ).strip()
            or "us-east-1"
        )
        self.addressing_style = (
            str(
                env.get("DALTON_ASR_STORAGE_ADDRESSING_STYLE")
                or env.get("STORAGE_ADDRESSING_STYLE")
                or "path"
            )
            .strip()
            .lower()
            or "path"
        )
        raw_prefix = str(
            key_prefix
            if key_prefix is not None
            else (
                env.get("DALTON_ASR_STORAGE_KEY_PREFIX")
                or env.get("STORAGE_KEY_PREFIX")
                or ""
            )
        ).strip().strip("/")
        self.key_prefix = f"{raw_prefix}/" if raw_prefix else ""

    def _get_s3_client(self) -> Any:
        if self._s3_client is not None:
            return self._s3_client
        if not self.bucket or not self.access_key or not self.secret_key:
            raise AsrWorkerError(
                "storage_not_configured",
                "Transcription source object storage credentials/bucket are not configured",
            )
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise AsrWorkerError(
                "asr_boto3_missing",
                "boto3 is not installed in the transcription service Python environment",
            ) from exc
        self._s3_client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": self.addressing_style},
                connect_timeout=10,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
                response_checksum_validation="when_required",
            ),
        )
        return self._s3_client

    def resolve_full_key(self, object_key: str) -> str:
        """Byte-identical to ``src/dalton/transcription_storage.py:resolve_full_key``."""
        cleaned = str(object_key or "").strip().lstrip("/")
        if self.key_prefix and not cleaned.startswith(self.key_prefix):
            return f"{self.key_prefix}{cleaned}"
        return cleaned

    @contextmanager
    def open_object_chunks(
        self,
        source_object_key: str,
        *,
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
    ) -> Iterator[Iterator[bytes]]:
        client = self._get_s3_client()
        full_key = self.resolve_full_key(source_object_key)
        step = max(4096, int(chunk_size))
        resp = client.get_object(Bucket=self.bucket, Key=full_key)
        body = resp.get("Body") if isinstance(resp, dict) else None
        if body is None or not hasattr(body, "read"):
            raise AsrWorkerError(
                "source_download_failed",
                f"Object storage get_object returned no readable stream for {source_object_key}",
            )

        def _chunk_iter() -> Iterator[bytes]:
            while True:
                data = body.read(step)
                if not data:
                    break
                yield bytes(data)

        try:
            yield _chunk_iter()
        finally:
            close_fn = getattr(body, "close", None)
            if callable(close_fn):
                try:
                    close_fn()
                except Exception:
                    pass

    def download_to_path(
        self,
        source_object_key: str,
        dest_path: Path,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
        deadline_monotonic: float | None = None,
    ) -> tuple[int, str]:
        """Stream ``source_object_key`` into ``dest_path``, verifying size, SHA-256, and deadline."""
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise AsrWorkerError(
                "asr_job_deadline_exceeded",
                "Overall job deadline exceeded before source audio download started",
            )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        written = 0
        expected_size = int(expected_size_bytes)
        expected_hash = str(expected_sha256 or "").strip().lower()

        try:
            with (
                self.open_object_chunks(source_object_key, chunk_size=chunk_size) as chunks,
                dest_path.open("wb") as out_fh,
            ):
                for chunk in chunks:
                    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                        raise AsrWorkerError(
                            "asr_job_deadline_exceeded",
                            "Overall job deadline exceeded during source audio download",
                        )
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > expected_size:
                        raise AsrWorkerError(
                            "source_size_mismatch",
                            f"Downloaded stream exceeded expected size ({written} > {expected_size} bytes)",
                        )
                    hasher.update(chunk)
                    out_fh.write(chunk)
                out_fh.flush()
        except AsrWorkerError:
            try:
                if dest_path.exists():
                    dest_path.unlink()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                if dest_path.exists():
                    dest_path.unlink()
            except Exception:
                pass
            raise AsrWorkerError(
                "source_download_failed",
                f"Streaming download failed ({type(exc).__name__}): {redact_asr_secrets(str(exc))}",
            ) from exc

        actual_hash = hasher.hexdigest().lower()
        if written != expected_size:
            try:
                dest_path.unlink()
            except Exception:
                pass
            raise AsrWorkerError(
                "source_size_mismatch",
                f"Downloaded size mismatch: got {written} bytes, expected {expected_size} bytes",
            )
        if expected_hash and actual_hash != expected_hash:
            try:
                dest_path.unlink()
            except Exception:
                pass
            raise AsrWorkerError(
                "source_integrity_mismatch",
                f"Downloaded SHA-256 mismatch: got {actual_hash}, expected {expected_hash}",
            )
        return written, actual_hash


def resolve_binary(preferred: str | Path | None, default_name: str) -> str:
    if preferred:
        cand = str(preferred).strip()
        if cand:
            return cand
    hb = Path("/opt/homebrew/bin") / default_name
    if hb.is_file() and os.access(hb, os.X_OK):
        return str(hb)
    found = shutil.which(default_name)
    return found or default_name


def is_binary_available(default_name: str, preferred: str | Path | None = None) -> bool:
    resolved = resolve_binary(preferred, default_name)
    if os.path.sep in resolved:
        return Path(resolved).is_file() and os.access(resolved, os.X_OK)
    return shutil.which(resolved) is not None


async def probe_audio_duration(
    audio_path: Path,
    *,
    ffprobe_bin: str | Path | None = None,
    timeout_s: float = DEFAULT_FFPROBE_TIMEOUT_S,
    base_env: Mapping[str, str] | None = None,
) -> float:
    """Measure audio duration in seconds via ``ffprobe`` with allowlisted env."""
    if not audio_path.is_file():
        raise AsrWorkerError("ffprobe_input_missing", f"Audio file not found: {audio_path.name}")
    bin_path = resolve_binary(ffprobe_bin, "ffprobe")
    cmd = [
        bin_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(audio_path),
    ]
    subproc_env = build_asr_subprocess_env(base_env)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=subproc_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=max(0.001, float(timeout_s))
        )
    except TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise AsrWorkerError(
            "ffprobe_timeout", f"ffprobe timed out after {timeout_s}s on {audio_path.name}"
        ) from exc
    except BaseException:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise

    if proc.returncode != 0:
        err_snip = redact_asr_secrets(
            stderr_bytes.decode("utf-8", errors="replace").strip()[:200]
        )
        raise AsrWorkerError(
            "ffprobe_failed",
            f"ffprobe exited with code {proc.returncode}: {err_snip or 'unknown error'}",
        )

    try:
        parsed = json.loads(stdout_bytes.decode("utf-8", errors="replace"))
        dur_raw = (parsed.get("format") or {}).get("duration")
        duration = float(dur_raw)
    except Exception as exc:
        raise AsrWorkerError(
            "ffprobe_invalid_output",
            "ffprobe output did not contain a valid numeric format.duration",
        ) from exc

    if duration <= 0.0:
        raise AsrWorkerError(
            "ffprobe_invalid_duration", f"ffprobe reported non-positive duration ({duration}s)"
        )
    return duration


def should_split_audio(
    duration_seconds: float,
    *,
    threshold_seconds: float = SPLIT_THRESHOLD_SECONDS,
) -> bool:
    """Return True strictly when ``duration_seconds > threshold_seconds`` (1800s boundary)."""
    return float(duration_seconds) > float(threshold_seconds)


def compute_segment_ranges(
    duration_seconds: float,
    *,
    threshold_seconds: float = SPLIT_THRESHOLD_SECONDS,
    segment_time_seconds: int = SEGMENT_TIME_SECONDS,
) -> list[tuple[float, float]]:
    dur = round(float(duration_seconds), 3)
    if not should_split_audio(dur, threshold_seconds=threshold_seconds):
        return [(0.0, dur)]
    step = float(max(1, int(segment_time_seconds)))
    ranges: list[tuple[float, float]] = []
    start = 0.0
    while start < dur:
        end = round(min(dur, start + step), 3)
        if end > start:
            ranges.append((round(start, 3), end))
        start = round(start + step, 3)
    return ranges


@dataclass
class PreparedSegmentFile:
    """Local segment file prepared for ASR provider submission."""

    index: int
    path: Path
    start_s: float
    end_s: float
    sha256: str
    size_bytes: int
    codec_params: dict[str, Any]


async def split_audio_to_mp3_segments(
    source_path: Path,
    chunks_dir: Path,
    *,
    total_duration_s: float,
    segment_time_s: int = SEGMENT_TIME_SECONDS,
    ffmpeg_bin: str | Path | None = None,
    timeout_s: float = DEFAULT_FFMPEG_TIMEOUT_S,
    transcode_semaphore: asyncio.Semaphore | None = None,
    base_env: Mapping[str, str] | None = None,
) -> list[PreparedSegmentFile]:
    """Split ``source_path`` into MP3 segments via ``ffmpeg`` with transcode concurrency = 1."""
    if not source_path.is_file():
        raise AsrWorkerError(
            "ffmpeg_input_missing", f"Source audio not found: {source_path.name}"
        )
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for old_chunk in chunks_dir.glob("chunk_*.mp3"):
        try:
            old_chunk.unlink()
        except Exception:
            pass

    bin_path = resolve_binary(ffmpeg_bin, "ffmpeg")
    seg_time = max(1, int(segment_time_s))
    pattern_path = chunks_dir / "chunk_%02d.mp3"
    cmd = [
        bin_path,
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-f",
        "segment",
        "-segment_time",
        str(seg_time),
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(pattern_path),
    ]
    subproc_env = build_asr_subprocess_env(base_env)
    sem = transcode_semaphore or _GLOBAL_TRANSCODE_SEMAPHORE
    eff_timeout = max(0.001, float(timeout_s))
    step_deadline = time.monotonic() + eff_timeout

    try:
        await asyncio.wait_for(sem.acquire(), timeout=eff_timeout)
    except TimeoutError as exc:
        raise AsrWorkerError(
            "ffmpeg_timeout",
            f"ffmpeg segmentation timed out waiting for transcode lock after {timeout_s}s",
        ) from exc

    try:
        rem_exec = max(0.001, step_deadline - time.monotonic())
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=subproc_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=rem_exec)
        except TimeoutError as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise AsrWorkerError(
                "ffmpeg_timeout", f"ffmpeg segmentation timed out after {timeout_s}s"
            ) from exc
        except BaseException:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
    finally:
        sem.release()

    if proc.returncode != 0:
        err_snip = redact_asr_secrets(
            stderr_b.decode("utf-8", errors="replace").strip()[-240:]
        )
        raise AsrWorkerError(
            "ffmpeg_split_failed",
            f"ffmpeg segmentation exited with code {proc.returncode}: {err_snip or 'unknown error'}",
        )

    def _chunk_sort_key(p: Path) -> tuple[int, str]:
        suffix_num = p.stem.rsplit("_", 1)[-1]
        return (int(suffix_num) if suffix_num.isdigit() else 999999, p.name)

    chunk_files = sorted(chunks_dir.glob("chunk_*.mp3"), key=_chunk_sort_key)
    if not chunk_files:
        raise AsrWorkerError(
            "ffmpeg_no_chunks", "ffmpeg segmentation produced no chunk_*.mp3 files"
        )

    codec_params = {
        "format": "mp3",
        "codec": "libmp3lame",
        "q_a": "2",
        "segment_time": seg_time,
    }
    prepared: list[PreparedSegmentFile] = []
    num_chunks = len(chunk_files)
    total_dur = round(float(total_duration_s), 3)
    for idx, cpath in enumerate(chunk_files):
        sha_hex, fsize = await asyncio.to_thread(compute_file_sha256, cpath)
        start_s = round(idx * float(seg_time), 3)
        if idx == num_chunks - 1:
            end_s = max(round(start_s + 0.001, 3), total_dur)
        else:
            end_s = round(min(total_dur, (idx + 1) * float(seg_time)), 3)
        prepared.append(
            PreparedSegmentFile(
                index=idx,
                path=cpath,
                start_s=start_s,
                end_s=end_s,
                sha256=sha_hex,
                size_bytes=fsize,
                codec_params=dict(codec_params),
            )
        )
    return prepared


def _extract_utterance_speaker(utt: Mapping[str, Any]) -> str | None:
    additions = utt.get("additions")
    speaker: Any = None
    if isinstance(additions, Mapping):
        speaker = additions.get("speaker")
    elif isinstance(additions, str) and additions.strip():
        try:
            parsed_add = json.loads(additions)
            if isinstance(parsed_add, Mapping):
                speaker = parsed_add.get("speaker")
        except (ValueError, TypeError):
            pass
    if speaker is None:
        speaker = utt.get("speaker")
    if speaker is None:
        return None
    cleaned = str(speaker).strip()
    return cleaned if cleaned else None


def format_scoped_speaker(segment_index: int, raw_speaker: str | None) -> str:
    """Format a speaker label scoped to ``S{segment_index + 1}``."""
    seg_prefix = f"S{int(segment_index) + 1}"
    if not raw_speaker:
        return f"{seg_prefix}-Speaker 1"
    cleaned = str(raw_speaker).strip()
    if cleaned.lower().startswith("speaker"):
        suffix = cleaned[len("speaker") :].strip(" -_:")
        base = f"Speaker {suffix}" if suffix else "Speaker 1"
    else:
        base = f"Speaker {cleaned}"
    return f"{seg_prefix}-{base}"


def merge_segment_transcriptions(
    segments_meta: list[dict[str, Any]],
    raw_responses: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Merge per-segment ASR responses with global timestamp offsets and scoped speaker labels."""
    merged_utterances: list[dict[str, Any]] = []
    transcript_lines: list[str] = []
    current_speaker_header: str | None = None

    for seg_meta, raw_resp in zip(segments_meta, raw_responses, strict=False):
        seg_idx = int(seg_meta.get("index") or 0)
        seg_label = f"S{seg_idx + 1}"
        start_s = float(seg_meta.get("start_s") or 0.0)
        end_s = float(seg_meta.get("end_s") or start_s)
        offset_ms = int(round(start_s * 1000.0))

        if not isinstance(raw_resp, Mapping) or raw_resp.get("silent") is True:
            continue
        result_block = raw_resp.get("result")
        if not isinstance(result_block, Mapping):
            continue

        utterances = result_block.get("utterances")
        if isinstance(utterances, list) and utterances:
            for utt in utterances:
                if not isinstance(utt, Mapping):
                    continue
                text = str(utt.get("text") or "").strip()
                if not text:
                    continue
                raw_spk = _extract_utterance_speaker(utt)
                scoped_spk = format_scoped_speaker(seg_idx, raw_spk)

                raw_start = int(utt.get("start_time", utt.get("start_ms", 0)) or 0)
                raw_end = int(utt.get("end_time", utt.get("end_ms", raw_start)) or raw_start)
                global_start = raw_start + offset_ms
                global_end = raw_end + offset_ms

                merged_utterances.append(
                    {
                        "segment_index": seg_idx,
                        "segment_label": seg_label,
                        "speaker": scoped_spk,
                        "raw_speaker": raw_spk,
                        "start_ms": global_start,
                        "end_ms": global_end,
                        "raw_start_ms": raw_start,
                        "raw_end_ms": raw_end,
                        "text": text,
                    }
                )
                if scoped_spk != current_speaker_header:
                    transcript_lines.append(f"[{scoped_spk}]")
                    current_speaker_header = scoped_spk
                transcript_lines.append(text)
            continue

        fallback_text = str(result_block.get("text") or "").strip()
        if fallback_text:
            scoped_spk = format_scoped_speaker(seg_idx, None)
            seg_end_ms = int(round(end_s * 1000.0))
            merged_utterances.append(
                {
                    "segment_index": seg_idx,
                    "segment_label": seg_label,
                    "speaker": scoped_spk,
                    "raw_speaker": None,
                    "start_ms": offset_ms,
                    "end_ms": seg_end_ms,
                    "raw_start_ms": 0,
                    "raw_end_ms": max(0, seg_end_ms - offset_ms),
                    "text": fallback_text,
                }
            )
            if scoped_spk != current_speaker_header:
                transcript_lines.append(f"[{scoped_spk}]")
                current_speaker_header = scoped_spk
            transcript_lines.append(fallback_text)

    return "\n".join(transcript_lines), merged_utterances


__all__ = [
    "ASR_SUBPROCESS_ENV_ALLOWLIST",
    "AsrWorkerError",
    "AsrWorkerProtocol",
    "DEFAULT_DISK_HEADROOM_BYTES",
    "DEFAULT_FFMPEG_TIMEOUT_S",
    "DEFAULT_FFPROBE_TIMEOUT_S",
    "DEFAULT_STREAM_CHUNK_BYTES",
    "PreparedSegmentFile",
    "SEGMENT_TIME_SECONDS",
    "SPLIT_THRESHOLD_SECONDS",
    "SourceObjectStore",
    "build_asr_subprocess_env",
    "check_disk_admission",
    "compute_file_sha256",
    "compute_segment_ranges",
    "detect_format",
    "format_scoped_speaker",
    "is_binary_available",
    "merge_segment_transcriptions",
    "normalize_asr_language",
    "normalize_whisper_language",
    "probe_audio_duration",
    "redact_asr_secrets",
    "resolve_binary",
    "should_split_audio",
    "split_audio_to_mp3_segments",
]
