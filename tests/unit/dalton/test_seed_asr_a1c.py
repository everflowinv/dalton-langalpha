"""Unit tests for Backlog A1c: real Seed-ASR client, ffprobe/ffmpeg segmentation, TOS transfer, and merge.

All tests run strictly offline using `create_autospec` doubles for TOS / Seed-ASR / S3
object storage and real `/opt/homebrew/bin/ffmpeg` + `/opt/homebrew/bin/ffprobe` on
tiny (2-3 second) synthetic `sine` / `anullsrc` audio files.
"""

from __future__ import annotations

import ast
import asyncio
from collections import namedtuple
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_DIR = REPO_ROOT / "scripts" / "dalton"
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import asr_jobs  # noqa: E402
import researchdata_bridge as rb  # noqa: E402
import seed_asr_client as sac  # noqa: E402

DiskUsage = namedtuple("usage", ["total", "used", "free"])


def _test_credentials() -> sac.SeedAsrCredentials:
    return sac.SeedAsrCredentials(
        app_id="test_seed_app_id_secret_999",
        access_key="test_seed_access_key_secret_888",
        tos_ak="AKLTTosAccessKeySecret777",
        tos_sk="SKLTTosSecretKeyValue666",
        tos_bucket="dalton-asr-test-bucket",
        tos_region="cn-beijing",
        tos_endpoint="tos-cn-beijing.volces.com",
    )


def _make_httpx_response(
    status_code: int = 200,
    *,
    api_status_code: str | None = "20000000",
    api_message: str = "OK",
    json_body: Any = None,
    raw_text: str | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {"content-type": "application/json"}
    if api_status_code is not None:
        headers["X-Api-Status-Code"] = api_status_code
    if api_message:
        headers["X-Api-Message"] = api_message
    if raw_text is not None:
        content = raw_text.encode("utf-8")
    elif json_body is not None:
        content = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
    else:
        content = b"{}"
    return httpx.Response(status_code=status_code, headers=headers, content=content)


def _generate_synthetic_audio(
    dest_path: Path,
    *,
    duration_s: float = 2.4,
    source_filter: str | None = None,
) -> tuple[bytes, str]:
    """Generate a tiny synthetic audio file via `/opt/homebrew/bin/ffmpeg` (`sine` or `anullsrc`)."""
    ffmpeg_bin = "/opt/homebrew/bin/ffmpeg"
    assert Path(ffmpeg_bin).is_file(), "Expected /opt/homebrew/bin/ffmpeg on host"
    lavfi_spec = source_filter or f"sine=frequency=440:duration={duration_s}"
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi_spec,
        "-ar",
        "8000",
        "-ac",
        "1",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "9",
        str(dest_path),
    ]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=15.0,
        check=True,
        env=sac.build_asr_subprocess_env(),
    )
    assert res.returncode == 0
    raw_bytes = dest_path.read_bytes()
    assert 0 < len(raw_bytes) < 64 * 1024
    return raw_bytes, hashlib.sha256(raw_bytes).hexdigest()


def _make_fake_source_store(audio_bytes: bytes) -> asr_jobs.SourceObjectStore:
    """Create a `create_autospec(SourceObjectStore)` that streams `audio_bytes` in fixed-size chunks."""
    store = create_autospec(asr_jobs.SourceObjectStore, instance=True)

    def _download_to_path(
        source_object_key: str,
        dest_path: Path,
        *,
        expected_size_bytes: int,
        expected_sha256: str,
        chunk_size: int = asr_jobs.DEFAULT_STREAM_CHUNK_BYTES,
        deadline_monotonic: float | None = None,
    ) -> tuple[int, str]:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise asr_jobs.AsrWorkerError(
                "asr_job_deadline_exceeded",
                "Overall job deadline exceeded during source audio download",
            )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        stream = io.BytesIO(audio_bytes)
        hasher = hashlib.sha256()
        written = 0
        with dest_path.open("wb") as fh:
            while True:
                chunk = stream.read(max(256, min(chunk_size, 4096)))
                if not chunk:
                    break
                written += len(chunk)
                hasher.update(chunk)
                fh.write(chunk)
        digest = hasher.hexdigest().lower()
        assert written == expected_size_bytes
        assert digest == expected_sha256.lower()
        return written, digest

    store.download_to_path.side_effect = _download_to_path
    return store


# ---------------------------------------------------------------------------
# 1. 30-Minute Boundary (1799s / 1800s / 1801s) & Last Short Segment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_30_minute_boundary_1799_1800_1801_and_last_short_segment(
    tmp_path: Path,
) -> None:
    """1799s and 1800s are NOT split (1 segment); 1801s IS split into 4 x 600s segments with a 1s final short segment."""
    assert asr_jobs.should_split_audio(1799.0) is False
    assert asr_jobs.should_split_audio(1800.0) is False
    assert asr_jobs.should_split_audio(1801.0) is True

    assert asr_jobs.compute_segment_ranges(1799.0) == [(0.0, 1799.0)]
    assert asr_jobs.compute_segment_ranges(1800.0) == [(0.0, 1800.0)]
    assert asr_jobs.compute_segment_ranges(1801.0) == [
        (0.0, 600.0),
        (600.0, 1200.0),
        (1200.0, 1800.0),
        (1800.0, 1801.0),
    ]

    sample_audio = tmp_path / "sample.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.2)

    # Verify worker behavior across 1799s, 1800s, and 1801s
    for dur_s, expect_split, expect_seg_count in (
        (1799.0, False, 1),
        (1800.0, False, 1),
        (1801.0, True, 4),
    ):
        jobs_dir = tmp_path / f"jobs_{int(dur_s)}"
        source_store = _make_fake_source_store(audio_bytes)
        tos_client = create_autospec(sac.VolcTosClient, instance=True)
        tos_client.upload_and_presign.side_effect = (
            lambda filepath, *, object_key=None, expires_in=None: (
                f"https://tos.example.com/{object_key}?X-Amz-Signature=secret",
                object_key or "asr-temp/seg.mp3",
            )
        )
        tos_client.delete_object.return_value = True

        asr_client = create_autospec(sac.SeedAsrClient, instance=True)

        async def _fake_transcribe_segment(
            *,
            audio_url: str,
            audio_format: str,
            persist_request_id_fn: Any,
            language: str | None = None,
            hotwords: list[str] | None = None,
            uid: str = "dalton-asr",
            request_id: str | None = None,
            already_submitted: bool = False,
            on_status_change_fn: Any = None,
            deadline_monotonic: float | None = None,
        ) -> sac.QueryOutcome:
            req_id = request_id or f"req-{len(asr_client.transcribe_segment.mock_calls)}"
            await persist_request_id_fn(req_id)
            if on_status_change_fn is not None:
                await on_status_change_fn("transcribing", req_id)
            return sac.QueryOutcome(
                request_id=req_id,
                state="succeeded",
                provider_status_code="20000000",
                response={
                    "result": {
                        "text": f"文本 {req_id}",
                        "utterances": [
                            {
                                "text": f"文本 {req_id}",
                                "start_time": 100,
                                "end_time": 900,
                                "additions": {"speaker": "1"},
                            }
                        ],
                    }
                },
            )

        asr_client.transcribe_segment.side_effect = _fake_transcribe_segment

        mock_probe = create_autospec(asr_jobs.probe_audio_duration, return_value=dur_s)

        async def _fake_split(
            source_path: Path,
            chunks_dir: Path,
            *,
            total_duration_s: float,
            segment_time_s: int = 600,
            ffmpeg_bin: Any = None,
            timeout_s: float = 600.0,
            transcode_semaphore: Any = None,
            base_env: Any = None,
        ) -> list[asr_jobs.PreparedSegmentFile]:
            chunks_dir.mkdir(parents=True, exist_ok=True)
            ranges = asr_jobs.compute_segment_ranges(
                total_duration_s, segment_time_seconds=segment_time_s
            )
            out: list[asr_jobs.PreparedSegmentFile] = []
            for idx, (st, en) in enumerate(ranges):
                cpath = chunks_dir / f"chunk_{idx:02d}.mp3"
                cpath.write_bytes(audio_bytes)
                out.append(
                    asr_jobs.PreparedSegmentFile(
                        index=idx,
                        path=cpath,
                        start_s=st,
                        end_s=en,
                        sha256=audio_sha,
                        size_bytes=len(audio_bytes),
                        codec_params={
                            "format": "mp3",
                            "codec": "libmp3lame",
                            "q_a": "2",
                            "segment_time": segment_time_s,
                        },
                    )
                )
            return out

        mock_split = create_autospec(
            asr_jobs.split_audio_to_mp3_segments, side_effect=_fake_split
        )

        worker = asr_jobs.RealAsrWorker(
            jobs_dir,
            source_store=source_store,
            tos_client=tos_client,
            asr_client=asr_client,
            probe_duration_fn=mock_probe,
            split_audio_fn=mock_split,
        )
        observed_stages: list[str] = []

        async def _on_prog(patch: dict[str, Any]) -> None:
            if "stage" in patch:
                observed_stages.append(str(patch["stage"]))

        outcome = await worker.run_job(
            {
                "job_id": f"job_{int(dur_s)}",
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": f"transcriptions/ws-1/job_{int(dur_s)}/source/call.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
                "language": None,
                "hotwords": [],
            },
            on_progress=_on_prog,
        )
        assert outcome["status"] == "succeeded"
        assert outcome["segment_count"] == expect_seg_count
        assert outcome["completed_segments"] == expect_seg_count
        assert (mock_split.call_count == 1) is expect_split
        assert ("splitting" in observed_stages) is expect_split
        assert "downloading" in observed_stages
        assert "probing" in observed_stages
        assert "submitting" in observed_stages
        assert "transcribing" in observed_stages
        assert "assembling" in observed_stages

        if dur_s == 1801.0:
            segs = outcome["segments"]
            assert len(segs) == 4
            assert segs[0]["start_s"] == 0.0 and segs[0]["end_s"] == 600.0
            assert segs[1]["start_s"] == 600.0 and segs[1]["end_s"] == 1200.0
            assert segs[2]["start_s"] == 1200.0 and segs[2]["end_s"] == 1800.0
            # Final 1-second short segment (1800.0 -> 1801.0)
            assert segs[3]["start_s"] == 1800.0 and segs[3]["end_s"] == 1801.0
            assert segs[3]["duration_s"] == 1.0
            assert segs[3]["sha256"] == audio_sha
            assert segs[3]["codec_params"]["codec"] == "libmp3lame"


# ---------------------------------------------------------------------------
# 2. Real `/opt/homebrew/bin/ffprobe` & `/opt/homebrew/bin/ffmpeg` on Tiny
#    Synthetic Audio (`sine` and `anullsrc`) + Transcode Concurrency = 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_ffprobe_and_ffmpeg_segmentation_on_synthetic_audio(
    tmp_path: Path,
) -> None:
    """Exercise real `/opt/homebrew/bin/ffprobe` and `/opt/homebrew/bin/ffmpeg` on a 2.5s synthetic audio file, producing a short final segment."""
    synth_path = tmp_path / "synth_sine.mp3"
    _raw, _sha = _generate_synthetic_audio(synth_path, duration_s=2.5)

    probed_dur = await asr_jobs.probe_audio_duration(synth_path)
    assert 2.3 <= probed_dur <= 2.7

    chunks_dir = tmp_path / "chunks"
    segments = await asr_jobs.split_audio_to_mp3_segments(
        synth_path,
        chunks_dir,
        total_duration_s=probed_dur,
        segment_time_s=1,
    )
    assert len(segments) == 3
    assert segments[0].index == 0 and segments[0].start_s == 0.0 and segments[0].end_s == 1.0
    assert segments[1].index == 1 and segments[1].start_s == 1.0 and segments[1].end_s == 2.0
    # Final short segment (~0.5s)
    assert segments[2].index == 2 and segments[2].start_s == 2.0
    assert 2.3 <= segments[2].end_s <= 2.7
    for seg in segments:
        assert seg.path.is_file()
        assert seg.size_bytes > 0
        assert len(seg.sha256) == 64
        assert seg.codec_params == {
            "format": "mp3",
            "codec": "libmp3lame",
            "q_a": "2",
            "segment_time": 1,
        }

    # Also verify `anullsrc` silent synthetic audio probing with real ffprobe
    silent_path = tmp_path / "synth_anullsrc.mp3"
    _generate_synthetic_audio(
        silent_path,
        duration_s=1.2,
        source_filter="anullsrc=r=8000:cl=mono:d=1.2",
    )
    silent_dur = await asr_jobs.probe_audio_duration(silent_path)
    assert 1.0 <= silent_dur <= 1.5


