"""Unit tests for Backup Hotfix Round 2:
1. Terminal transcription job source audio expires by design after
   ``TRANSCRIPTION_AUDIO_EXPIRE_DAYS`` (7 days, shared constant) and is no longer
   required in ``storage_refs.enumerate_snapshot_refs()``, while active,
   submitted awaiting_upload, and indeterminate jobs still require it.
2. ``storage_bucket_rules`` manages ``dalton-transcription-audio-expire``
   (``Prefix: transcriptions/``, current-version 7-day expiration) on ``secondary``,
   replaces equivalent unmanaged rules while preserving all other unmanaged rules,
   and read-back verifies via both ``configure_minio.sh`` (``storage_bucket_rules apply``)
   and Standalone Core ``scripts/standalone/minio_init.py``.

All test doubles use ``create_autospec``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import boto3
import psycopg
import pytest
from botocore.exceptions import ClientError

from src.dalton import consistent_backup, storage_bucket_rules, storage_refs

REPO_ROOT = Path(__file__).resolve().parents[3]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_autospec_s3_client(
    *,
    buckets: set[str],
    versioning: dict[str, str] | None = None,
    lifecycle: dict[str, list[dict[str, Any]]] | None = None,
    policy: dict[str, str] | None = None,
    readback_lifecycle_override: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Build a ``create_autospec`` boto3 S3 client backed by in-memory bucket state."""
    s3_spec = boto3.session.Session().client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id="ak",
        aws_secret_access_key="sk",
        region_name="us-east-1",
    )
    fake_s3 = create_autospec(s3_spec)
    state: dict[str, Any] = {
        "buckets": set(buckets),
        "versioning": dict(versioning or {}),
        "lifecycle": {k: [dict(r) for r in v] for k, v in (lifecycle or {}).items()},
        "policy": dict(policy or {}),
        "readback_lifecycle_override": {
            k: [dict(r) for r in v]
            for k, v in (readback_lifecycle_override or {}).items()
        },
        "put_lifecycle_calls": [],
    }

    def _head_bucket(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        if Bucket not in state["buckets"]:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadBucket",
            )
        return {}

    def _create_bucket(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        state["buckets"].add(Bucket)
        return {}

    def _get_bucket_policy(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        if Bucket not in state["policy"]:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucketPolicy", "Message": "none"}},
                "GetBucketPolicy",
            )
        return {"Policy": state["policy"][Bucket]}

    def _put_bucket_policy(*, Bucket: str, Policy: str, **_kw: Any) -> dict[str, Any]:
        state["policy"][Bucket] = Policy
        return {}

    def _delete_bucket_policy(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        if Bucket not in state["policy"]:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucketPolicy", "Message": "none"}},
                "DeleteBucketPolicy",
            )
        del state["policy"][Bucket]
        return {}

    def _get_bucket_versioning(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        st = state["versioning"].get(Bucket)
        return {"Status": st} if st else {}

    def _put_bucket_versioning(
        *, Bucket: str, VersioningConfiguration: dict[str, Any], **_kw: Any
    ) -> dict[str, Any]:
        state["versioning"][Bucket] = VersioningConfiguration["Status"]
        return {}

    def _get_bucket_lifecycle(*, Bucket: str, **_kw: Any) -> dict[str, Any]:
        if Bucket in state["readback_lifecycle_override"] and state["put_lifecycle_calls"]:
            return {"Rules": [dict(r) for r in state["readback_lifecycle_override"][Bucket]]}
        if Bucket not in state["lifecycle"]:
            raise ClientError(
                {"Error": {"Code": "NoSuchLifecycleConfiguration", "Message": "none"}},
                "GetBucketLifecycleConfiguration",
            )
        return {"Rules": [dict(r) for r in state["lifecycle"][Bucket]]}

    def _put_bucket_lifecycle(
        *, Bucket: str, LifecycleConfiguration: dict[str, Any], **_kw: Any
    ) -> dict[str, Any]:
        rules = [dict(r) for r in LifecycleConfiguration.get("Rules", [])]
        state["lifecycle"][Bucket] = rules
        state["put_lifecycle_calls"].append((Bucket, rules))
        return {}

    fake_s3.head_bucket.side_effect = _head_bucket
    fake_s3.create_bucket.side_effect = _create_bucket
    fake_s3.get_bucket_policy.side_effect = _get_bucket_policy
    fake_s3.put_bucket_policy.side_effect = _put_bucket_policy
    fake_s3.delete_bucket_policy.side_effect = _delete_bucket_policy
    fake_s3.get_bucket_versioning.side_effect = _get_bucket_versioning
    fake_s3.put_bucket_versioning.side_effect = _put_bucket_versioning
    fake_s3.get_bucket_lifecycle_configuration.side_effect = _get_bucket_lifecycle
    fake_s3.put_bucket_lifecycle_configuration.side_effect = _put_bucket_lifecycle
    return fake_s3, state


def _make_autospec_async_db_conn(rows_by_query_fn: Any) -> Any:
    """Build a ``create_autospec`` psycopg AsyncConnection + AsyncCursor."""
    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    last_result: dict[str, Any] = {"val": []}

    async def _execute(sql: Any, params: Any = None, **_kw: Any) -> Any:
        last_result["val"] = rows_by_query_fn(str(sql), params)
        return cur

    async def _fetchone() -> Any:
        val = last_result["val"]
        if isinstance(val, list):
            return val[0] if val else None
        return val

    async def _fetchall() -> list[Any]:
        val = last_result["val"]
        if isinstance(val, list):
            return list(val)
        return [val] if val is not None else []

    cur.execute = AsyncMock(side_effect=_execute)
    cur.fetchone = AsyncMock(side_effect=_fetchone)
    cur.fetchall = AsyncMock(side_effect=_fetchall)
    cur.__aenter__ = AsyncMock(return_value=cur)
    cur.__aexit__ = AsyncMock(return_value=False)

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cur
    return conn


def _load_minio_init_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "minio_init_r2_test", REPO_ROOT / "scripts/standalone/minio_init.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. Shared constant & storage_refs transcription source audio expiration
# ---------------------------------------------------------------------------


def test_storage_refs_and_storage_bucket_rules_share_transcription_expire_days_constant() -> None:
    assert storage_refs.TRANSCRIPTION_AUDIO_EXPIRE_DAYS == 7
    assert (
        storage_refs.TRANSCRIPTION_AUDIO_EXPIRE_DAYS
        is storage_bucket_rules.TRANSCRIPTION_AUDIO_EXPIRE_DAYS
    )
    assert (
        storage_bucket_rules.RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE
        == "dalton-transcription-audio-expire"
    )
    assert storage_bucket_rules.TRANSCRIPTION_AUDIO_PREFIX == "transcriptions/"


def test_storage_refs_transcription_source_audio_expiration_and_inflight_retention(
    tmp_path: Path,
) -> None:
    now = dt.datetime(2026, 11, 28, 12, 0, 0, tzinfo=dt.timezone.utc)
    audio_bytes = b"synthetic-asr-source-audio"
    audio_sha = _sha(audio_bytes)
    done_usage = {"upload": {"completed": True}}
    pending_usage = {"upload": {"completed": False}}
    submit_usage = {"submit_attempted": True}

    def _row(
        job_id: str,
        status: str,
        *,
        usage: dict[str, Any],
        created_at: dt.datetime | None,
        completed_at: dt.datetime | None = None,
        updated_at: dt.datetime | None = None,
    ) -> tuple[Any, ...]:
        return (
            job_id,
            status,
            f"transcriptions/ws1/{job_id}/source/call.m4a",
            len(audio_bytes),
            audio_sha,
            usage,
            created_at,
            completed_at,
            updated_at,
        )

    rows = [
        # Terminal succeeded > 7d -> exempt from source audio reference, still in terminal/succeeded archive IDs
        _row(
            "job-succeeded-8d",
            "succeeded",
            usage=done_usage,
            created_at=now - dt.timedelta(days=8),
            completed_at=now - dt.timedelta(days=8) + dt.timedelta(minutes=5),
        ),
        # Terminal succeeded just past 7d boundary (+1s) -> exempt
        _row(
            "job-succeeded-7d-1s",
            "succeeded",
            usage=done_usage,
            created_at=now - dt.timedelta(days=7, seconds=1),
            completed_at=now - dt.timedelta(days=7),
        ),
        # Terminal succeeded with created_at=None, completed_at > 7d -> fallback works -> exempt
        _row(
            "job-succeeded-fallback-10d",
            "succeeded",
            usage=done_usage,
            created_at=None,
            completed_at=now - dt.timedelta(days=10),
        ),
        # Terminal succeeded at exact 7d boundary -> still required (not yet exceeded 7d)
        _row(
            "job-succeeded-exact-7d",
            "succeeded",
            usage=done_usage,
            created_at=now - dt.timedelta(days=7),
            completed_at=now - dt.timedelta(days=7) + dt.timedelta(minutes=2),
        ),
        # Terminal succeeded within 7d (3d) -> still required
        _row(
            "job-succeeded-3d",
            "succeeded",
            usage=done_usage,
            created_at=now - dt.timedelta(days=3),
            completed_at=now - dt.timedelta(days=3) + dt.timedelta(minutes=2),
        ),
        # Terminal succeeded with no timestamps (legacy mock) -> conservatively required
        _row(
            "job-succeeded-no-ts",
            "succeeded",
            usage=done_usage,
            created_at=None,
            completed_at=None,
            updated_at=None,
        ),
        # Non-terminal in-flight statuses > 30d -> ALL still required
        _row("job-queued-30d", "queued", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-downloading-30d", "downloading", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-probing-30d", "probing", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-splitting-30d", "splitting", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-submitting-30d", "submitting", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-transcribing-30d", "transcribing", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-assembling-30d", "assembling", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        # Indeterminate > 30d -> still required
        _row("job-indeterminate-30d", "indeterminate", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        # awaiting_upload with completed upload or submit_attempted > 30d -> still required
        _row("job-awaiting-uploaded-30d", "awaiting_upload", usage=done_usage, created_at=now - dt.timedelta(days=30)),
        _row("job-awaiting-submitted-30d", "awaiting_upload", usage=submit_usage, created_at=now - dt.timedelta(days=30)),
        # awaiting_upload before upload/submit -> skipped_pending
        _row("job-awaiting-pending-30d", "awaiting_upload", usage=pending_usage, created_at=now - dt.timedelta(days=30)),
        # Terminal failed/cancelled -> in terminal_asr_job_ids only, never required
        _row("job-failed-1d", "failed", usage=done_usage, created_at=now - dt.timedelta(days=1)),
        _row("job-cancelled-10d", "cancelled", usage=done_usage, created_at=now - dt.timedelta(days=10)),
    ]

    def _query_handler(sql: str, params: Any) -> Any:
        s = " ".join(sql.split())
        if s.startswith("SELECT to_regclass"):
            tbl = params[0] if params else ""
            if tbl == "public.dalton_transcription_jobs":
                return (tbl,)
            return (None,)
        if "FROM dalton_transcription_jobs" in s:
            return rows
        return []

    conn = _make_autospec_async_db_conn(_query_handler)
    inv = asyncio.run(
        storage_refs.enumerate_snapshot_refs(
            conn, include_main=False, include_aux=True, now=now
        )
    )

    ref_ids = {r.row_id for r in inv.references if r.consumer == "dalton_transcription_jobs"}
    # Expired succeeded jobs are NOT required references
    assert "job-succeeded-8d" not in ref_ids
    assert "job-succeeded-7d-1s" not in ref_ids
    assert "job-succeeded-fallback-10d" not in ref_ids
    assert inv.skipped_expired.get("dalton_transcription_jobs") == 3

    # Fresh / boundary / timestamp-less succeeded jobs ARE required references
    assert "job-succeeded-exact-7d" in ref_ids
    assert "job-succeeded-3d" in ref_ids
    assert "job-succeeded-no-ts" in ref_ids

    # All active, indeterminate, and uploaded/submitted awaiting_upload jobs ARE required regardless of age
    for must_keep in (
        "job-queued-30d",
        "job-downloading-30d",
        "job-probing-30d",
        "job-splitting-30d",
        "job-submitting-30d",
        "job-transcribing-30d",
        "job-assembling-30d",
        "job-indeterminate-30d",
        "job-awaiting-uploaded-30d",
        "job-awaiting-submitted-30d",
    ):
        assert must_keep in ref_ids

    # Pending awaiting_upload and failed/cancelled are NOT required references
    assert "job-awaiting-pending-30d" not in ref_ids
    assert inv.skipped_pending.get("dalton_transcription_jobs") == 1
    assert "job-failed-1d" not in ref_ids
    assert "job-cancelled-10d" not in ref_ids

    # Expired succeeded jobs MUST still be archived as terminal/succeeded ASR jobs (result.json)
    for jid in (
        "job-succeeded-8d",
        "job-succeeded-7d-1s",
        "job-succeeded-fallback-10d",
        "job-succeeded-exact-7d",
        "job-succeeded-3d",
        "job-succeeded-no-ts",
    ):
        assert jid in inv.terminal_asr_job_ids
        assert jid in inv.succeeded_asr_job_ids
    assert "job-failed-1d" in inv.terminal_asr_job_ids
    assert "job-cancelled-10d" in inv.terminal_asr_job_ids

    # Reconcile against manifest that omits expired succeeded source audio -> passes with zero errors & zero missing
    asr_dir = tmp_path / "asr_jobs"
    for jid in inv.terminal_asr_job_ids:
        jdir = asr_dir / jid
        jdir.mkdir(parents=True)
        st_val = (
            "failed"
            if jid == "job-failed-1d"
            else ("cancelled" if jid == "job-cancelled-10d" else "succeeded")
        )
        (jdir / "state.json").write_text(json.dumps({"status": st_val}), encoding="utf-8")
        if st_val == "succeeded":
            (jdir / "result.json").write_text(json.dumps({"text": "ok"}), encoding="utf-8")
    asr_tar = tmp_path / "asr-jobs.tar.gz"
    consistent_backup.archive_terminal_asr_jobs(asr_dir, inv.terminal_asr_job_ids, asr_tar)

    manifest = [
        {
            "bucket": "dalton-asr",
            "key": ref.object_key,
            "size": len(audio_bytes),
            "archived_sha256": audio_sha,
        }
        for ref in inv.references
    ]
    report = storage_refs.reconcile_inventory_against_manifest(
        inv,
        manifest,
        bucket_targets={"main": ("dalton-main", ""), "asr": ("dalton-asr", ""), "chat_upload": ("dalton-asr", "")},
        asr_jobs_archive=asr_tar,
    )
    assert report.ok is True, report.errors
    assert report.errors == ()
    assert report.known_missing == ()

    # Inventory round-trip preserves skipped_expired
    restored_inv = storage_refs.SnapshotRefInventory.from_dict(inv.to_dict())
    assert restored_inv.skipped_expired == {"dalton_transcription_jobs": 3}


# ---------------------------------------------------------------------------
# 2. storage_bucket_rules: secondary managed rule + equivalent unmanaged replacement
# ---------------------------------------------------------------------------


def test_storage_bucket_rules_secondary_adds_transcription_expire_and_replaces_equivalent_unmanaged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    eq_v2 = {
        "ID": "manual-asr-7d",
        "Status": "Enabled",
        "Filter": {"Prefix": "transcriptions/"},
        "Expiration": {"Days": 7},
    }
    eq_v1 = {
        "ID": "legacy-v1-asr-7d",
        "Status": "Enabled",
        "Prefix": "transcriptions/",
        "Expiration": {"Days": 7},
    }
    keep_diff_days = {
        "ID": "custom-transcriptions-14d",
        "Status": "Enabled",
        "Filter": {"Prefix": "transcriptions/"},
        "Expiration": {"Days": 14},
    }
    keep_diff_prefix = {
        "ID": "custom-tmp-7d",
        "Status": "Enabled",
        "Filter": {"Prefix": "tmp/"},
        "Expiration": {"Days": 7},
    }
    keep_tagged = {
        "ID": "custom-tagged-asr-7d",
        "Status": "Enabled",
        "Filter": {
            "And": {
                "Prefix": "transcriptions/",
                "Tags": [{"Key": "tier", "Value": "cold"}],
            }
        },
        "Expiration": {"Days": 7},
    }
    keep_disabled = {
        "ID": "custom-disabled-asr-7d",
        "Status": "Disabled",
        "Filter": {"Prefix": "transcriptions/"},
        "Expiration": {"Days": 7},
    }
    keep_compound = {
        "ID": "custom-compound-asr-7d",
        "Status": "Enabled",
        "Filter": {"Prefix": "transcriptions/"},
        "Expiration": {"Days": 7},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 60},
    }
    stale_uploads = {
        "ID": storage_bucket_rules.RULE_ID_UPLOADS_EXPIRE,
        "Status": "Enabled",
        "Filter": {"Prefix": "uploads/"},
        "Expiration": {"Days": 2},
    }

    fake_s3, state = _make_autospec_s3_client(
        buckets={"dalton-asr"},
        versioning={"dalton-asr": "Suspended"},
        lifecycle={
            "dalton-asr": [
                eq_v2,
                eq_v1,
                keep_diff_days,
                keep_diff_prefix,
                keep_tagged,
                keep_disabled,
                keep_compound,
                stale_uploads,
            ]
        },
    )

    res = storage_bucket_rules.apply_bucket_rules(
        fake_s3, bucket="dalton-asr", role="secondary", key_prefix=""
    )
    assert res.versioning_status == "Enabled"
    assert res.removed_secondary_uploads_rule is True
    assert res.managed_rule_ids == (
        "dalton-transcription-audio-expire",
        "dalton-noncurrent-expire",
        "dalton-expired-delete-marker",
    )
    assert [r["ID"] for r in res.replaced_equivalent_rules] == [
        "manual-asr-7d",
        "legacy-v1-asr-7d",
    ]
    assert [r["ID"] for r in res.preserved_rules] == [
        "custom-transcriptions-14d",
        "custom-tmp-7d",
        "custom-tagged-asr-7d",
        "custom-disabled-asr-7d",
        "custom-compound-asr-7d",
    ]

    written_by_id = {r["ID"]: r for r in state["lifecycle"]["dalton-asr"]}
    assert "manual-asr-7d" not in written_by_id
    assert "legacy-v1-asr-7d" not in written_by_id
    assert "dalton-uploads-expire" not in written_by_id
    tx_rule = written_by_id["dalton-transcription-audio-expire"]
    assert tx_rule == {
        "ID": "dalton-transcription-audio-expire",
        "Status": "Enabled",
        "Filter": {"Prefix": "transcriptions/"},
        "Expiration": {"Days": 7},
    }
    for kept_id in (
        "custom-transcriptions-14d",
        "custom-tmp-7d",
        "custom-tagged-asr-7d",
        "custom-disabled-asr-7d",
        "custom-compound-asr-7d",
    ):
        assert kept_id in written_by_id

    # CLI entrypoint used by scripts/dalton/configure_minio.sh also applies, replaces, and verifies
    fake_s3_cli, _cli_state = _make_autospec_s3_client(
        buckets={"dalton-asr"},
        lifecycle={"dalton-asr": [eq_v2, keep_diff_prefix]},
    )
    monkeypatch.setattr(
        storage_bucket_rules,
        "_build_s3_client_from_env",
        create_autospec(storage_bucket_rules._build_s3_client_from_env, return_value=fake_s3_cli),
    )
    rc = storage_bucket_rules.main(
        ["apply", "--bucket", "dalton-asr", "--role", "secondary", "--prefix", ""]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Replaced equivalent unmanaged lifecycle rule" in out
    assert "manual-asr-7d" in out
    assert "dalton-transcription-audio-expire" in out
    assert "Preserved unmanaged lifecycle rule" in out
    assert "custom-tmp-7d" in out


def test_storage_bucket_rules_secondary_prefix_scoping_and_readback_verification() -> None:
    eq_prefixed = {
        "ID": "manual-staging-asr-7d",
        "Status": "Enabled",
        "Filter": {"Prefix": "staging/transcriptions/"},
        "Expiration": {"Days": 7},
    }
    other_unprefixed = {
        "ID": "manual-root-asr-7d",
        "Status": "Enabled",
        "Filter": {"Prefix": "transcriptions/"},
        "Expiration": {"Days": 7},
    }
    fake_s3, state = _make_autospec_s3_client(
        buckets={"shared-asr"},
        lifecycle={"shared-asr": [eq_prefixed, other_unprefixed]},
    )

    res = storage_bucket_rules.apply_bucket_rules(
        fake_s3, bucket="shared-asr", role="secondary", key_prefix="staging"
    )
    assert [r["ID"] for r in res.replaced_equivalent_rules] == ["manual-staging-asr-7d"]
    assert [r["ID"] for r in res.preserved_rules] == ["manual-root-asr-7d"]
    by_id = {r["ID"]: r for r in state["lifecycle"]["shared-asr"]}
    assert by_id["dalton-transcription-audio-expire"]["Filter"]["Prefix"] == "staging/transcriptions/"
    assert by_id["dalton-transcription-audio-expire"]["Expiration"]["Days"] == 7

    # Main role does NOT add dalton-transcription-audio-expire or replace unmanaged transcriptions/ 7d
    fake_main, main_state = _make_autospec_s3_client(
        buckets={"dalton-main"},
        lifecycle={"dalton-main": [other_unprefixed]},
    )
    res_main = storage_bucket_rules.apply_bucket_rules(
        fake_main, bucket="dalton-main", role="main", key_prefix=""
    )
    assert "dalton-transcription-audio-expire" not in res_main.managed_rule_ids
    assert [r["ID"] for r in res_main.preserved_rules] == ["manual-root-asr-7d"]
    assert "dalton-transcription-audio-expire" not in {
        r["ID"] for r in main_state["lifecycle"]["dalton-main"]
    }

    # Read-back verification fails if duplicate equivalent unmanaged rule remains
    bad_dup_s3, _ = _make_autospec_s3_client(
        buckets={"dalton-asr"},
        lifecycle={"dalton-asr": [other_unprefixed]},
        readback_lifecycle_override={
            "dalton-asr": [
                other_unprefixed,
                *storage_bucket_rules.build_lifecycle_rules(role="secondary", key_prefix=""),
            ]
        },
    )
    with pytest.raises(storage_bucket_rules.BucketRuleVerificationError, match="equivalent"):
        storage_bucket_rules.apply_bucket_rules(
            bad_dup_s3, bucket="dalton-asr", role="secondary", key_prefix=""
        )

    # Read-back verification fails if prefix or days drift on dalton-transcription-audio-expire
    bad_days_rules = storage_bucket_rules.build_lifecycle_rules(role="secondary", key_prefix="")
    bad_days_rules[0]["Expiration"]["Days"] = 30
    with pytest.raises(storage_bucket_rules.BucketRuleVerificationError, match="Expiration.Days"):
        storage_bucket_rules.verify_lifecycle_rules(bad_days_rules, role="secondary", key_prefix="")

    bad_prefix_rules = storage_bucket_rules.build_lifecycle_rules(role="secondary", key_prefix="")
    bad_prefix_rules[0]["Filter"]["Prefix"] = ""
    with pytest.raises(storage_bucket_rules.BucketRuleVerificationError, match="prefix"):
        storage_bucket_rules.verify_lifecycle_rules(bad_prefix_rules, role="secondary", key_prefix="")


# ---------------------------------------------------------------------------
# 3. Standalone Core minio_init.py applies & read-back verifies secondary rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asr_bucket_name", ["transcriptions", "chat-uploads"])
def test_standalone_minio_init_applies_and_verifies_transcription_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    asr_bucket_name: str,
) -> None:
    minio_init = _load_minio_init_module()
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "chat-uploads")
    monkeypatch.setenv("MINIO_ROOT_USER", "ak")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "sk")
    monkeypatch.delenv("STORAGE_BUCKET_NAME", raising=False)
    monkeypatch.delenv("S3_BUCKET_NAME", raising=False)
    monkeypatch.setenv("DALTON_ASR_STORAGE_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("DALTON_ASR_STORAGE_BUCKET_NAME", asr_bucket_name)
    monkeypatch.setenv("DALTON_ASR_STORAGE_KEY_PREFIX", "tenant-a")

    eq_unmanaged = {
        "ID": "op-transcriptions-7d",
        "Status": "Enabled",
        "Filter": {"Prefix": "tenant-a/transcriptions/"},
        "Expiration": {"Days": 7},
    }
    keep_other = {
        "ID": "op-other-rule",
        "Status": "Enabled",
        "Filter": {"Prefix": "archive/"},
        "Expiration": {"Days": 90},
    }

    fake_s3, state = _make_autospec_s3_client(
        buckets={"chat-uploads", asr_bucket_name},
        lifecycle={asr_bucket_name: [eq_unmanaged, keep_other]},
    )
    monkeypatch.setattr(
        minio_init.boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=fake_s3),
    )

    assert minio_init.main() == 0

    written = state["lifecycle"][asr_bucket_name]
    by_id = {r["ID"]: r for r in written}
    assert "op-transcriptions-7d" not in by_id
    assert "op-other-rule" in by_id
    assert by_id["dalton-transcription-audio-expire"] == {
        "ID": "dalton-transcription-audio-expire",
        "Status": "Enabled",
        "Filter": {"Prefix": "tenant-a/transcriptions/"},
        "Expiration": {"Days": 7},
    }
    assert "dalton-noncurrent-expire" in by_id
    assert "dalton-expired-delete-marker" in by_id

    # Read-back mismatch fails minio_init with exit code 1
    bad_s3, _ = _make_autospec_s3_client(
        buckets={"chat-uploads", asr_bucket_name},
        lifecycle={asr_bucket_name: []},
        readback_lifecycle_override={asr_bucket_name: []},
    )
    monkeypatch.setattr(
        minio_init.boto3,
        "client",
        create_autospec(boto3.session.Session().client, return_value=bad_s3),
    )
    assert minio_init.main() == 1
