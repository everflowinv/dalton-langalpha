"""Open-source OpenAI-compatible Whisper ASR worker for ``dalton-transcription``.

Supports OpenAI Audio Transcriptions API (``POST /audio/transcriptions``) as well
as self-hosted OpenAI-compatible Whisper servers (e.g., ``faster-whisper-server``,
``whisper.cpp``, corporate gateways). Performs:
1. Streaming download from ``SourceObjectStore`` with size and SHA-256 verification.
2. Disk headroom check (``2x source + 64 MiB``).
3. ``ffprobe`` duration measurement.
4. ``ffmpeg`` MP3 chunking when duration exceeds 1800s or file size exceeds the
   per-request upload cap (default 24 MiB for OpenAI Whisper's 25 MiB limit).
5. Per-segment Whisper transcription with persisted segment raw outputs so
   interrupted jobs resume from the last incomplete segment without redoing
   finished segments.
6. Global timestamp offset merging, ``raw_response.json`` + ``transcript.txt``
   persistence, and terminal temporary work directory cleanup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .worker_base import (
    DEFAULT_FFMPEG_TIMEOUT_S,
    DEFAULT_FFPROBE_TIMEOUT_S,
    SEGMENT_TIME_SECONDS,
    SPLIT_THRESHOLD_SECONDS,
    PreparedSegmentFile,
    AsrWorkerError,
    SourceObjectStore,
    check_disk_admission,
    compute_file_sha256,
    merge_segment_transcriptions,
    normalize_asr_language,
    normalize_whisper_language,
    probe_audio_duration,
    redact_asr_secrets,
    should_split_audio,
    split_audio_to_mp3_segments,
)

logger = logging.getLogger("dalton-transcription")

DEFAULT_WHISPER_API_BASE_URL: str = "https://api.openai.com/v1"
DEFAULT_WHISPER_MODEL: str = "whisper-1"
DEFAULT_WHISPER_RESPONSE_FORMAT: str = "verbose_json"
DEFAULT_WHISPER_TIMEOUT_S: float = 300.0
DEFAULT_WHISPER_JOB_DEADLINE_S: float = 1800.0
DEFAULT_WHISPER_MAX_UPLOAD_BYTES: int = 24 * 1024 * 1024  # 24 MiB safety margin under 25 MiB cap
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _parse_bool(val: str | None) -> bool:
    return str(val or "").strip().lower() in _TRUTHY


@dataclass
class WhisperWorkerConfig:
    """Configuration for :class:`OpenAICompatibleWhisperWorker`."""

    api_base_url: str = DEFAULT_WHISPER_API_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_WHISPER_MODEL
    response_format: str = DEFAULT_WHISPER_RESPONSE_FORMAT
    allow_no_auth: bool = False
    request_timeout_s: float = DEFAULT_WHISPER_TIMEOUT_S
    job_deadline_s: float = DEFAULT_WHISPER_JOB_DEADLINE_S
    split_threshold_s: float = SPLIT_THRESHOLD_SECONDS
    segment_time_s: int = SEGMENT_TIME_SECONDS
    max_upload_bytes: int = DEFAULT_WHISPER_MAX_UPLOAD_BYTES
    ffprobe_timeout_s: float = DEFAULT_FFPROBE_TIMEOUT_S
    ffmpeg_timeout_s: float = DEFAULT_FFMPEG_TIMEOUT_S
    ffprobe_bin: str | Path | None = None
    ffmpeg_bin: str | Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WhisperWorkerConfig:
        env = os.environ if environ is None else environ
        raw_base = (
            str(env.get("DALTON_WHISPER_API_BASE_URL") or "").strip().rstrip("/")
            or DEFAULT_WHISPER_API_BASE_URL
        )
        raw_key = str(
            env.get("DALTON_WHISPER_API_KEY") or env.get("OPENAI_API_KEY") or ""
        ).strip()
        model = str(env.get("DALTON_WHISPER_MODEL") or DEFAULT_WHISPER_MODEL).strip() or DEFAULT_WHISPER_MODEL
        resp_fmt = (
            str(env.get("DALTON_WHISPER_RESPONSE_FORMAT") or DEFAULT_WHISPER_RESPONSE_FORMAT).strip()
            or DEFAULT_WHISPER_RESPONSE_FORMAT
        )
        allow_no_auth = _parse_bool(env.get("DALTON_WHISPER_ALLOW_NO_AUTH"))

        def _float_env(key: str, default: float) -> float:
            try:
                val = env.get(key)
                return max(0.1, float(val)) if val and str(val).strip() else default
            except ValueError:
                return default

        def _int_env(key: str, default: int) -> int:
            try:
                val = env.get(key)
                return max(1, int(val)) if val and str(val).strip() else default
            except ValueError:
                return default

        return cls(
            api_base_url=raw_base,
            api_key=raw_key,
            model=model,
            response_format=resp_fmt,
            allow_no_auth=allow_no_auth,
            request_timeout_s=_float_env("DALTON_WHISPER_TIMEOUT_S", DEFAULT_WHISPER_TIMEOUT_S),
            job_deadline_s=_float_env("DALTON_WHISPER_JOB_DEADLINE_S", DEFAULT_WHISPER_JOB_DEADLINE_S),
            max_upload_bytes=_int_env(
                "DALTON_WHISPER_MAX_UPLOAD_BYTES", DEFAULT_WHISPER_MAX_UPLOAD_BYTES
            ),
        )

    @classmethod
    def is_configured(cls, environ: Mapping[str, str] | None = None) -> bool:
        env = os.environ if environ is None else environ
        if str(env.get("DALTON_WHISPER_API_KEY") or "").strip():
            return True
        if str(env.get("OPENAI_API_KEY") or "").strip():
            return True
        if (
            str(env.get("DALTON_WHISPER_API_BASE_URL") or "").strip()
            and _parse_bool(env.get("DALTON_WHISPER_ALLOW_NO_AUTH"))
        ):
            return True
        return False


def whisper_response_to_raw_envelope(
    payload: Mapping[str, Any],
    *,
    segment_duration_s: float,
) -> dict[str, Any]:
    """Normalize a Whisper API JSON response into the canonical raw envelope consumed by ``merge_segment_transcriptions``."""
    text = str(payload.get("text") or "").strip()
    raw_segments = payload.get("segments")
    utterances: list[dict[str, Any]] = []
    if isinstance(raw_segments, list):
        for seg in raw_segments:
            if not isinstance(seg, Mapping):
                continue
            seg_text = str(seg.get("text") or "").strip()
            if not seg_text:
                continue
            start_ms = int(round(max(0.0, float(seg.get("start") or 0.0)) * 1000.0))
            end_ms = int(round(max(float(seg.get("start") or 0.0), float(seg.get("end") or 0.0)) * 1000.0))
            utt: dict[str, Any] = {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": seg_text,
            }
            if seg.get("speaker") is not None:
                utt["speaker"] = str(seg["speaker"]).strip()
            utterances.append(utt)

    dur_raw = payload.get("duration")
    dur_ms = (
        int(round(float(dur_raw) * 1000.0))
        if isinstance(dur_raw, (int, float)) and not isinstance(dur_raw, bool) and float(dur_raw) > 0
        else int(round(max(0.0, float(segment_duration_s)) * 1000.0))
    )
    if not text and not utterances:
        return {
            "provider": "openai_whisper",
            "silent": True,
            "result": {"text": "", "utterances": []},
            "audio_info": {"duration": dur_ms},
            "raw": dict(payload),
        }
    return {
        "provider": "openai_whisper",
        "result": {
            "text": text,
            "utterances": utterances,
        },
        "audio_info": {"duration": dur_ms},
        "raw": dict(payload),
    }


class OpenAICompatibleWhisperWorker:
    """Open-source OpenAI-compatible Whisper transcription worker."""

    worker_name = "whisper"

    def __init__(
        self,
        jobs_dir: Path,
        *,
        config: WhisperWorkerConfig | None = None,
        source_store: SourceObjectStore | None = None,
        http_client: httpx.AsyncClient | None = None,
        atomic_write_fn: Callable[..., bool] | None = None,
        probe_duration_fn: Callable[..., Awaitable[float]] | None = None,
        split_segments_fn: Callable[..., Awaitable[list[PreparedSegmentFile]]] | None = None,
        disk_usage_fn: Callable[[Path | str], Any] | None = None,
    ) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.config = config or WhisperWorkerConfig.from_env()
        self._source_store = source_store
        self._http_client = http_client
        self._atomic_write = atomic_write_fn
        self._probe_duration_fn = probe_duration_fn or probe_audio_duration
        self._split_segments_fn = split_segments_fn or split_audio_to_mp3_segments
        self._disk_usage_fn = disk_usage_fn or shutil.disk_usage

    def _write_json(self, path: Path, payload: dict[str, Any], *, label: str) -> bool:
        if self._atomic_write is not None:
            return bool(self._atomic_write(path, payload, label=label))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True

    def _job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def _work_dir(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "work"

    def _segments_dir(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "segments"

    def _segment_raw_path(self, job_id: str, idx: int) -> Path:
        return self._segments_dir(job_id) / f"seg_{idx:03d}_raw.json"

    def raw_response_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "raw_response.json"

    def transcript_text_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "transcript.txt"

    async def cleanup_job_resources(
        self,
        job_id: str,
        segments: list[dict[str, Any]] | None = None,
    ) -> None:
        work_dir = self._work_dir(job_id)
        try:
            if work_dir.exists():
                await asyncio.to_thread(shutil.rmtree, work_dir)
        except Exception as exc:
            logger.warning(
                "Whisper worker cleanup warning for job_id=%s: %s", job_id, type(exc).__name__
            )

    async def _transcribe_segment_file(
        self,
        segment_path: Path,
        *,
        language: str | None,
        hotwords: list[str],
        timeout_s: float,
    ) -> tuple[str, dict[str, Any]]:
        if not self.config.api_key and not self.config.allow_no_auth:
            raise AsrWorkerError(
                "asr_credentials_missing",
                "Whisper API key is not configured (set DALTON_WHISPER_API_KEY or OPENAI_API_KEY)",
            )
        endpoint = f"{self.config.api_base_url.rstrip('/')}/audio/transcriptions"
        headers: dict[str, str] = {}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        data_fields: dict[str, str] = {
            "model": self.config.model,
            "response_format": self.config.response_format,
        }
        if self.config.response_format == "verbose_json":
            data_fields["timestamp_granularities[]"] = "segment"
        whisper_lang = normalize_whisper_language(language)
        if whisper_lang:
            data_fields["language"] = whisper_lang
        if hotwords:
            data_fields["prompt"] = ", ".join(hotwords[:50])

        audio_bytes = await asyncio.to_thread(segment_path.read_bytes)
        files = {"file": (segment_path.name, audio_bytes, "application/octet-stream")}

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=max(1.0, float(timeout_s)), trust_env=False
        )
        try:
            resp = await client.post(
                endpoint,
                headers=headers,
                data=data_fields,
                files=files,
                timeout=max(1.0, float(timeout_s)),
            )
        except httpx.TimeoutException as exc:
            raise AsrWorkerError(
                "whisper_request_timeout",
                f"Whisper API request timed out after {timeout_s:.1f}s",
            ) from exc
        except Exception as exc:
            raise AsrWorkerError(
                "whisper_network_error",
                f"Whisper API request failed ({type(exc).__name__}): "
                f"{redact_asr_secrets(str(exc), extra_secrets=[self.config.api_key])}",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        req_id = (
            str(resp.headers.get("x-request-id") or resp.headers.get("openai-request-id") or "").strip()
            or f"whisper-{uuid.uuid4().hex[:12]}"
        )
        if resp.status_code == 401 or resp.status_code == 403:
            raise AsrWorkerError(
                "whisper_auth_error",
                f"Whisper API authentication failed (HTTP {resp.status_code})",
            )
        if resp.status_code == 429:
            raise AsrWorkerError(
                "whisper_rate_limited",
                "Whisper API rate limited (HTTP 429)",
            )
        if resp.status_code >= 400:
            snippet = redact_asr_secrets(
                resp.text[:240], extra_secrets=[self.config.api_key]
            )
            raise AsrWorkerError(
                "whisper_upstream_error",
                f"Whisper API returned HTTP {resp.status_code}: {snippet}",
            )

        try:
            parsed = resp.json()
            if not isinstance(parsed, dict):
                parsed = {"text": str(parsed)}
        except Exception:
            parsed = {"text": resp.text.strip()}
        return req_id, parsed

    async def run_job(
        self,
        job_state: dict[str, Any],
        *,
        on_progress: Callable[[dict[str, Any]], Awaitable[bool | None]],
    ) -> dict[str, Any]:
        job_id = str(job_state["job_id"])
        attempt = int(job_state.get("attempt") or 1)
        caller = dict(job_state.get("caller") or {})
        workspace_id = str(caller.get("workspace_id") or "workspace")
        source_object_key = str(job_state.get("source_object_key") or "").strip()
        source_size_bytes = int(job_state.get("source_size_bytes") or 0)
        source_sha256 = str(job_state.get("source_sha256") or "").strip().lower()
        raw_language = job_state.get("language")
        norm_language = normalize_asr_language(raw_language)
        hotwords = [
            str(w).strip()
            for w in (job_state.get("hotwords") or [])
            if str(w).strip()
        ]

        work_dir = self._work_dir(job_id)
        segments_dir = self._segments_dir(job_id)
        segments_dir.mkdir(parents=True, exist_ok=True)

        existing_segments: list[dict[str, Any]] = [
            dict(s) for s in (job_state.get("segments") or []) if isinstance(s, dict)
        ]
        provider_request_ids: list[str] = [
            str(r) for r in (job_state.get("provider_request_ids") or []) if str(r).strip()
        ]
        raw_dur = job_state.get("duration_seconds")
        if not isinstance(raw_dur, (int, float)) or float(raw_dur) <= 0.0:
            raw_dur = (job_state.get("usage") or {}).get("duration_seconds")
        duration_s = float(raw_dur or 0.0)
        deadline_mono = time.monotonic() + self.config.job_deadline_s
        terminal_reached = False

        def _build_usage(
            *,
            dur_s: float,
            submitted_s: float,
            completed_s: float,
            billable_s: float | None,
            last_req_id: str | None = None,
        ) -> dict[str, Any]:
            return {
                "user_id": caller.get("user_id", ""),
                "workspace_id": workspace_id,
                "thread_id": caller.get("thread_id", ""),
                "duration_seconds": round(dur_s, 3),
                "submitted_seconds": round(submitted_s, 3),
                "completed_seconds": round(completed_s, 3),
                "billable_seconds": round(billable_s, 3) if billable_s is not None else None,
                "provider_request_id": last_req_id
                or (provider_request_ids[-1] if provider_request_ids else None),
                "provider": "openai_whisper",
                "model": self.config.model,
                "attempt": attempt,
            }

        def _current_usage() -> dict[str, Any]:
            sub_s = sum(
                float(s.get("end_s", 0.0)) - float(s.get("start_s", 0.0))
                for s in existing_segments
                if s.get("provider_request_id")
            )
            comp_s = sum(
                float(s.get("end_s", 0.0)) - float(s.get("start_s", 0.0))
                for s in existing_segments
                if s.get("status") == "succeeded"
            )
            return _build_usage(
                dur_s=duration_s,
                submitted_s=sub_s,
                completed_s=comp_s,
                billable_s=None,
            )

        try:
            source_store = self._source_store or SourceObjectStore()

            # Load any already-succeeded segment raw responses for resume
            raw_responses_by_idx: dict[int, dict[str, Any]] = {}
            for seg in existing_segments:
                idx = int(seg.get("index") or 0)
                if seg.get("status") == "succeeded":
                    seg_raw = self._segment_raw_path(job_id, idx)
                    if seg_raw.is_file():
                        try:
                            loaded = json.loads(seg_raw.read_text(encoding="utf-8"))
                            if isinstance(loaded, dict):
                                raw_responses_by_idx[idx] = loaded
                        except Exception:
                            pass

            needs_audio = not existing_segments or any(
                seg.get("status") != "succeeded" or int(seg.get("index") or 0) not in raw_responses_by_idx
                for seg in existing_segments
            )
            prepared_by_idx: dict[int, PreparedSegmentFile] = {}

            if needs_audio:
                check_disk_admission(
                    work_dir,
                    source_size_bytes,
                    disk_usage_fn=self._disk_usage_fn,
                )
                await on_progress(
                    {
                        "status": "downloading",
                        "stage": "downloading",
                        "segment_count": max(1, len(existing_segments)),
                        "completed_segments": sum(
                            1 for s in existing_segments if s.get("status") == "succeeded"
                        ),
                        "usage": _current_usage(),
                    }
                )
                src_name = Path(source_object_key).name or "source.audio"
                source_path = work_dir / src_name
                await asyncio.to_thread(
                    source_store.download_to_path,
                    source_object_key,
                    source_path,
                    expected_size_bytes=source_size_bytes,
                    expected_sha256=source_sha256,
                    deadline_monotonic=deadline_mono,
                )

                await on_progress(
                    {
                        "status": "probing",
                        "stage": "probing",
                        "segment_count": max(1, len(existing_segments)),
                        "completed_segments": sum(
                            1 for s in existing_segments if s.get("status") == "succeeded"
                        ),
                        "usage": _current_usage(),
                    }
                )
                rem_probe = max(1.0, min(self.config.ffprobe_timeout_s, deadline_mono - time.monotonic()))
                duration_s = await self._probe_duration_fn(
                    source_path,
                    ffprobe_bin=self.config.ffprobe_bin,
                    timeout_s=rem_probe,
                )

                must_split = (
                    should_split_audio(duration_s, threshold_seconds=self.config.split_threshold_s)
                    or source_size_bytes > self.config.max_upload_bytes
                )
                if must_split:
                    await on_progress(
                        {
                            "status": "splitting",
                            "stage": "splitting",
                            "duration_seconds": round(duration_s, 3),
                            "segment_count": max(1, len(existing_segments)),
                            "completed_segments": sum(
                                1 for s in existing_segments if s.get("status") == "succeeded"
                            ),
                            "usage": _current_usage(),
                        }
                    )
                    rem_split = max(1.0, min(self.config.ffmpeg_timeout_s, deadline_mono - time.monotonic()))
                    prepared_list = await self._split_segments_fn(
                        source_path,
                        work_dir / "chunks",
                        total_duration_s=duration_s,
                        segment_time_s=self.config.segment_time_s,
                        ffmpeg_bin=self.config.ffmpeg_bin,
                        timeout_s=rem_split,
                    )
                else:
                    sha_hex, fsize = await asyncio.to_thread(compute_file_sha256, source_path)
                    prepared_list = [
                        PreparedSegmentFile(
                            index=0,
                            path=source_path,
                            start_s=0.0,
                            end_s=round(duration_s, 3),
                            sha256=sha_hex,
                            size_bytes=fsize,
                            codec_params={"format": source_path.suffix.lstrip(".") or "mp3"},
                        )
                    ]

                existing_by_idx = {int(s.get("index") or 0): s for s in existing_segments}
                new_segments: list[dict[str, Any]] = []
                for pf in prepared_list:
                    prepared_by_idx[pf.index] = pf
                    prev = existing_by_idx.get(pf.index)
                    if prev and prev.get("status") == "succeeded" and pf.index in raw_responses_by_idx:
                        new_segments.append(prev)
                    else:
                        new_segments.append(
                            {
                                "index": pf.index,
                                "start_s": pf.start_s,
                                "end_s": pf.end_s,
                                "sha256": pf.sha256,
                                "size_bytes": pf.size_bytes,
                                "codec_params": dict(pf.codec_params),
                                "status": "queued",
                                "provider_request_id": None,
                                "attempt": attempt,
                            }
                        )
                existing_segments = new_segments

            segment_count = max(1, len(existing_segments))
            for seg in existing_segments:
                idx = int(seg["index"])
                if seg.get("status") == "succeeded" and idx in raw_responses_by_idx:
                    continue
                rem = deadline_mono - time.monotonic()
                if rem <= 0:
                    raise AsrWorkerError(
                        "asr_job_deadline_exceeded",
                        f"Overall job deadline ({self.config.job_deadline_s}s) exceeded before segment {idx}",
                    )
                pf = prepared_by_idx.get(idx)
                if pf is None:
                    raise AsrWorkerError(
                        "segment_file_missing",
                        f"Prepared audio segment {idx} is missing on disk",
                    )

                seg["status"] = "transcribing"
                await on_progress(
                    {
                        "status": "transcribing",
                        "stage": "transcribing",
                        "duration_seconds": round(duration_s, 3),
                        "segment_count": segment_count,
                        "completed_segments": sum(
                            1 for s in existing_segments if s.get("status") == "succeeded"
                        ),
                        "current_segment": idx,
                        "provider_request_ids": list(provider_request_ids),
                        "segments": [dict(s) for s in existing_segments],
                        "usage": _current_usage(),
                    }
                )

                req_id, raw_whisper = await self._transcribe_segment_file(
                    pf.path,
                    language=raw_language,
                    hotwords=hotwords,
                    timeout_s=min(self.config.request_timeout_s, rem),
                )
                if req_id not in provider_request_ids:
                    provider_request_ids.append(req_id)
                seg["provider_request_id"] = req_id

                seg_dur = max(0.0, float(seg.get("end_s") or 0.0) - float(seg.get("start_s") or 0.0))
                envelope = whisper_response_to_raw_envelope(raw_whisper, segment_duration_s=seg_dur)
                self._write_json(
                    self._segment_raw_path(job_id, idx),
                    envelope,
                    label=f"asr_segment_{idx:03d}_raw",
                )
                raw_responses_by_idx[idx] = envelope
                seg["status"] = "succeeded"

                completed_count = sum(
                    1 for s in existing_segments if s.get("status") == "succeeded"
                )
                await on_progress(
                    {
                        "status": "transcribing",
                        "stage": "transcribing",
                        "duration_seconds": round(duration_s, 3),
                        "segment_count": segment_count,
                        "completed_segments": completed_count,
                        "current_segment": idx,
                        "provider_request_ids": list(provider_request_ids),
                        "segments": [dict(s) for s in existing_segments],
                        "usage": _current_usage(),
                    }
                )

            await on_progress(
                {
                    "status": "assembling",
                    "stage": "assembling",
                    "duration_seconds": round(duration_s, 3),
                    "segment_count": segment_count,
                    "completed_segments": segment_count,
                    "current_segment": None,
                    "provider_request_ids": list(provider_request_ids),
                    "segments": [dict(s) for s in existing_segments],
                    "usage": _current_usage(),
                }
            )

            ordered_raw = [
                raw_responses_by_idx.get(int(s["index"]), {}) for s in existing_segments
            ]
            raw_bundle = {
                "job_id": job_id,
                "provider": "openai_whisper",
                "model": self.config.model,
                "duration_seconds": round(duration_s, 3),
                "segment_count": segment_count,
                "segments": [
                    {
                        "index": int(s["index"]),
                        "segment_label": f"S{int(s['index']) + 1}",
                        "start_s": s["start_s"],
                        "end_s": s["end_s"],
                        "offset_ms": int(round(float(s["start_s"]) * 1000.0)),
                        "sha256": s.get("sha256"),
                        "codec_params": s.get("codec_params"),
                        "provider_request_id": s.get("provider_request_id"),
                        "raw_response": ordered_raw[i],
                    }
                    for i, s in enumerate(existing_segments)
                ],
            }
            self._write_json(self.raw_response_path(job_id), raw_bundle, label="asr_raw_response")

            merged_text, merged_utterances = merge_segment_transcriptions(
                existing_segments, ordered_raw
            )
            transcript_path = self.transcript_text_path(job_id)
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text((merged_text + "\n") if merged_text else "", encoding="utf-8")

            submitted_s = sum(
                float(s.get("end_s", 0.0)) - float(s.get("start_s", 0.0))
                for s in existing_segments
            )
            usage = _build_usage(
                dur_s=duration_s,
                submitted_s=submitted_s,
                completed_s=duration_s,
                billable_s=duration_s,
            )
            result_object_key = f"transcriptions/{workspace_id}/{job_id}/result.json"
            result_payload = {
                "job_id": job_id,
                "language": norm_language or raw_language,
                "duration_seconds": round(duration_s, 3),
                "text": merged_text,
                "utterances": merged_utterances,
                "segments": [dict(s) for s in existing_segments],
                "provider_request_ids": list(provider_request_ids),
                "raw_response_file": "raw_response.json",
                "transcript_file": "transcript.txt",
                "usage": usage,
            }
            terminal_reached = True
            return {
                "status": "succeeded",
                "stage": "succeeded",
                "duration_seconds": round(duration_s, 3),
                "segment_count": segment_count,
                "completed_segments": segment_count,
                "current_segment": None,
                "provider_request_ids": list(provider_request_ids),
                "segments": [dict(s) for s in existing_segments],
                "usage": usage,
                "result_object_key": result_object_key,
                "error_category": None,
                "error_message": None,
                "result": result_payload,
            }

        except asyncio.CancelledError:
            raise

        except AsrWorkerError as exc:
            terminal_reached = True
            for s in existing_segments:
                if s.get("status") in ("submitting", "transcribing"):
                    s["status"] = "failed"
                    s["error_category"] = exc.category
            submitted_s = sum(
                float(s.get("end_s", 0.0)) - float(s.get("start_s", 0.0))
                for s in existing_segments
                if s.get("provider_request_id")
            )
            completed_s = sum(
                float(s.get("end_s", 0.0)) - float(s.get("start_s", 0.0))
                for s in existing_segments
                if s.get("status") == "succeeded"
            )
            return {
                "status": "failed",
                "stage": "failed",
                "duration_seconds": round(duration_s, 3),
                "segment_count": max(1, len(existing_segments)),
                "completed_segments": sum(
                    1 for s in existing_segments if s.get("status") == "succeeded"
                ),
                "current_segment": None,
                "provider_request_ids": list(provider_request_ids),
                "segments": [dict(s) for s in existing_segments],
                "usage": _build_usage(
                    dur_s=duration_s,
                    submitted_s=submitted_s,
                    completed_s=completed_s,
                    billable_s=None,
                ),
                "error_category": exc.category,
                "error_message": exc.message,
            }

        except Exception:
            terminal_reached = True
            raise

        finally:
            if terminal_reached:
                await self.cleanup_job_resources(job_id, existing_segments)


__all__ = [
    "DEFAULT_WHISPER_API_BASE_URL",
    "DEFAULT_WHISPER_JOB_DEADLINE_S",
    "DEFAULT_WHISPER_MAX_UPLOAD_BYTES",
    "DEFAULT_WHISPER_MODEL",
    "DEFAULT_WHISPER_RESPONSE_FORMAT",
    "DEFAULT_WHISPER_TIMEOUT_S",
    "OpenAICompatibleWhisperWorker",
    "WhisperWorkerConfig",
    "whisper_response_to_raw_envelope",
]