# ---------------------------------------------------------------------------
# 3. Language Parameter Rules (`en-US` Explicit, Omitted When Unset) & Hotwords
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_language_parameter_english_explicit_and_omitted_when_unset() -> None:
    """`build_submit_payload` omits `audio.language` when language is None/empty, and writes explicit `en-US` for English."""
    # 1. Unset / None / empty -> `language` key is NOT present in `audio`
    p_none = sac.build_submit_payload(
        audio_url="https://tos.example.com/a.mp3",
        audio_format="mp3",
        language=None,
    )
    assert "language" not in p_none["audio"]
    assert p_none["request"] == {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "enable_speaker_info": True,
        "ssd_version": "300",
        "show_utterances": True,
    }

    p_empty = sac.build_submit_payload(
        audio_url="https://tos.example.com/a.mp3",
        audio_format="mp3",
        language="   ",
    )
    assert "language" not in p_empty["audio"]

    # 2. English variants (`en`, `en-us`, `en_US`, `en-US`) -> explicit `en-US`
    for eng_input in ("en", "en-us", "en_US", "en-US", "english"):
        p_en = sac.build_submit_payload(
            audio_url="https://tos.example.com/a.mp3",
            audio_format="mp3",
            language=eng_input,
            hotwords=["Blackwell", "NVLink"],
        )
        assert p_en["audio"]["language"] == "en-US"
        corpus_ctx = json.loads(p_en["request"]["corpus"]["context"])
        assert corpus_ctx == {
            "hotwords": [{"word": "Blackwell"}, {"word": "NVLink"}]
        }

    # 3. Explicit language passed (e.g. `zh-CN`) is preserved when explicitly provided
    p_zh = sac.build_submit_payload(
        audio_url="https://tos.example.com/a.mp3",
        audio_format="mp3",
        language="zh-CN",
    )
    assert p_zh["audio"]["language"] == "zh-CN"


# ---------------------------------------------------------------------------
# 4. Strict 3-Way Validation (Missing `X-Api-Status-Code`, Bad HTTP, Bad JSON)
#    & Silent Audio (`20000003` -> Empty Result)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_status_code_and_malformed_structure_fail_and_silent_audio_succeeds() -> None:
    """Missing `X-Api-Status-Code` header on HTTP 200 fails on both submit and query; malformed structure fails; `20000003` silent audio returns empty result."""
    creds = _test_credentials()
    http_client = create_autospec(httpx.AsyncClient, instance=True)
    client = sac.SeedAsrClient(
        creds,
        http_client=http_client,
        poll_interval_s=0.0,
        initial_backoff_s=0.0,
    )

    # 1. Submit HTTP 200 with missing `X-Api-Status-Code` header -> MUST fail (fixes OpenClaw `:187`)
    http_client.post.return_value = _make_httpx_response(
        status_code=200, api_status_code=None, json_body={}
    )
    with pytest.raises(sac.SeedAsrError) as exc_sub_missing:
        await client.submit_once(
            request_id="req-missing-1",
            audio_url="https://tos.example.com/a.mp3",
            audio_format="mp3",
        )
    assert exc_sub_missing.value.category == "asr_missing_status_code"
    assert exc_sub_missing.value.submit_uncertain is True

    # 2. Query HTTP 200 with missing `X-Api-Status-Code` header -> MUST fail
    http_client.post.return_value = _make_httpx_response(
        status_code=200,
        api_status_code=None,
        json_body={"result": {"text": "should not pass"}},
    )
    with pytest.raises(sac.SeedAsrError) as exc_q_missing:
        await client.query_once("req-missing-2")
    assert exc_q_missing.value.category == "asr_missing_status_code"

    # 3. Query HTTP 200 with `X-Api-Status-Code: 20000000` but missing `result` block -> MUST fail
    http_client.post.return_value = _make_httpx_response(
        status_code=200,
        api_status_code="20000000",
        json_body={"audio_info": {"duration": 1000}},
    )
    with pytest.raises(sac.SeedAsrError) as exc_bad_struct:
        await client.query_once("req-bad-struct")
    assert exc_bad_struct.value.category == "asr_invalid_response_structure"

    # 4. Silent audio (`X-Api-Status-Code: 20000003`) -> succeeds with empty text and empty utterances
    http_client.post.return_value = _make_httpx_response(
        status_code=200,
        api_status_code="20000003",
        api_message="Silent audio",
        json_body={},
    )
    silent_outcome = await client.query_once("req-silent-1")
    assert silent_outcome.state == "succeeded"
    assert silent_outcome.is_silent is True
    assert silent_outcome.response["result"] == {"text": "", "utterances": []}

    merged_text, merged_utts = asr_jobs.merge_segment_transcriptions(
        [{"index": 0, "start_s": 0.0, "end_s": 10.0}],
        [silent_outcome.response],
    )
    assert merged_text == ""
    assert merged_utts == []


# ---------------------------------------------------------------------------
# 5. Pre-Submit Request ID Persistence & Lost Submit Response -> Query First
#    Without Re-Submitting (`indeterminate` until resolved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lost_submit_response_persists_request_id_marks_indeterminate_and_queries_first() -> None:
    """When `submit` loses its response (timeout), `request_id` is already persisted, status becomes `indeterminate`, and `query` resolves it with ZERO re-submissions."""
    creds = _test_credentials()
    http_client = create_autospec(httpx.AsyncClient, instance=True)
    client = sac.SeedAsrClient(
        creds,
        http_client=http_client,
        poll_interval_s=0.0,
        initial_backoff_s=0.0,
        max_retries=2,
    )

    events: list[tuple[str, str]] = []
    submit_calls: list[str] = []
    query_calls: list[str] = []

    async def _fake_post(
        url: str, *, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None
    ) -> httpx.Response:
        req_id = (headers or {}).get("X-Api-Request-Id", "")
        if url == sac.API_SUBMIT:
            submit_calls.append(req_id)
            # Ensure request_id was persisted BEFORE the submit HTTP request was sent!
            assert ("persisted", req_id) in events
            raise httpx.ReadTimeout("Submit response lost over network")
        if url == sac.API_QUERY:
            query_calls.append(req_id)
            if len(query_calls) == 1:
                return _make_httpx_response(
                    status_code=200,
                    api_status_code="20000001",
                    json_body={},
                )
            return _make_httpx_response(
                status_code=200,
                api_status_code="20000000",
                json_body={
                    "result": {
                        "text": "找回的转写结果",
                        "utterances": [
                            {
                                "text": "找回的转写结果",
                                "start_time": 0,
                                "end_time": 1500,
                                "additions": {"speaker": "1"},
                            }
                        ],
                    }
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    http_client.post.side_effect = _fake_post

    async def _persist_req_id(rid: str) -> None:
        events.append(("persisted", rid))

    async def _on_status(st: str, rid: str) -> None:
        events.append((f"status:{st}", rid))

    outcome = await client.transcribe_segment(
        audio_url="https://tos.example.com/seg0.mp3",
        audio_format="mp3",
        persist_request_id_fn=_persist_req_id,
        on_status_change_fn=_on_status,
    )
    assert outcome.state == "succeeded"
    assert outcome.response["result"]["text"] == "找回的转写结果"
    # Submit was called ONLY ONCE; lost response triggered indeterminate -> query first!
    assert len(submit_calls) == 1
    req_id = submit_calls[0]
    assert query_calls == [req_id, req_id]
    assert events[0] == ("persisted", req_id)
    assert ("status:indeterminate", req_id) in events
    assert ("status:transcribing", req_id) in events


@pytest.mark.asyncio
async def test_lost_submit_and_unreachable_query_stays_indeterminate_until_retry_queries(
    tmp_path: Path,
) -> None:
    """When submit response is lost AND query also times out, job stays `indeterminate`; subsequent retry queries the existing `request_id` without re-submitting."""
    sample_audio = tmp_path / "indet.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/indet.mp3?sig=1",
        "asr-temp/indet.mp3",
    )
    tos_client.delete_object.return_value = True

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    submit_count = 0
    query_count = 0
    query_should_succeed = False

    async def _post(
        url: str, *, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None
    ) -> httpx.Response:
        nonlocal submit_count, query_count
        if url == sac.API_SUBMIT:
            submit_count += 1
            raise httpx.ReadTimeout("Lost submit response")
        if url == sac.API_QUERY:
            query_count += 1
            if not query_should_succeed:
                raise httpx.ConnectError("Network down during reconciliation")
            return _make_httpx_response(
                status_code=200,
                api_status_code="20000000",
                json_body={
                    "result": {
                        "text": "恢复查询成功",
                        "utterances": [
                            {
                                "text": "恢复查询成功",
                                "start_time": 0,
                                "end_time": 1000,
                                "additions": {"speaker": "1"},
                            }
                        ],
                    }
                },
            )
        raise AssertionError(url)

    http_client.post.side_effect = _post
    asr_client = sac.SeedAsrClient(
        _test_credentials(),
        http_client=http_client,
        poll_interval_s=0.0,
        initial_backoff_s=0.0,
        max_retries=1,
    )

    jobs_dir = tmp_path / "asr_jobs"
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=120.0
        ),
    )
    mgr = asr_jobs.AsrJobManager(jobs_dir, worker=worker)

    code, sub_resp = await mgr.submit_job(
        {
            "job_id": "job_indet_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_indet_01/source/indet.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-indet-01",
        }
    )
    assert code == 200

    for _ in range(50):
        _, st_resp = await mgr.get_job("job_indet_01")
        if st_resp["job"]["status"] == "indeterminate":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected job to enter indeterminate state")

    assert submit_count == 1
    persisted_req_id = st_resp["job"]["segments"][0]["provider_request_id"]
    assert persisted_req_id

    # Now network recovers; retry_job must query `persisted_req_id` and NOT call submit again!
    query_should_succeed = True
    code_ret, _ = await mgr.retry_job("job_indet_01")
    assert code_ret == 200

    for _ in range(50):
        _, st_resp2 = await mgr.get_job("job_indet_01")
        if st_resp2["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected indeterminate job to succeed on retry via query")

    assert submit_count == 1, "Must NEVER re-submit when query reconciles the persisted request_id"
    assert st_resp2["job"]["provider_request_ids"] == [persisted_req_id]


# ---------------------------------------------------------------------------
# 6. Single-Segment Failure Retries ONLY the Failed Segment + Duplicate Retry
#    Deduplication + Bridge Restart Recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_segment_failure_retries_only_failed_segment_and_deduplicates_retry(
    tmp_path: Path,
) -> None:
    """When segment 1 fails after segment 0 succeeds, `retry_job` skips segment 0 and only re-submits segment 1; duplicate `retry_job` calls do not double-submit."""
    sample_audio = tmp_path / "multi.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.5)
    source_store = _make_fake_source_store(audio_bytes)

    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.upload_and_presign.side_effect = (
        lambda filepath, *, object_key=None, expires_in=None: (
            f"https://tos.example.com/{object_key}",
            object_key or "asr-temp/seg.mp3",
        )
    )
    tos_client.delete_object.return_value = True

    submitted_segments: list[str] = []
    fail_seg_1_once = True

    asr_client = create_autospec(sac.SeedAsrClient, instance=True)

    async def _transcribe_seg(
        *,
        audio_url: str,
        audio_format: str,
        persist_request_id_fn: Any,
        language: str | None = None,
        hotwords: list[str] | None = None,
        uid: str = "dalton-asr",
        request_id: str | None = None,
        already_submitted: bool = False,
        on_status_change_fn: Any = None,
        deadline_monotonic: float | None = None,
    ) -> sac.QueryOutcome:
        nonlocal fail_seg_1_once
        seg_tag = "seg_000" if "seg_000" in audio_url else "seg_001"
        submitted_segments.append(seg_tag)
        req_id = f"req-{seg_tag}-{len(submitted_segments)}"
        await persist_request_id_fn(req_id)
        await asyncio.sleep(0.02)
        if seg_tag == "seg_001" and fail_seg_1_once:
            fail_seg_1_once = False
            raise sac.SeedAsrError(
                "asr_provider_server_error",
                "Segment 1 failed with 55000001",
                provider_status="55000001",
                request_id=req_id,
                retryable=False,
                submit_uncertain=False,
            )
        if on_status_change_fn is not None:
            await on_status_change_fn("transcribing", req_id)
        return sac.QueryOutcome(
            request_id=req_id,
            state="succeeded",
            provider_status_code="20000000",
            response={
                "result": {
                    "text": f"内容-{seg_tag}",
                    "utterances": [
                        {
                            "text": f"内容-{seg_tag}",
                            "start_time": 500,
                            "end_time": 2500,
                            "additions": {"speaker": "1"},
                        }
                    ],
                }
            },
        )

    asr_client.transcribe_segment.side_effect = _transcribe_seg

    async def _two_seg_split(
        source_path: Path,
        chunks_dir: Path,
        *,
        total_duration_s: float,
        segment_time_s: int = 600,
        **kwargs: Any,
    ) -> list[asr_jobs.PreparedSegmentFile]:
        chunks_dir.mkdir(parents=True, exist_ok=True)
        out: list[asr_jobs.PreparedSegmentFile] = []
        for idx, (st, en) in enumerate(((0.0, 600.0), (600.0, 1200.0))):
            cpath = chunks_dir / f"chunk_{idx:02d}.mp3"
            cpath.write_bytes(audio_bytes)
            out.append(
                asr_jobs.PreparedSegmentFile(
                    index=idx,
                    path=cpath,
                    start_s=st,
                    end_s=en,
                    sha256=audio_sha,
                    size_bytes=len(audio_bytes),
                    codec_params={
                        "format": "mp3",
                        "codec": "libmp3lame",
                        "q_a": "2",
                        "segment_time": 600,
                    },
                )
            )
        return out

    jobs_dir = tmp_path / "jobs_partial"
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        config=asr_jobs.RealAsrWorkerConfig(split_threshold_seconds=600.0),
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=1200.0
        ),
        split_audio_fn=create_autospec(
            asr_jobs.split_audio_to_mp3_segments, side_effect=_two_seg_split
        ),
    )
    mgr = asr_jobs.AsrJobManager(jobs_dir, worker=worker)

    await mgr.submit_job(
        {
            "job_id": "job_partial_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_partial_01/source/multi.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-partial-01",
        }
    )

    for _ in range(60):
        _, st1 = await mgr.get_job("job_partial_01")
        if st1["job"]["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected first attempt to fail on segment 1")

    assert submitted_segments == ["seg_000", "seg_001"]
    assert st1["job"]["completed_segments"] == 1
    assert st1["job"]["segments"][0]["status"] == "succeeded"
    assert st1["job"]["segments"][1]["status"] == "failed"

    # Trigger two retry_job calls back-to-back: second call while active must deduplicate!
    code_r1, resp_r1 = await mgr.retry_job("job_partial_01")
    code_r2, resp_r2 = await mgr.retry_job("job_partial_01")
    assert code_r1 == 200
    assert code_r2 == 200
    assert resp_r2.get("deduplicated") is True

    for _ in range(60):
        _, st2 = await mgr.get_job("job_partial_01")
        if st2["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected retry to succeed")

    # Segment 0 was NOT re-submitted; only segment 1 was retried once!
    assert submitted_segments == ["seg_000", "seg_001", "seg_001"]


@pytest.mark.asyncio
async def test_bridge_restart_resumes_inflight_segment_via_query_without_resubmitting(
    tmp_path: Path,
) -> None:
    """When bridge restarts while a segment has a persisted `provider_request_id` in `transcribing`, the new manager resumes by querying `API_QUERY` without calling `API_SUBMIT`."""
    jobs_dir = tmp_path / "jobs_restart"
    job_id = "job_restart_01"
    job_dir = jobs_dir / job_id
    (job_dir / "segments").mkdir(parents=True, exist_ok=True)

    # Pre-populate state.json as left on disk mid-transcription before bridge restart
    persisted_state = {
        "job_id": job_id,
        "status": "transcribing",
        "stage": "transcribing",
        "version": 3,
        "attempt": 1,
        "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
        "source_object_key": "transcriptions/ws-1/job_restart_01/source/a.mp3",
        "source_size_bytes": 1024,
        "source_sha256": "a" * 64,
        "language": "en",
        "hotwords": [],
        "idempotency_key": "idemp-restart-01",
        "segment_count": 1,
        "completed_segments": 0,
        "current_segment": 0,
        "provider_request_ids": ["req-persisted-before-restart"],
        "segments": [
            {
                "index": 0,
                "start_s": 0.0,
                "end_s": 300.0,
                "duration_s": 300.0,
                "sha256": "a" * 64,
                "size_bytes": 1024,
                "codec_params": {"format": "mp3", "codec": "original"},
                "status": "transcribing",
                "provider_request_id": "req-persisted-before-restart",
                "tos_object_key": "asr-temp/job_restart_01/seg_000.mp3",
                "tos_cleaned": False,
                "attempt": 1,
            }
        ],
        "usage": {
            "user_id": "u-1",
            "workspace_id": "ws-1",
            "thread_id": "th-1",
            "duration_seconds": 300.0,
            "submitted_seconds": 300.0,
            "completed_seconds": 0.0,
            "billable_seconds": None,
        },
        "result_object_key": None,
        "error_category": None,
        "error_message": None,
        "created_at": "2026-10-28T00:00:00+00:00",
        "updated_at": "2026-10-28T00:00:05+00:00",
        "completed_at": None,
    }
    (job_dir / "state.json").write_text(
        json.dumps(persisted_state, indent=2), encoding="utf-8"
    )

    source_store = create_autospec(asr_jobs.SourceObjectStore, instance=True)
    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.delete_object.return_value = True

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    called_urls: list[str] = []

    async def _post(
        url: str, *, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None
    ) -> httpx.Response:
        called_urls.append(url)
        assert (headers or {}).get("X-Api-Request-Id") == "req-persisted-before-restart"
        return _make_httpx_response(
            status_code=200,
            api_status_code="20000000",
            json_body={
                "result": {
                    "text": "Resumed after bridge restart.",
                    "utterances": [
                        {
                            "text": "Resumed after bridge restart.",
                            "start_time": 200,
                            "end_time": 1800,
                            "additions": {"speaker": "1"},
                        }
                    ],
                }
            },
        )

    http_client.post.side_effect = _post
    asr_client = sac.SeedAsrClient(
        _test_credentials(),
        http_client=http_client,
        poll_interval_s=0.0,
    )
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
    )
    mgr = asr_jobs.AsrJobManager(jobs_dir, worker=worker)

    # Polling `get_job` after restart triggers `_ensure_worker_running`
    for _ in range(50):
        code, st = await mgr.get_job(job_id)
        assert code == 200
        if st["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Resumed job did not complete")

    # Verify ONLY `API_QUERY` was called, NEVER `API_SUBMIT` and NEVER re-downloaded!
    assert called_urls == [sac.API_QUERY]
    source_store.download_to_path.assert_not_called()
    tos_client.upload_and_presign.assert_not_called()
    tos_client.delete_object.assert_called_with("asr-temp/job_restart_01/seg_000.mp3")

    # Verify all A1c intermediate statuses + indeterminate are in RECOVERABLE_JOB_STATUSES
    assert {
        "queued",
        "downloading",
        "probing",
        "splitting",
        "submitting",
        "transcribing",
        "assembling",
        "indeterminate",
    }.issubset(asr_jobs.RECOVERABLE_JOB_STATUSES)

    # Also verify a job persisted in `indeterminate` on disk before restart is recovered via query-first
    indet_job_id = "job_restart_indet_02"
    indet_dir = jobs_dir / indet_job_id
    (indet_dir / "segments").mkdir(parents=True, exist_ok=True)
    indet_state = dict(persisted_state)
    indet_state["job_id"] = indet_job_id
    indet_state["status"] = "indeterminate"
    indet_state["stage"] = "indeterminate"
    indet_state["interrupted"] = True
    indet_state["segments"] = [
        {
            **persisted_state["segments"][0],
            "status": "indeterminate",
            "tos_object_key": "asr-temp/job_restart_indet_02/seg_000.mp3",
        }
    ]
    (indet_dir / "state.json").write_text(
        json.dumps(indet_state, indent=2), encoding="utf-8"
    )
    called_urls.clear()
    mgr2 = asr_jobs.AsrJobManager(jobs_dir, worker=worker)
    for _ in range(50):
        _, st_indet = await mgr2.get_job(indet_job_id)
        if st_indet["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Indeterminate job did not recover via query on restart")
    assert called_urls == [sac.API_QUERY]
    assert "interrupted" not in st_indet["job"]


# ---------------------------------------------------------------------------
# 7. Global Timestamp Offset, Speaker Segment Scope (`S{段号}-Speaker X`), and
#    Separate Raw vs Merged Files
# ---------------------------------------------------------------------------


def test_merge_global_timestamp_offset_and_speaker_segment_scope() -> None:
    """`Speaker 1` across segments is scoped as `S1-Speaker 1`, `S2-Speaker 1` (never conflated), and utterance timestamps add each segment's global offset while preserving `raw_start_ms` / `raw_end_ms`."""
    segments_meta = [
        {"index": 0, "start_s": 0.0, "end_s": 600.0},
        {"index": 1, "start_s": 600.0, "end_s": 1200.0},
    ]
    raw_responses = [
        {
            "result": {
                "text": "第一段话",
                "utterances": [
                    {
                        "text": "大家好，我是主持人。",
                        "start_time": 1500,
                        "end_time": 4200,
                        "additions": {"speaker": "1"},
                    },
                    {
                        "text": "我们看一季度指引。",
                        "start_time": 5000,
                        "end_time": 8000,
                        "additions": json.dumps({"speaker": "2"}),
                    },
                ],
            }
        },
        {
            "result": {
                "text": "第二段话",
                "utterances": [
                    {
                        "text": "下半场提问开始。",
                        "start_time": 2000,
                        "end_time": 6500,
                        "additions": {"speaker": "1"},
                    }
                ],
            }
        },
    ]

    merged_text, utterances = asr_jobs.merge_segment_transcriptions(
        segments_meta, raw_responses
    )
    assert "[S1-Speaker 1]\n大家好，我是主持人。" in merged_text
    assert "[S1-Speaker 2]\n我们看一季度指引。" in merged_text
    assert "[S2-Speaker 1]\n下半场提问开始。" in merged_text

    assert len(utterances) == 3
    # Segment 0 (offset 0ms)
    assert utterances[0]["speaker"] == "S1-Speaker 1"
    assert utterances[0]["start_ms"] == 1500 and utterances[0]["end_ms"] == 4200
    assert utterances[1]["speaker"] == "S1-Speaker 2"
    assert utterances[1]["start_ms"] == 5000 and utterances[1]["end_ms"] == 8000

    # Segment 1 (offset 600,000ms): Speaker 1 is scoped to S2-Speaker 1, NOT S1-Speaker 1!
    assert utterances[2]["speaker"] == "S2-Speaker 1"
    assert utterances[2]["raw_speaker"] == "1"
    assert utterances[2]["raw_start_ms"] == 2000
    assert utterances[2]["raw_end_ms"] == 6500
    assert utterances[2]["start_ms"] == 602000
    assert utterances[2]["end_ms"] == 606500

    # Original raw_responses dict timestamps remain untouched
    assert raw_responses[1]["result"]["utterances"][0]["start_time"] == 2000


# ---------------------------------------------------------------------------
# 8. Terminal Cleanup (Succeeded, Failed, Cancelled) + Cleanup Error Resilience
#    + Separate Raw / Transcript / Result Files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_cleanup_on_success_failure_and_cancel_and_cleanup_error_resilience(
    tmp_path: Path,
) -> None:
    """TOS objects and `<job_dir>/work/` are cleaned up on `succeeded`, `failed`, and `cancelled`; TOS delete errors are logged and never break the result."""
    sample_audio = tmp_path / "clean.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    # 1. Succeeded job where TOS cleanup raises an exception -> job still succeeds and local work dir is removed!
    tos_client_ok = create_autospec(sac.VolcTosClient, instance=True)
    tos_client_ok.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/s1.mp3",
        "asr-temp/s1.mp3",
    )

    def _noisy_delete(key: str) -> bool:
        return False

    tos_client_ok.delete_object.side_effect = _noisy_delete

    asr_client_ok = create_autospec(sac.SeedAsrClient, instance=True)

    async def _ok_transcribe(**kwargs: Any) -> sac.QueryOutcome:
        await kwargs["persist_request_id_fn"]("req-clean-ok")
        return sac.QueryOutcome(
            request_id="req-clean-ok",
            state="succeeded",
            provider_status_code="20000000",
            response={
                "result": {
                    "text": "完整纪要",
                    "utterances": [
                        {
                            "text": "完整纪要",
                            "start_time": 100,
                            "end_time": 800,
                            "additions": {"speaker": "1"},
                        }
                    ],
                }
            },
        )

    asr_client_ok.transcribe_segment.side_effect = _ok_transcribe

    jobs_dir = tmp_path / "jobs_cleanup"
    worker_ok = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client_ok,
        asr_client=asr_client_ok,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=60.0
        ),
    )
    res_ok = await worker_ok.run_job(
        {
            "job_id": "job_clean_ok",
            "attempt": 1,
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_clean_ok/source/clean.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
        },
        on_progress=lambda _p: asyncio.sleep(0),
    )
    assert res_ok["status"] == "succeeded"
    tos_client_ok.delete_object.assert_called_with("asr-temp/s1.mp3")
    assert not (jobs_dir / "job_clean_ok" / "work").exists()
    # Separate raw_response.json and transcript.txt exist and do not overwrite each other
    raw_json_path = jobs_dir / "job_clean_ok" / "raw_response.json"
    txt_path = jobs_dir / "job_clean_ok" / "transcript.txt"
    assert raw_json_path.is_file()
    assert txt_path.is_file()
    raw_saved = json.loads(raw_json_path.read_text(encoding="utf-8"))
    assert (
        raw_saved["segments"][0]["raw_response"]["result"]["utterances"][0]["start_time"]
        == 100
    )
    assert "[S1-Speaker 1]\n完整纪要" in txt_path.read_text(encoding="utf-8")

    # 2. Failed job cleans up TOS object and local `<job_dir>/work/`
    tos_client_fail = create_autospec(sac.VolcTosClient, instance=True)
    tos_client_fail.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/s_fail.mp3",
        "asr-temp/s_fail.mp3",
    )
    tos_client_fail.delete_object.return_value = True

    asr_client_fail = create_autospec(sac.SeedAsrClient, instance=True)

    async def _fail_transcribe(**kwargs: Any) -> sac.QueryOutcome:
        await kwargs["persist_request_id_fn"]("req-clean-fail")
        raise sac.SeedAsrError(
            "asr_format_error",
            "Unsupported audio stream",
            provider_status="45000151",
            request_id="req-clean-fail",
            retryable=False,
            submit_uncertain=False,
        )

    asr_client_fail.transcribe_segment.side_effect = _fail_transcribe
    worker_fail = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client_fail,
        asr_client=asr_client_fail,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=60.0
        ),
    )
    res_fail = await worker_fail.run_job(
        {
            "job_id": "job_clean_fail",
            "attempt": 1,
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_clean_fail/source/clean.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
        },
        on_progress=lambda _p: asyncio.sleep(0),
    )
    assert res_fail["status"] == "failed"
    tos_client_fail.delete_object.assert_called_with("asr-temp/s_fail.mp3")
    assert not (jobs_dir / "job_clean_fail" / "work").exists()

    # 3. Cancelled job cleans up TOS object and local `<job_dir>/work/`
    tos_client_cancel = create_autospec(sac.VolcTosClient, instance=True)
    tos_client_cancel.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/s_cancel.mp3",
        "asr-temp/s_cancel.mp3",
    )
    tos_client_cancel.delete_object.return_value = True

    asr_client_cancel = create_autospec(sac.SeedAsrClient, instance=True)

    async def _slow_transcribe(**kwargs: Any) -> sac.QueryOutcome:
        await kwargs["persist_request_id_fn"]("req-clean-cancel")
        await asyncio.sleep(10.0)
        raise AssertionError("Should have been cancelled")

    asr_client_cancel.transcribe_segment.side_effect = _slow_transcribe
    worker_cancel = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client_cancel,
        asr_client=asr_client_cancel,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=60.0
        ),
    )
    mgr_cancel = asr_jobs.AsrJobManager(jobs_dir, worker=worker_cancel)
    await mgr_cancel.submit_job(
        {
            "job_id": "job_clean_cancel",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_clean_cancel/source/clean.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-cancel-01",
        }
    )
    for _ in range(40):
        _, st_c = await mgr_cancel.get_job("job_clean_cancel")
        if st_c["job"]["segments"] and st_c["job"]["segments"][0].get("tos_object_key"):
            break
        await asyncio.sleep(0.01)

    code_c, body_c = await mgr_cancel.cancel_job("job_clean_cancel")
    assert code_c == 200
    assert body_c["job"]["status"] == "cancelled"
    await asyncio.sleep(0.02)
    tos_client_cancel.delete_object.assert_called_with("asr-temp/s_cancel.mp3")
    assert not (jobs_dir / "job_clean_cancel" / "work").exists()


# ---------------------------------------------------------------------------
# 9. Credentials Never Appear in Logs, Repr, Errors, or Subprocess Environments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_never_in_logs_or_subprocess_environments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`SEED_ASR_*` and `VOLC_TOS_*` never appear in `SeedAsrCredentials` repr/str, error logs, ASR subprocess allowlist env, or bridge child CLI environments."""
    secret_env = {
        "SEED_ASR_APP_ID": "SECRET_APP_ID_112233",
        "SEED_ASR_ACCESS_KEY": "SECRET_ACCESS_KEY_445566",
        "VOLC_TOS_AK": "SECRET_TOS_AK_778899",
        "VOLC_TOS_SK": "SECRET_TOS_SK_001122",
        "VOLC_TOS_BUCKET": "private-asr-bucket",
        "VOLC_TOS_REGION": "cn-beijing",
        "VOLC_TOS_ENDPOINT": "tos-cn-beijing.volces.com",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY": "SECRET_MINIO_ASR_KEY_999",
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    for k, v in secret_env.items():
        monkeypatch.setenv(k, v)

    creds = sac.SeedAsrCredentials.from_env()
    for secret_val in (
        "SECRET_APP_ID_112233",
        "SECRET_ACCESS_KEY_445566",
        "SECRET_TOS_AK_778899",
        "SECRET_TOS_SK_001122",
    ):
        assert secret_val not in repr(creds)
        assert secret_val not in str(creds)

    # 1. ASR subprocess environment allowlist (`build_asr_subprocess_env`)
    asr_sub_env = sac.build_asr_subprocess_env()
    for key in sac.ASR_CREDENTIAL_ENV_KEYS | sac.ASR_STORAGE_ENV_KEYS:
        assert key not in asr_sub_env
    for v in asr_sub_env.values():
        for secret_val in (
            "SECRET_APP_ID_112233",
            "SECRET_ACCESS_KEY_445566",
            "SECRET_TOS_AK_778899",
            "SECRET_TOS_SK_001122",
            "SECRET_MINIO_ASR_KEY_999",
        ):
            assert secret_val not in v

    # 2. Bridge child CLI environment builders (`_build_community_subprocess_env`, `strip_asr_credentials_from_env`)
    bridge = rb.ResearchDataBridge(rb.BridgeConfig())
    comm_env = bridge._build_community_subprocess_env()
    wiki_env = bridge._build_wiki_subprocess_env()
    stripped_env = sac.strip_asr_credentials_from_env(os.environ)
    for env_dict in (comm_env, wiki_env, stripped_env):
        for key in sac.ASR_CREDENTIAL_ENV_KEYS | sac.ASR_STORAGE_ENV_KEYS:
            assert key not in env_dict
        for val in env_dict.values():
            assert "SECRET_APP_ID_112233" not in val
            assert "SECRET_ACCESS_KEY_445566" not in val
            assert "SECRET_TOS_AK_778899" not in val
            assert "SECRET_TOS_SK_001122" not in val

    # 3. Error messages and logs redact secrets and presigned URL query strings
    caplog.set_level(logging.WARNING)
    s3_mock = create_autospec(sac.S3ClientProtocol, instance=True)
    s3_mock.delete_object.side_effect = RuntimeError(
        "Failed deleting https://tos-cn-beijing.volces.com/asr-temp/a.mp3?X-Amz-Signature=SECRET_TOS_SK_001122 with key SECRET_ACCESS_KEY_445566"
    )
    tos_client = sac.VolcTosClient(creds, s3_client=s3_mock)
    assert tos_client.delete_object("asr-temp/a.mp3") is False

    for record in caplog.records:
        msg = record.getMessage()
        assert "SECRET_TOS_SK_001122" not in msg
        assert "SECRET_ACCESS_KEY_445566" not in msg
        assert "X-Amz-Signature=" not in msg


# ---------------------------------------------------------------------------
# 10. TOS Multipart (>20 MiB in 10 MiB Parts) vs PutObject & Disk Admission
# ---------------------------------------------------------------------------


def test_tos_multipart_threshold_and_disk_admission_check(tmp_path: Path) -> None:
    """Files > threshold use multipart upload and abort on failure; disk admission enforces `2 * source_size + headroom`."""
    creds = _test_credentials()
    s3_mock = create_autospec(sac.S3ClientProtocol, instance=True)
    s3_mock.create_multipart_upload.return_value = {"UploadId": "up-123"}
    s3_mock.upload_part.side_effect = [
        {"ETag": '"etag-1"'},
        {"ETag": '"etag-2"'},
        {"ETag": '"etag-3"'},
    ]
    s3_mock.complete_multipart_upload.return_value = {}
    s3_mock.generate_presigned_url.return_value = (
        "https://dalton-asr-test-bucket.tos-cn-beijing.volces.com/asr-temp/big.mp3?X-Amz-Expires=7200"
    )

    # Use scaled threshold (10 KiB threshold, 5 MiB min part size clamped in VolcTosClient or tested via mock)
    small_file = tmp_path / "small.mp3"
    small_file.write_bytes(b"x" * 2048)

    tos_client = sac.VolcTosClient(
        creds,
        s3_client=s3_mock,
        multipart_threshold=1024,
    )
    url, key = tos_client.upload_and_presign(
        small_file, object_key="asr-temp/big.mp3", expires_in=7200
    )
    assert url.startswith("https://")
    assert key == "asr-temp/big.mp3"
    s3_mock.create_multipart_upload.assert_called_once()
    s3_mock.complete_multipart_upload.assert_called_once()

    # Multipart part upload failure triggers abort_multipart_upload
    s3_fail_mock = create_autospec(sac.S3ClientProtocol, instance=True)
    s3_fail_mock.create_multipart_upload.return_value = {"UploadId": "up-abort-99"}
    s3_fail_mock.upload_part.side_effect = RuntimeError("part upload network broken")
    tos_fail_client = sac.VolcTosClient(
        creds,
        s3_client=s3_fail_mock,
        multipart_threshold=1024,
    )
    with pytest.raises(sac.SeedAsrError) as exc_mp:
        tos_fail_client.upload_and_presign(
            small_file, object_key="asr-temp/abort.mp3", expires_in=7200
        )
    assert exc_mp.value.category == "tos_upload_failed"
    s3_fail_mock.abort_multipart_upload.assert_called_once_with(
        Bucket="dalton-asr-test-bucket",
        Key="asr-temp/abort.mp3",
        UploadId="up-abort-99",
    )

    # Disk admission check: requires 2 * source_size_bytes + headroom_bytes
    with pytest.raises(asr_jobs.AsrWorkerError) as exc_disk:
        asr_jobs.check_disk_admission(
            tmp_path / "work",
            source_size_bytes=500 * 1024 * 1024,  # 500 MiB -> needs > 1 GiB
            headroom_bytes=64 * 1024 * 1024,
            disk_usage_fn=lambda _p: DiskUsage(total=2000, used=1500, free=500 * 1024 * 1024),
        )
    assert exc_disk.value.category == "insufficient_disk_space"


@pytest.mark.asyncio
async def test_retry_policy_429_5xx_backoff_vs_immediate_auth_param_failure() -> None:
    """429 / 5xx on query back off and retry without creating a new recognition task; 401/403/45000xxx terminate immediately without retrying."""
    creds = _test_credentials()
    http_client = create_autospec(httpx.AsyncClient, instance=True)
    client = sac.SeedAsrClient(
        creds,
        http_client=http_client,
        request_timeout_s=12.5,
        job_deadline_s=600.0,
        poll_interval_s=0.0,
        initial_backoff_s=0.0,
        max_retries=3,
    )

    # 1. Immediate termination on 401 unauthorized (only 1 HTTP call, no backoff retry)
    auth_calls = 0

    async def _auth_fail(url: str, **kwargs: Any) -> httpx.Response:
        nonlocal auth_calls
        auth_calls += 1
        assert kwargs.get("timeout") == 12.5
        return _make_httpx_response(
            status_code=401,
            api_status_code="45000010",
            api_message="Invalid access key",
        )

    http_client.post.side_effect = _auth_fail
    with pytest.raises(sac.SeedAsrError) as exc_auth:
        await client.transcribe_segment(
            audio_url="https://tos.example.com/a.mp3",
            audio_format="mp3",
            persist_request_id_fn=lambda _rid: asyncio.sleep(0),
        )
    assert exc_auth.value.category == "asr_auth_error"
    assert exc_auth.value.retryable is False
    assert auth_calls == 1

    # 2. 429 rate-limit + 503 server error on query retry and succeed without re-submitting
    q_seq = 0
    sub_seq = 0

    async def _transient_then_ok(url: str, **kwargs: Any) -> httpx.Response:
        nonlocal q_seq, sub_seq
        if url == sac.API_SUBMIT:
            sub_seq += 1
            return _make_httpx_response(status_code=200, api_status_code="20000000")
        q_seq += 1
        if q_seq == 1:
            return _make_httpx_response(
                status_code=429, api_status_code="45000299", api_message="Rate limit"
            )
        if q_seq == 2:
            return _make_httpx_response(
                status_code=503, api_status_code="55000000", api_message="Busy"
            )
        return _make_httpx_response(
            status_code=200,
            api_status_code="20000000",
            json_body={"result": {"text": "退避重试成功", "utterances": []}},
        )

    http_client.post.side_effect = _transient_then_ok
    outcome = await client.transcribe_segment(
        audio_url="https://tos.example.com/a.mp3",
        audio_format="mp3",
        persist_request_id_fn=lambda _rid: asyncio.sleep(0),
    )
    assert outcome.state == "succeeded"
    assert sub_seq == 1
    assert q_seq == 3


# ---------------------------------------------------------------------------
# 10. A1c fix1 Regression Suite (Items 1 - 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix1_item1_xreach_version_strips_credentials_and_all_bridge_subprocesses_pass_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 1: `/healthz` `xreach --version` strips ASR/TOS credentials, and AST scan confirms every subprocess call in bridge modules explicitly passes `env=`."""
    # 1. AST audit of bridge modules (`researchdata_bridge.py` and `asr_jobs.py`)
    subproc_funcs = {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "Popen",
        "run",
        "check_output",
        "check_call",
        "call",
    }
    found_sites: list[tuple[str, int, str]] = []
    for mod_name, mod_path in (
        ("researchdata_bridge.py", BRIDGE_DIR / "researchdata_bridge.py"),
        ("asr_jobs.py", BRIDGE_DIR / "asr_jobs.py"),
    ):
        tree = ast.parse(mod_path.read_text(encoding="utf-8"), filename=mod_name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            call_name = ""
            if isinstance(func, ast.Attribute) and isinstance(
                func.value, ast.Name
            ):
                if func.value.id == "asyncio" and func.attr in (
                    "create_subprocess_exec",
                    "create_subprocess_shell",
                ):
                    call_name = f"asyncio.{func.attr}"
                elif func.value.id == "subprocess" and func.attr in subproc_funcs:
                    call_name = f"subprocess.{func.attr}"
            elif isinstance(func, ast.Name) and func.id in (
                "create_subprocess_exec",
                "create_subprocess_shell",
                "Popen",
            ):
                call_name = func.id

            if not call_name:
                continue
            found_sites.append((mod_name, node.lineno, call_name))
            kw_names = {kw.arg for kw in node.keywords if kw.arg is not None}
            assert "env" in kw_names, (
                f"{mod_name}:{node.lineno} ({call_name}) must explicitly pass `env=` keyword argument"
            )

    # 9 sites in researchdata_bridge.py + 2 sites in asr_jobs.py = 11 sites total
    assert len(found_sites) == 11

    # 2. Verify `probe_xreach_version` (`/healthz` xreach --version) strips all 7 ASR/TOS credentials
    creds = _test_credentials()
    for k, v in {
        "SEED_ASR_APP_ID": creds.app_id,
        "SEED_ASR_ACCESS_KEY": creds.access_key,
        "VOLC_TOS_AK": creds.tos_ak,
        "VOLC_TOS_SK": creds.tos_sk,
        "VOLC_TOS_BUCKET": creds.tos_bucket,
        "VOLC_TOS_REGION": creds.tos_region,
        "VOLC_TOS_ENDPOINT": creds.tos_endpoint,
    }.items():
        monkeypatch.setenv(k, v)

    fake_xreach = tmp_path / "xreach"
    fake_xreach.write_text("#!/bin/sh\necho 'xreach 1.0.0'\n", encoding="utf-8")
    fake_xreach.chmod(0o755)

    captured_envs: list[dict[str, str]] = []

    async def _fake_exec(*cmd: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        assert "env" in kwargs and isinstance(kwargs["env"], dict)
        captured_envs.append(dict(kwargs["env"]))
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.communicate.return_value = (b"xreach 1.0.0\n", b"")
        return proc

    monkeypatch.setattr(
        rb.asyncio,
        "create_subprocess_exec",
        create_autospec(rb.asyncio.create_subprocess_exec, side_effect=_fake_exec),
    )
    cfg = rb.BridgeConfig()
    cfg.xreach_bin = fake_xreach
    bridge = rb.ResearchDataBridge(cfg)
    res = await bridge.probe_xreach_version()
    assert res["ok"] is True
    assert len(captured_envs) == 1
    for secret_key in sac.ASR_CREDENTIAL_ENV_KEYS:
        assert secret_key not in captured_envs[0]
    for secret_val in (
        creds.app_id,
        creds.access_key,
        creds.tos_ak,
        creds.tos_sk,
        creds.tos_bucket,
        creds.tos_region,
        creds.tos_endpoint,
    ):
        assert secret_val not in str(captured_envs[0])


@pytest.mark.asyncio
async def test_fix1_item2_query_exhaustion_marks_indeterminate_not_failed_and_preserves_tos_and_id(
    tmp_path: Path,
) -> None:
    """Item 2: After submit succeeds, query network/429/5xx exhaustion marks segment & job `indeterminate` (not `failed`), keeps TOS object, and retries via `API_QUERY` with the original ID."""
    sample_audio = tmp_path / "q_exhaust.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.upload_and_presign.side_effect = (
        lambda filepath, *, object_key=None, expires_in=None: (
            f"https://tos.example.com/{object_key}",
            object_key or "asr-temp/q_exhaust.mp3",
        )
    )
    tos_client.delete_object.return_value = True

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    submit_reqs: list[str] = []
    query_reqs: list[str] = []
    query_mode = "503_exhaust"

    async def _post(
        url: str, *, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None
    ) -> httpx.Response:
        rid = (headers or {}).get("X-Api-Request-Id", "")
        if url == sac.API_SUBMIT:
            submit_reqs.append(rid)
            return _make_httpx_response(status_code=200, api_status_code="20000000")
        query_reqs.append(rid)
        if query_mode == "503_exhaust":
            return _make_httpx_response(
                status_code=503, api_status_code="55000000", api_message="Service busy"
            )
        return _make_httpx_response(
            status_code=200,
            api_status_code="20000000",
            json_body={"result": {"text": "查询恢复成功", "utterances": []}},
        )

    http_client.post.side_effect = _post
    asr_client = sac.SeedAsrClient(
        _test_credentials(),
        http_client=http_client,
        poll_interval_s=0.0,
        initial_backoff_s=0.0,
        max_retries=2,
    )

    jobs_dir = tmp_path / "jobs_q_exhaust"
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=60.0
        ),
    )
    mgr = asr_jobs.AsrJobManager(jobs_dir, worker=worker)

    await mgr.submit_job(
        {
            "job_id": "job_q_exhaust_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_q_exhaust_01/source/a.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-q-exhaust-01",
        }
    )

    for _ in range(50):
        _, st = await mgr.get_job("job_q_exhaust_01")
        if st["job"]["status"] in ("indeterminate", "failed"):
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected job to reach indeterminate")

    # Must be `indeterminate` (NOT `failed`), TOS must NOT be deleted, and request_id must be preserved!
    assert st["job"]["status"] == "indeterminate"
    assert st["job"]["segments"][0]["status"] == "indeterminate"
    orig_req_id = st["job"]["segments"][0]["provider_request_id"]
    assert orig_req_id and submit_reqs == [orig_req_id]
    assert len(query_reqs) == 3  # initial + 2 retries
    tos_client.delete_object.assert_not_called()

    # Also verify historical `failed` segment with a `provider_request_id` is treated as `already_submitted=True`
    state_on_disk = mgr.read_state("job_q_exhaust_01")
    assert state_on_disk is not None
    state_on_disk["status"] = "failed"
    state_on_disk["segments"][0]["status"] = "failed"
    mgr._write_state("job_q_exhaust_01", state_on_disk)

    query_mode = "ok"
    code_r, _ = await mgr.retry_job("job_q_exhaust_01")
    assert code_r == 200
    for _ in range(50):
        _, st_done = await mgr.get_job("job_q_exhaust_01")
        if st_done["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected retry to succeed via query")

    # Submit was NEVER called a second time!
    assert submit_reqs == [orig_req_id]
    assert query_reqs[-1] == orig_req_id
    tos_client.delete_object.assert_called_once()


@pytest.mark.asyncio
async def test_fix1_item3_persist_request_id_failure_aborts_before_submit(
    tmp_path: Path,
) -> None:
    """Item 3: When `_write_state` returns `False` or raises while persisting `request_id`, `API_SUBMIT` is never called and the segment fails as retryable with `provider_request_id=None`."""
    sample_audio = tmp_path / "persist_fail.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/pf.mp3",
        "asr-temp/pf.mp3",
    )
    tos_client.delete_object.return_value = True

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    submit_called = 0

    async def _post(url: str, **kwargs: Any) -> httpx.Response:
        nonlocal submit_called
        if url == sac.API_SUBMIT:
            submit_called += 1
        return _make_httpx_response(status_code=200, api_status_code="20000000")

    http_client.post.side_effect = _post
    asr_client = sac.SeedAsrClient(_test_credentials(), http_client=http_client)

    jobs_dir = tmp_path / "jobs_persist_fail"
    real_write = asr_jobs._fallback_atomic_write_json_locked
    fail_on_req_id = True

    def _flaky_write(path: Path, data: Any, *, label: str = "state") -> bool:
        if (
            fail_on_req_id
            and label == "asr_job_state"
            and isinstance(data, dict)
            and data.get("provider_request_ids")
        ):
            return False
        return real_write(path, data, label=label)

    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=30.0
        ),
        atomic_write_fn=_flaky_write,
    )
    mgr = asr_jobs.AsrJobManager(
        jobs_dir, worker=worker, atomic_write_fn=_flaky_write
    )

    await mgr.submit_job(
        {
            "job_id": "job_pf_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_pf_01/source/pf.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-pf-01",
        }
    )

    for _ in range(50):
        _, st = await mgr.get_job("job_pf_01")
        if st["job"]["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected job to fail when request_id persistence returned False")

    assert submit_called == 0, "API_SUBMIT must NEVER be called if request_id persistence failed"
    assert st["job"]["error_category"] == "asr_state_persist_failed"
    assert st["job"]["provider_request_ids"] == []
    assert st["job"]["segments"][0]["provider_request_id"] is None


@pytest.mark.asyncio
async def test_fix1_item4_deadline_checked_before_generating_id_and_clear_id_rules() -> None:
    """Item 4: Deadline expiry before submit does NOT generate/persist a new request_id; HTTP 404/400/422 & task_not_found clear unaccepted request_id."""
    creds = _test_credentials()
    http_client = create_autospec(httpx.AsyncClient, instance=True)
    client = sac.SeedAsrClient(creds, http_client=http_client)

    persisted_ids: list[str] = []

    async def _persist(rid: str) -> bool:
        persisted_ids.append(rid)
        return True

    # 1. Expired deadline before transcribe_segment -> must NOT call `_persist` or `http_client.post`
    with pytest.raises(sac.SeedAsrError) as exc_dl:
        await client.transcribe_segment(
            audio_url="https://tos.example.com/a.mp3",
            audio_format="mp3",
            persist_request_id_fn=_persist,
            deadline_monotonic=time.monotonic() - 1.0,
        )
    assert exc_dl.value.category == "asr_job_deadline_exceeded"
    assert persisted_ids == []
    http_client.post.assert_not_called()

    # 2. Verify `should_clear_request_id_on_error` rules
    # Bare HTTP 404 on query WITHOUT a `task_not_found` business status header is NOT treated as
    # `task_not_found`: it must retain the persisted request_id and transition to `indeterminate`.
    http_client.post.return_value = _make_httpx_response(
        status_code=404, api_status_code=None, raw_text="Not found"
    )
    with pytest.raises(sac.SeedAsrError) as exc_404_q:
        await client.query_once("req-404")
    assert exc_404_q.value.phase == "query"
    assert exc_404_q.value.task_not_found is False
    assert (
        sac.should_clear_request_id_on_error(exc_404_q.value, submit_accepted=True)
        is False
    )
    assert (
        sac.should_clear_request_id_on_error(exc_404_q.value, submit_accepted=False)
        is False
    )
    observed_statuses_404: list[tuple[str, str]] = []
    with pytest.raises(sac.SeedAsrIndeterminateError) as exc_404_indet:
        await client.transcribe_segment(
            audio_url="https://tos.example.com/a.mp3",
            audio_format="mp3",
            request_id="req-404-accepted",
            already_submitted=True,
            persist_request_id_fn=_persist,
            on_status_change_fn=lambda st, rid: observed_statuses_404.append((st, rid)),
        )
    assert exc_404_indet.value.request_id == "req-404-accepted"
    assert ("indeterminate", "req-404-accepted") in observed_statuses_404

    # Rule (a): Query with explicit `task_not_found` business status code (e.g. 45000007) -> clears ID
    http_client.post.return_value = _make_httpx_response(
        status_code=200,
        api_status_code="45000007",
        api_message="Task does not exist",
    )
    with pytest.raises(sac.SeedAsrError) as exc_tnf_q:
        await client.query_once("req-tnf")
    assert exc_tnf_q.value.phase == "query"
    assert exc_tnf_q.value.task_not_found is True
    assert (
        sac.should_clear_request_id_on_error(exc_tnf_q.value, submit_accepted=True)
        is True
    )
    assert (
        sac.should_clear_request_id_on_error(exc_tnf_q.value, submit_accepted=False)
        is True
    )

    # Rule (b): HTTP 400 / 422 on a never-accepted submit (`phase="submit"`, `submit_accepted=False`) -> clears ID
    http_client.post.return_value = _make_httpx_response(
        status_code=422, api_status_code="45000001", api_message="Invalid audio param"
    )
    with pytest.raises(sac.SeedAsrError) as exc_422_sub:
        await client.submit_once(
            request_id="req-422",
            audio_url="https://tos.example.com/a.mp3",
            audio_format="mp3",
        )
    assert exc_422_sub.value.phase == "submit"
    assert sac.should_clear_request_id_on_error(exc_422_sub.value, submit_accepted=False) is True
    assert sac.should_clear_request_id_on_error(exc_422_sub.value, submit_accepted=True) is False


@pytest.mark.asyncio
async def test_fix1_item5_job_deadline_constrains_download_probe_split_and_upload(
    tmp_path: Path,
) -> None:
    """Item 5: `job_deadline_s` constrains download, probe, split, and upload; pre-submit timeout marks `failed` (no ID generated), post-submit timeout marks `indeterminate`."""
    sample_audio = tmp_path / "dl_timeout.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)

    # 1. Download chunk loop checks `deadline_monotonic`
    real_store = asr_jobs.SourceObjectStore(
        s3_client=create_autospec(sac.S3ClientProtocol, instance=True),
        bucket="b",
    )
    with pytest.raises(asr_jobs.AsrWorkerError) as exc_dl_exp:
        real_store.download_to_path(
            "transcriptions/ws-1/j1/a.mp3",
            tmp_path / "out.mp3",
            expected_size_bytes=len(audio_bytes),
            expected_sha256=audio_sha,
            deadline_monotonic=time.monotonic() - 0.1,
        )
    assert exc_dl_exp.value.category == "asr_job_deadline_exceeded"

    # 2. Probe timeout bounded by remaining `job_deadline_s` before any segment is submitted -> `failed`
    observed_probe_timeouts: list[float] = []

    async def _slow_probe(audio_path: Path, *, timeout_s: float = 60.0, **kw: Any) -> float:
        observed_probe_timeouts.append(timeout_s)
        await asyncio.sleep(0.08)
        raise asr_jobs.AsrWorkerError("ffprobe_timeout", "ffprobe timed out")

    store_fast = _make_fake_source_store(audio_bytes)
    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.delete_object.return_value = True
    asr_client = create_autospec(sac.SeedAsrClient, instance=True)

    jobs_dir = tmp_path / "jobs_deadline_pre"
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        config=asr_jobs.RealAsrWorkerConfig(
            job_deadline_s=0.05,
            ffprobe_timeout_s=60.0,
        ),
        source_store=store_fast,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=_slow_probe,
    )
    mgr = asr_jobs.AsrJobManager(jobs_dir, worker=worker)
    await mgr.submit_job(
        {
            "job_id": "job_dl_pre_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_dl_pre_01/source/a.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-dl-pre-01",
        }
    )
    for _ in range(50):
        _, st = await mgr.get_job("job_dl_pre_01")
        if st["job"]["status"] in ("failed", "indeterminate"):
            break
        await asyncio.sleep(0.01)
    assert st["job"]["status"] == "failed"
    assert st["job"]["error_category"] == "asr_job_deadline_exceeded"
    assert st["job"]["provider_request_ids"] == []
    assert observed_probe_timeouts and observed_probe_timeouts[0] <= 0.05
    assert not (jobs_dir / "job_dl_pre_01" / "work").exists()


@pytest.mark.asyncio
async def test_fix1_item6_and_item7_pre_persisted_tos_key_cancel_mid_upload_and_uncategorized_exception_cleanup(
    tmp_path: Path,
) -> None:
    """Item 6 & Item 7: `tos_object_key` is persisted before upload; cancelling mid-upload deletes the TOS object once upload thread finishes; uncategorized exception (`FileNotFoundError`) cleans up work dir and TOS."""
    sample_audio = tmp_path / "up_cancel.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    upload_started = asyncio.Event()
    deleted_keys: list[str] = []
    persisted_key_at_upload_start: list[str | None] = []

    jobs_dir = tmp_path / "jobs_up_cancel"

    def _slow_upload(
        filepath: Path, *, object_key: str | None = None, expires_in: int | None = None
    ) -> tuple[str, str]:
        # Verify state.json on disk ALREADY has `tos_object_key` persisted BEFORE upload finishes!
        state_file = jobs_dir / "job_up_cancel_01" / "state.json"
        if state_file.is_file():
            st_data = json.loads(state_file.read_text(encoding="utf-8"))
            segs = st_data.get("segments") or []
            if segs:
                persisted_key_at_upload_start.append(segs[0].get("tos_object_key"))
        upload_started.set()
        time.sleep(0.06)
        return f"https://tos.example.com/{object_key}", str(object_key)

    def _delete_obj(key: str) -> bool:
        deleted_keys.append(key)
        return True

    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.upload_and_presign.side_effect = _slow_upload
    tos_client.delete_object.side_effect = _delete_obj

    asr_client = create_autospec(sac.SeedAsrClient, instance=True)
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=45.0
        ),
    )
    mgr = asr_jobs.AsrJobManager(jobs_dir, worker=worker)

    await mgr.submit_job(
        {
            "job_id": "job_up_cancel_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_up_cancel_01/source/a.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-up-cancel-01",
        }
    )

    await asyncio.wait_for(upload_started.wait(), timeout=2.0)
    assert persisted_key_at_upload_start and persisted_key_at_upload_start[0]
    pre_persisted_key = persisted_key_at_upload_start[0]

    # Cancel while upload thread is still in-flight
    code_c, cancel_resp = await mgr.cancel_job("job_up_cancel_01")
    assert code_c == 200
    assert cancel_resp["job"]["status"] == "cancelled"
    assert pre_persisted_key in deleted_keys
    assert not (jobs_dir / "job_up_cancel_01" / "work").exists()

    # Item 7: Uncategorized exception (e.g. `FileNotFoundError` when ffprobe is missing) cleans up `work/`
    async def _raise_fnf(*args: Any, **kwargs: Any) -> float:
        raise FileNotFoundError("ffprobe binary not found on host")

    worker_fnf = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=_raise_fnf,
    )
    mgr_fnf = asr_jobs.AsrJobManager(jobs_dir, worker=worker_fnf)
    await mgr_fnf.submit_job(
        {
            "job_id": "job_fnf_02",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_fnf_02/source/a.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-fnf-02",
        }
    )
    for _ in range(50):
        _, st_fnf = await mgr_fnf.get_job("job_fnf_02")
        if st_fnf["job"]["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected uncategorized exception to mark job failed")

    assert st_fnf["job"]["error_category"] == "worker_exception"
    assert "FileNotFoundError" in st_fnf["job"]["error_message"]
    assert not (jobs_dir / "job_fnf_02" / "work").exists(), (
        "Uncategorized exception entering terminal failed must clean up local work dir"
    )


@pytest.mark.asyncio
async def test_fix1_item8_and_item9_duration_persisted_across_restart_and_backend_accepts_indeterminate_to_transcribing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 8 & Item 9: Probed `duration_seconds` is persisted immediately after probing and preserved on restart recovery; backend `_sync_job_from_bridge_state` accepts `indeterminate -> transcribing` within the same attempt."""
    sample_audio = tmp_path / "dur_restart.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/dur.mp3",
        "asr-temp/dur.mp3",
    )
    tos_client.delete_object.return_value = True

    entered_transcribing = asyncio.Event()
    allow_finish = asyncio.Event()

    http_client = create_autospec(httpx.AsyncClient, instance=True)

    async def _post(
        url: str, *, json: Any = None, headers: dict[str, str] | None = None, timeout: Any = None
    ) -> httpx.Response:
        if url == sac.API_SUBMIT:
            return _make_httpx_response(status_code=200, api_status_code="20000000")
        entered_transcribing.set()
        await allow_finish.wait()
        return _make_httpx_response(
            status_code=200,
            api_status_code="20000000",
            json_body={"result": {"text": "时长保留测试", "utterances": []}},
        )

    http_client.post.side_effect = _post
    asr_client = sac.SeedAsrClient(
        _test_credentials(), http_client=http_client, poll_interval_s=0.01
    )

    jobs_dir = tmp_path / "jobs_dur_restart"
    worker1 = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=432.5
        ),
    )
    mgr1 = asr_jobs.AsrJobManager(jobs_dir, worker=worker1)
    await mgr1.submit_job(
        {
            "job_id": "job_dur_01",
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_dur_01/source/a.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
            "idempotency_key": "idemp-dur-01",
        }
    )

    await asyncio.wait_for(entered_transcribing.wait(), timeout=2.0)
    mid_state = mgr1.read_state("job_dur_01")
    assert mid_state is not None
    # Duration must ALREADY be persisted in state.json while in-flight!
    assert mid_state["duration_seconds"] == 432.5
    assert mid_state["usage"]["duration_seconds"] == 432.5

    # Simulate process exit (non-explicit cancel -> interrupted: True)
    running_task = mgr1._tasks.get("job_dur_01")
    assert running_task is not None
    running_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running_task

    interrupted_state = mgr1.read_state("job_dur_01")
    assert interrupted_state is not None
    assert interrupted_state.get("interrupted") is True
    assert interrupted_state["usage"]["duration_seconds"] == 432.5

    # Resume on a new manager without re-probing; duration_seconds must remain 432.5 (never 0.0)
    allow_finish.set()
    mgr2 = asr_jobs.AsrJobManager(jobs_dir, worker=worker1)
    for _ in range(50):
        _, st_after = await mgr2.get_job("job_dur_01")
        if st_after["job"]["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Expected interrupted job to recover and succeed")

    assert st_after["job"]["usage"]["duration_seconds"] == 432.5
    _, res_after = await mgr2.get_job_result("job_dur_01")
    assert res_after["result"]["duration_seconds"] == 432.5
    assert res_after["usage"]["duration_seconds"] == 432.5

    # Item 9: Backend `_sync_job_from_bridge_state` accepts `indeterminate -> transcribing` in same attempt
    from src.dalton.database import transcriptions as trans_db
    from src.dalton.services import transcriptions as trans_svc

    fake_db = create_autospec(trans_db, spec_set=True)
    db_row: dict[str, Any] = {
        "job_id": "job_sync_01",
        "version": 3,
        "status": "indeterminate",
        "stage": "indeterminate",
        "segment_count": 2,
        "completed_segments": 0,
        "provider_request_ids": ["req-1"],
        "usage": {"attempt": 1, "bridge_version": 4, "duration_seconds": 1200.0},
        "result_object_key": None,
        "error_category": "asr_request_timeout",
        "error_message": "Submit timed out",
    }

    async def _fake_update_versioned(
        job_id: str, *, expected_version: int, **updates: Any
    ) -> tuple[bool, dict[str, Any]]:
        assert expected_version == db_row["version"]
        db_row["version"] = expected_version + 1
        for k, v in updates.items():
            if v is not None or k in ("error_category", "error_message", "result_object_key"):
                db_row[k] = v
        return True, dict(db_row)

    fake_db.update_transcription_job_versioned.side_effect = _fake_update_versioned
    monkeypatch.setattr(trans_svc, "db", fake_db)

    # 1. Stale version (`version: 4` <= `bridge_version: 4`) in same attempt is ignored
    out_stale = await trans_svc._sync_job_from_bridge_state(
        db_row,
        {
            "status": "transcribing",
            "stage": "transcribing",
            "attempt": 1,
            "version": 4,
        },
    )
    assert out_stale["status"] == "indeterminate"
    assert fake_db.update_transcription_job_versioned.await_count == 0

    # 2. Newer version (`version: 5` > `bridge_version: 4`) in same attempt transitions `indeterminate -> transcribing`
    out_trans = await trans_svc._sync_job_from_bridge_state(
        db_row,
        {
            "status": "transcribing",
            "stage": "transcribing",
            "attempt": 1,
            "version": 5,
            "segment_count": 2,
            "completed_segments": 1,
            "provider_request_ids": ["req-1"],
            "usage": {"duration_seconds": 1200.0},
        },
    )
    assert out_trans["status"] == "transcribing"
    assert out_trans["usage"]["bridge_version"] == 5
    assert fake_db.update_transcription_job_versioned.await_count == 1


# ---------------------------------------------------------------------------
# 11. A1c fix2 Regression Suite (Items 1 - 8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fix2_items_1_to_4_query_errors_retain_id_and_indeterminate_while_only_task_not_found_and_pre_accept_submit_clear_id(
    tmp_path: Path,
) -> None:
    """Items 1-4: Query-side 401/403/404(bare)/422/missing-header/invalid-JSON/structure-invalid/45000151 ALWAYS retain `provider_request_id`, mark `indeterminate`, and keep TOS; ONLY query business-code `task_not_found` (a) or pre-accept submit rejection (b) clears ID."""
    sample_audio = tmp_path / "fix2_q.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    source_store = _make_fake_source_store(audio_bytes)

    query_error_responses: list[tuple[str, httpx.Response]] = [
        (
            "q_401",
            _make_httpx_response(
                status_code=401, api_status_code="45000010", api_message="Unauthorized"
            ),
        ),
        (
            "q_403",
            _make_httpx_response(
                status_code=403, api_status_code="45000030", api_message="Forbidden"
            ),
        ),
        (
            "q_404_bare",
            _make_httpx_response(
                status_code=404, api_status_code=None, raw_text="404 gateway route"
            ),
        ),
        (
            "q_422",
            _make_httpx_response(
                status_code=422, api_status_code="45000001", api_message="Bad query"
            ),
        ),
        (
            "q_missing_header",
            _make_httpx_response(
                status_code=200, api_status_code=None, json_body={"result": {"text": "x"}}
            ),
        ),
        (
            "q_invalid_json",
            _make_httpx_response(
                status_code=200, api_status_code="20000000", raw_text="{not-valid-json"
            ),
        ),
        (
            "q_bad_structure",
            _make_httpx_response(
                status_code=200,
                api_status_code="20000000",
                json_body={"result": {"unexpected_field": 123}},
            ),
        ),
        (
            "q_45000151",
            _make_httpx_response(
                status_code=200,
                api_status_code="45000151",
                api_message="Audio format/download error on query",
            ),
        ),
    ]

    jobs_dir = tmp_path / "jobs_fix2_q"

    # Part A: After submit succeeds (`submit_accepted=True`), every query error above must
    # retain `provider_request_id`, mark `indeterminate`, and NEVER delete TOS.
    for label, q_resp in query_error_responses:
        tos_client = create_autospec(sac.VolcTosClient, instance=True)
        tos_client.upload_and_presign.side_effect = (
            lambda filepath, *, object_key=None, expires_in=None: (
                f"https://tos.example.com/{object_key}",
                object_key or f"asr-temp/{label}.mp3",
            )
        )
        tos_client.delete_object.return_value = True

        http_client = create_autospec(httpx.AsyncClient, instance=True)

        async def _post_accepted_then_q_err(
            url: str,
            *,
            json: Any = None,
            headers: dict[str, str] | None = None,
            timeout: Any = None,
            _resp: httpx.Response = q_resp,
        ) -> httpx.Response:
            if url == sac.API_SUBMIT:
                return _make_httpx_response(status_code=200, api_status_code="20000000")
            return _resp

        http_client.post.side_effect = _post_accepted_then_q_err
        asr_client = sac.SeedAsrClient(
            _test_credentials(),
            http_client=http_client,
            poll_interval_s=0.0,
            initial_backoff_s=0.0,
            max_retries=1,
        )
        worker = asr_jobs.RealAsrWorker(
            jobs_dir,
            source_store=source_store,
            tos_client=tos_client,
            asr_client=asr_client,
            probe_duration_fn=create_autospec(
                asr_jobs.probe_audio_duration, return_value=30.0
            ),
        )
        job_id = f"job_acc_{label}"
        out = await worker.run_job(
            {
                "job_id": job_id,
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": f"transcriptions/ws-1/{job_id}/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        assert out["status"] == "indeterminate", f"Query error {label} must mark job indeterminate"
        assert out["segments"][0]["status"] == "indeterminate", (
            f"Query error {label} must mark segment indeterminate"
        )
        assert out["segments"][0]["provider_request_id"], (
            f"Query error {label} must retain provider_request_id"
        )
        assert out["provider_request_ids"] == [
            out["segments"][0]["provider_request_id"]
        ]
        tos_client.delete_object.assert_not_called()

    # Part B (Item 2): Uncertain submit (timeout/502) followed by reconciliation query returning
    # non-`task_not_found` errors (e.g. 422, bare 404, 45000151, bad structure) must ALSO stay
    # `indeterminate`, retain `provider_request_id`, and NEVER delete TOS.
    for label, q_resp in query_error_responses:
        tos_client_unc = create_autospec(sac.VolcTosClient, instance=True)
        tos_client_unc.upload_and_presign.return_value = (
            f"https://tos.example.com/asr-temp/unc_{label}.mp3",
            f"asr-temp/unc_{label}.mp3",
        )
        tos_client_unc.delete_object.return_value = True

        http_client_unc = create_autospec(httpx.AsyncClient, instance=True)

        async def _post_uncertain_then_q_err(
            url: str,
            *,
            json: Any = None,
            headers: dict[str, str] | None = None,
            timeout: Any = None,
            _resp: httpx.Response = q_resp,
        ) -> httpx.Response:
            if url == sac.API_SUBMIT:
                raise httpx.ReadTimeout("Submit response lost")
            return _resp

        http_client_unc.post.side_effect = _post_uncertain_then_q_err
        asr_client_unc = sac.SeedAsrClient(
            _test_credentials(),
            http_client=http_client_unc,
            poll_interval_s=0.0,
            initial_backoff_s=0.0,
            max_retries=1,
        )
        worker_unc = asr_jobs.RealAsrWorker(
            jobs_dir,
            source_store=source_store,
            tos_client=tos_client_unc,
            asr_client=asr_client_unc,
            probe_duration_fn=create_autospec(
                asr_jobs.probe_audio_duration, return_value=30.0
            ),
        )
        job_id_unc = f"job_unc_{label}"
        out_unc = await worker_unc.run_job(
            {
                "job_id": job_id_unc,
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": f"transcriptions/ws-1/{job_id_unc}/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        assert out_unc["status"] == "indeterminate", (
            f"Reconciliation query error {label} after uncertain submit must stay indeterminate"
        )
        assert out_unc["segments"][0]["provider_request_id"], (
            f"Reconciliation query error {label} must retain provider_request_id"
        )
        tos_client_unc.delete_object.assert_not_called()

    # Part C (Case a): Query returning provider business code `task_not_found` (`45000007`) clears ID
    tos_client_tnf = create_autospec(sac.VolcTosClient, instance=True)
    tos_client_tnf.upload_and_presign.return_value = (
        "https://tos.example.com/asr-temp/tnf.mp3",
        "asr-temp/tnf.mp3",
    )
    tos_client_tnf.delete_object.return_value = True
    http_client_tnf = create_autospec(httpx.AsyncClient, instance=True)

    async def _post_tnf(url: str, **kwargs: Any) -> httpx.Response:
        if url == sac.API_SUBMIT:
            return _make_httpx_response(status_code=200, api_status_code="20000000")
        return _make_httpx_response(
            status_code=200,
            api_status_code="45000007",
            api_message="Task does not exist",
        )

    http_client_tnf.post.side_effect = _post_tnf
    worker_tnf = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client_tnf,
        asr_client=sac.SeedAsrClient(
            _test_credentials(), http_client=http_client_tnf, poll_interval_s=0.0
        ),
        probe_duration_fn=create_autospec(
            asr_jobs.probe_audio_duration, return_value=30.0
        ),
    )
    out_tnf = await worker_tnf.run_job(
        {
            "job_id": "job_tnf_clear",
            "attempt": 1,
            "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
            "source_object_key": "transcriptions/ws-1/job_tnf_clear/source/a.mp3",
            "source_size_bytes": len(audio_bytes),
            "source_sha256": audio_sha,
        },
        on_progress=lambda _p: asyncio.sleep(0),
    )
    assert out_tnf["status"] == "failed"
    assert out_tnf["segments"][0]["provider_request_id"] is None
    assert out_tnf["provider_request_ids"] == []
    tos_client_tnf.delete_object.assert_called_once_with("asr-temp/tnf.mp3")

    # Part D (Case b): Submit itself returning 400 / 422 when never accepted clears ID
    for sub_status in (400, 422):
        tos_client_sub = create_autospec(sac.VolcTosClient, instance=True)
        tos_client_sub.upload_and_presign.return_value = (
            f"https://tos.example.com/asr-temp/sub_{sub_status}.mp3",
            f"asr-temp/sub_{sub_status}.mp3",
        )
        tos_client_sub.delete_object.return_value = True
        http_client_sub = create_autospec(httpx.AsyncClient, instance=True)
        http_client_sub.post.return_value = _make_httpx_response(
            status_code=sub_status,
            api_status_code="45000001",
            api_message="Invalid submit parameter",
        )
        worker_sub = asr_jobs.RealAsrWorker(
            jobs_dir,
            source_store=source_store,
            tos_client=tos_client_sub,
            asr_client=sac.SeedAsrClient(
                _test_credentials(), http_client=http_client_sub, poll_interval_s=0.0
            ),
            probe_duration_fn=create_autospec(
                asr_jobs.probe_audio_duration, return_value=30.0
            ),
        )
        out_sub = await worker_sub.run_job(
            {
                "job_id": f"job_sub_{sub_status}",
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": f"transcriptions/ws-1/job_sub_{sub_status}/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        assert out_sub["status"] == "failed"
        assert out_sub["segments"][0]["provider_request_id"] is None
        assert out_sub["provider_request_ids"] == []
        tos_client_sub.delete_object.assert_called_once_with(
            f"asr-temp/sub_{sub_status}.mp3"
        )


@pytest.mark.asyncio
async def test_fix2_item5_historical_state_without_submit_accepted_treated_as_accepted(
    tmp_path: Path,
) -> None:
    """Item 5: Historical segments carrying `provider_request_id` without a `submit_accepted` field (including historical `failed`) are treated as accepted and never clear their ID on non-`task_not_found` errors."""
    jobs_dir = tmp_path / "jobs_fix2_hist"
    source_store = create_autospec(asr_jobs.SourceObjectStore, instance=True)
    tos_client = create_autospec(sac.VolcTosClient, instance=True)
    tos_client.delete_object.return_value = True

    http_client = create_autospec(httpx.AsyncClient, instance=True)
    # Return a non-task-not-found error (e.g. 45000151 or HTTP 422) on query
    http_client.post.return_value = _make_httpx_response(
        status_code=200,
        api_status_code="45000151",
        api_message="Audio fetch error",
    )
    asr_client = sac.SeedAsrClient(
        _test_credentials(), http_client=http_client, poll_interval_s=0.0
    )
    worker = asr_jobs.RealAsrWorker(
        jobs_dir,
        source_store=source_store,
        tos_client=tos_client,
        asr_client=asr_client,
    )

    for hist_status in ("transcribing", "failed", "submitting", "indeterminate"):
        job_id = f"job_hist_{hist_status}"
        hist_seg: dict[str, Any] = {
            "index": 0,
            "start_s": 0.0,
            "end_s": 120.0,
            "duration_s": 120.0,
            "codec_params": {"format": "mp3"},
            "status": hist_status,
            "provider_request_id": f"req-hist-{hist_status}",
            "tos_object_key": f"asr-temp/{job_id}/seg_000.mp3",
            "tos_cleaned": False,
            "attempt": 1,
            # Intentionally NO `submit_accepted` key in historical state
        }
        assert "submit_accepted" not in hist_seg

        out = await worker.run_job(
            {
                "job_id": job_id,
                "attempt": 2,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": f"transcriptions/ws-1/{job_id}/source/a.mp3",
                "source_size_bytes": 1024,
                "source_sha256": "a" * 64,
                "duration_seconds": 120.0,
                "provider_request_ids": [f"req-hist-{hist_status}"],
                "segments": [hist_seg],
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        assert out["status"] == "indeterminate"
        assert out["segments"][0]["status"] == "indeterminate"
        assert out["segments"][0]["submit_accepted"] is True
        assert out["segments"][0]["provider_request_id"] == f"req-hist-{hist_status}"
        assert out["provider_request_ids"] == [f"req-hist-{hist_status}"]
        tos_client.delete_object.assert_not_called()


@pytest.mark.asyncio
async def test_fix2_item6_and_item7_blocking_download_and_upload_wait_bounded_by_deadline_and_interrupted_upload_never_deletes_tos(
    tmp_path: Path,
) -> None:
    """Items 6 & 7: Blocking download/upload threads are bounded by `asyncio.wait_for(..., remaining_s)` and transition on time (`failed` if unsubmitted, `indeterminate` if a prior segment was submitted); `interrupted` upload NEVER deletes TOS."""
    sample_audio = tmp_path / "fix2_dl.mp3"
    audio_bytes, audio_sha = _generate_synthetic_audio(sample_audio, duration_s=1.0)
    jobs_dir = tmp_path / "jobs_fix2_dl_up"

    # 1. Blocking `download_to_path` (simulating a hung `get_object` / `body.read` in thread)
    #    Case 1a: Pre-submit -> times out via `asyncio.wait_for` and lands in retryable `failed` on time.
    release_dl = threading.Event()
    try:
        blocking_store = create_autospec(asr_jobs.SourceObjectStore, instance=True)

        def _hung_download(
            object_key: str,
            dest_path: Path,
            **kwargs: Any,
        ) -> tuple[int, str]:
            release_dl.wait(timeout=1.0)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(audio_bytes)
            return len(audio_bytes), audio_sha

        blocking_store.download_to_path.side_effect = _hung_download
        tos_client = create_autospec(sac.VolcTosClient, instance=True)
        tos_client.delete_object.return_value = True
        asr_client = create_autospec(sac.SeedAsrClient, instance=True)

        worker_dl_pre = asr_jobs.RealAsrWorker(
            jobs_dir,
            config=asr_jobs.RealAsrWorkerConfig(job_deadline_s=0.1),
            source_store=blocking_store,
            tos_client=tos_client,
            asr_client=asr_client,
        )
        t0 = time.monotonic()
        out_dl_pre = await worker_dl_pre.run_job(
            {
                "job_id": "job_hung_dl_pre",
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/job_hung_dl_pre/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        elapsed_pre = time.monotonic() - t0
        assert elapsed_pre < 0.5, "wait_for must bound blocking download without waiting 1.0s"
        assert out_dl_pre["status"] == "failed"
        assert out_dl_pre["error_category"] == "asr_job_deadline_exceeded"

        # Case 1b: Post-submit (segment 0 already has a `provider_request_id`, segment 1 needs local audio)
        #          -> blocking download timeout lands in `indeterminate`!
        t1 = time.monotonic()
        out_dl_post = await worker_dl_pre.run_job(
            {
                "job_id": "job_hung_dl_post",
                "attempt": 2,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/job_hung_dl_post/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
                "duration_seconds": 1200.0,
                "provider_request_ids": ["req-seg0-submitted"],
                "segments": [
                    {
                        "index": 0,
                        "start_s": 0.0,
                        "end_s": 600.0,
                        "status": "indeterminate",
                        "provider_request_id": "req-seg0-submitted",
                        "submit_accepted": True,
                    },
                    {
                        "index": 1,
                        "start_s": 600.0,
                        "end_s": 1200.0,
                        "status": "pending",
                        "provider_request_id": None,
                    },
                ],
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        elapsed_post = time.monotonic() - t1
        assert elapsed_post < 0.5
        assert out_dl_post["status"] == "indeterminate"
        assert out_dl_post["error_category"] == "asr_poll_deadline_exceeded"
        assert out_dl_post["provider_request_ids"] == ["req-seg0-submitted"]
    finally:
        release_dl.set()

    # 2. Blocking upload + terminal `cleanup_job_resources` wait bounded by deadline, retaining deferred delete
    release_up = threading.Event()
    deleted_after_timeout: list[str] = []
    try:
        fast_store = _make_fake_source_store(audio_bytes)
        tos_slow = create_autospec(sac.VolcTosClient, instance=True)

        def _hung_upload(
            filepath: Path, *, object_key: str | None = None, expires_in: int | None = None
        ) -> tuple[str, str]:
            release_up.wait(timeout=1.0)
            return f"https://tos.example.com/{object_key}", str(object_key)

        tos_slow.upload_and_presign.side_effect = _hung_upload
        tos_slow.delete_object.side_effect = lambda k: (
            deleted_after_timeout.append(k) or True
        )

        worker_up_to = asr_jobs.RealAsrWorker(
            jobs_dir,
            config=asr_jobs.RealAsrWorkerConfig(job_deadline_s=0.1),
            source_store=fast_store,
            tos_client=tos_slow,
            asr_client=create_autospec(sac.SeedAsrClient, instance=True),
            probe_duration_fn=create_autospec(
                asr_jobs.probe_audio_duration, return_value=30.0
            ),
        )
        t2 = time.monotonic()
        out_up_to = await worker_up_to.run_job(
            {
                "job_id": "job_hung_up_pre",
                "attempt": 1,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/job_hung_up_pre/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        elapsed_up = time.monotonic() - t2
        assert elapsed_up < 0.5, (
            "cleanup_job_resources gather must be bounded by remaining deadline and not block 1.0s"
        )
        assert out_up_to["status"] == "failed"
        assert out_up_to["error_category"] == "asr_job_deadline_exceeded"
        timed_out_key = out_up_to["segments"][0]["tos_object_key"]
        assert timed_out_key
    finally:
        release_up.set()

    # Wait briefly for the released upload thread's deferred callback to delete the orphan TOS key
    for _ in range(30):
        if timed_out_key in deleted_after_timeout:
            break
        await asyncio.sleep(0.01)
    assert timed_out_key in deleted_after_timeout

    # 3. Item 7: Non-explicit `CancelledError` (`interrupted: True` on process shutdown) during
    #    in-flight upload MUST NOT schedule or call TOS `delete_object`.
    up_started_evt = asyncio.Event()
    release_interrupted_up = threading.Event()
    interrupted_deletes: list[str] = []
    try:
        tos_interrupted = create_autospec(sac.VolcTosClient, instance=True)

        def _interrupted_upload(
            filepath: Path, *, object_key: str | None = None, expires_in: int | None = None
        ) -> tuple[str, str]:
            up_started_evt.set()
            release_interrupted_up.wait(timeout=1.0)
            return f"https://tos.example.com/{object_key}", str(object_key)

        tos_interrupted.upload_and_presign.side_effect = _interrupted_upload
        tos_interrupted.delete_object.side_effect = lambda k: (
            interrupted_deletes.append(k) or True
        )

        worker_intr = asr_jobs.RealAsrWorker(
            jobs_dir,
            source_store=_make_fake_source_store(audio_bytes),
            tos_client=tos_interrupted,
            asr_client=create_autospec(sac.SeedAsrClient, instance=True),
            probe_duration_fn=create_autospec(
                asr_jobs.probe_audio_duration, return_value=30.0
            ),
        )
        mgr_intr = asr_jobs.AsrJobManager(jobs_dir, worker=worker_intr)
        await mgr_intr.submit_job(
            {
                "job_id": "job_intr_up_01",
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/job_intr_up_01/source/a.mp3",
                "source_size_bytes": len(audio_bytes),
                "source_sha256": audio_sha,
                "idempotency_key": "idemp-intr-up-01",
            }
        )
        await asyncio.wait_for(up_started_evt.wait(), timeout=2.0)

        # Simulate process shutdown (cancel task directly WITHOUT calling `mgr.cancel_job`)
        running_task = mgr_intr._tasks.get("job_intr_up_01")
        assert running_task is not None
        running_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running_task

        intr_state = mgr_intr.read_state("job_intr_up_01")
        assert intr_state is not None
        assert intr_state.get("interrupted") is True
        assert intr_state["status"] != "cancelled"
    finally:
        release_interrupted_up.set()

    # Let the background upload thread finish completely and verify NO TOS delete was scheduled!
    await asyncio.sleep(0.05)
    assert interrupted_deletes == []
    tos_interrupted.delete_object.assert_not_called()


@pytest.mark.asyncio
async def test_fix2_item8_backend_sync_allows_only_indeterminate_to_transcribing_and_rejects_submitting_and_assembling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 8: Within the same attempt and strictly larger bridge version, `_sync_job_from_bridge_state` ONLY allows `indeterminate -> transcribing` and rejects `indeterminate -> submitting` and `indeterminate -> assembling`."""
    from src.dalton.database import transcriptions as trans_db
    from src.dalton.services import transcriptions as trans_svc

    fake_db = create_autospec(trans_db, spec_set=True)
    db_row: dict[str, Any] = {
        "job_id": "job_sync_fix2",
        "version": 2,
        "status": "indeterminate",
        "stage": "indeterminate",
        "segment_count": 2,
        "completed_segments": 1,
        "provider_request_ids": ["req-1"],
        "usage": {"attempt": 1, "bridge_version": 5, "duration_seconds": 1200.0},
        "result_object_key": None,
        "error_category": "asr_request_timeout",
        "error_message": "Query timed out",
    }

    async def _fake_update_versioned(
        job_id: str, *, expected_version: int, **updates: Any
    ) -> tuple[bool, dict[str, Any]]:
        assert expected_version == db_row["version"]
        db_row["version"] = expected_version + 1
        for k, v in updates.items():
            if v is not None or k in (
                "error_category",
                "error_message",
                "result_object_key",
            ):
                db_row[k] = v
        return True, dict(db_row)

    fake_db.update_transcription_job_versioned.side_effect = _fake_update_versioned
    monkeypatch.setattr(trans_svc, "db", fake_db)

    # 1. `indeterminate -> submitting` in same attempt (version 6 > 5) MUST be rejected
    res_sub = await trans_svc._sync_job_from_bridge_state(
        db_row,
        {
            "status": "submitting",
            "stage": "submitting",
            "attempt": 1,
            "version": 6,
        },
    )
    assert res_sub["status"] == "indeterminate"
    assert fake_db.update_transcription_job_versioned.await_count == 0

    # 2. `indeterminate -> assembling` in same attempt (version 7 > 5) MUST be rejected
    res_asm = await trans_svc._sync_job_from_bridge_state(
        db_row,
        {
            "status": "assembling",
            "stage": "assembling",
            "attempt": 1,
            "version": 7,
        },
    )
    assert res_asm["status"] == "indeterminate"
    assert fake_db.update_transcription_job_versioned.await_count == 0

    # 3. `indeterminate -> transcribing` in same attempt (version 8 > 5) IS allowed
    res_tr = await trans_svc._sync_job_from_bridge_state(
        db_row,
        {
            "status": "transcribing",
            "stage": "transcribing",
            "attempt": 1,
            "version": 8,
            "segment_count": 2,
            "completed_segments": 1,
            "provider_request_ids": ["req-1"],
            "usage": {"duration_seconds": 1200.0},
        },
    )
    assert res_tr["status"] == "transcribing"
    assert res_tr["usage"]["bridge_version"] == 8
    assert fake_db.update_transcription_job_versioned.await_count == 1





@pytest.mark.asyncio
async def test_each_run_downloads_to_its_own_file_and_drops_earlier_runs_copies(
    tmp_path: Path,
) -> None:
    """A download thread outlives its `wait_for` timeout; the retry of the same job must not share its path."""
    dest_paths: list[Path] = []
    sources_at_download: list[list[Path]] = []
    store = create_autospec(asr_jobs.SourceObjectStore, instance=True)

    def _record_and_fail(object_key: str, dest_path: Path, **kwargs: Any) -> tuple[int, str]:
        dest_paths.append(dest_path)
        sources_at_download.append(sorted(dest_path.parent.glob("source.*")))
        # A file left behind, as by a download thread that outlived its timeout
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"x" * 1024)
        raise asr_jobs.AsrWorkerError("source_download_failed", "simulated")

    store.download_to_path.side_effect = _record_and_fail
    worker = asr_jobs.RealAsrWorker(
        tmp_path / "jobs_unique_dl",
        source_store=store,
        tos_client=create_autospec(sac.VolcTosClient, instance=True),
        asr_client=create_autospec(sac.SeedAsrClient, instance=True),
    )
    for attempt in (1, 1, 2):
        out = await worker.run_job(
            {
                "job_id": "job_unique_dl",
                "attempt": attempt,
                "caller": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"},
                "source_object_key": "transcriptions/ws-1/job_unique_dl/source/a.mp3",
                "source_size_bytes": 1024,
                "source_sha256": "a" * 64,
            },
            on_progress=lambda _p: asyncio.sleep(0),
        )
        assert out["status"] == "failed"
        # A non-terminal leftover, as an indeterminate run would leave it
        dest_paths[-1].parent.mkdir(parents=True, exist_ok=True)
        dest_paths[-1].write_bytes(b"x" * 1024)
    assert len(dest_paths) == 3
    assert len(set(dest_paths)) == 3
    assert all(p.suffix == ".mp3" for p in dest_paths)
    # Each download starts with no earlier run's copy left beside it
    assert sources_at_download == [[], [], []]
